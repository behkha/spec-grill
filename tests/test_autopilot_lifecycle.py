"""Tests for how the autopilot stops sessions and keeps its dispatcher alive: escalating a stop that a
session ignores, freeing the integration worktree a stopped card held, the Chrome check, the merge lock
of a batch session, malformed log lines, and a dispatcher pass that fails.

    python3 -m pytest -q tests/test_autopilot_lifecycle.py
"""

import contextlib
import os
import shutil
import signal
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
from test_autopilot import RESUME, TASKS  # noqa: E402,F401
import test_autopilot as base  # noqa: E402  (a Feature name here would make pytest run its tests again)


class Lifecycle(unittest.TestCase):
    """A feature on the fake `claude`, with the Feature fixture's helpers."""

    setUp = base.Feature.setUp
    script_for, launched, wait_calls = base.Feature.script_for, base.Feature.launched, base.Feature.wait_calls
    state, resume_text, finish = base.Feature.state, base.Feature.resume_text, base.Feature.finish
    touches, disjoint = base.Feature.touches, base.Feature.disjoint
    add_to_tasks, add_batch, BATCH = base.Feature.add_to_tasks, base.Feature.add_batch, base.Feature.BATCH

    def tearDown(self):
        for run in autopilot.registry(self.tasks)["runs"]:  # SIGKILL: some of these fakes ignore SIGTERM
            if not run["ended"] and run.get("pid"):
                with contextlib.suppress(OSError):
                    os.killpg(int(run["pid"]), signal.SIGKILL)
        autopilot.release(self.tasks)
        shutil.rmtree(self.root, ignore_errors=True)

    def runs(self) -> list:
        return autopilot.registry(self.tasks)["runs"]

    def wait_log(self, run: dict, text: str) -> None:
        path = os.path.join(autopilot.state_dir(self.tasks), run["log"])
        for _ in range(100):
            if os.path.exists(path) and text in open(path).read():
                return
            time.sleep(0.1)
        self.fail(f"the session never wrote {text!r}")

    def steps_until(self, done, limit: float = 15.0) -> list:
        events, until = [], time.time() + limit
        while time.time() < until:
            events += autopilot.step(self.tasks)
            if done():
                return events
            time.sleep(0.1)
        self.fail(f"timed out: {events}")

    def status(self, cid: str) -> str:
        """A card's RESUME status; a fake session rewrites RESUME in place, so a read may catch it half written."""
        for _ in range(40):
            found = self.state()["status"].get(cid)
            if found:
                return found
            time.sleep(0.05)
        return ""

    def set_status(self, cid: str, status: str) -> None:
        autopilot.set_row(self.state(), "status", {"card": cid}, {"status": status})

    # --- AP5: a stop the session ignores ---------------------------------------------------------

    def test_a_session_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        self.script_for({"T001": ["stubborn"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        self.wait_log(self.runs()[0], '"init"')
        pid = self.runs()[0]["pid"]
        autopilot.act(self.tasks, "stop", {"card": "T001"})
        self.assertTrue(self.runs()[0].get("kill_sent_ts"), "the stop is recorded on the run")
        time.sleep(0.5)
        self.assertTrue(sv.pid_alive(pid), "the fake ignores SIGTERM")
        with mock.patch.object(autopilot, "KILL_GRACE", 1):
            events = self.steps_until(lambda: self.runs()[0]["ended"])
        self.assertFalse(sv.pid_alive(pid), "SIGKILL ended it")
        self.assertEqual(len([e for e in events if "SIGKILL" in e]), 1, events)
        self.assertEqual(len(self.launched()), 1, "a stopped card is not restarted on its own")

    def test_a_session_that_lingers_after_its_result_is_stopped_once_then_killed(self):
        autopilot.change_settings(self.tasks, result_grace_s=1, max_parallel=1)
        self.script_for({"T001": ["linger", "done"]})
        with mock.patch.object(autopilot, "KILL_GRACE", 1):
            events = self.steps_until(lambda: self.status("T001") == "done")
        said = [e for e in events if "gave its result but did not exit" in e]
        self.assertEqual(len(said), 1, events)
        first = self.runs()[0]
        self.assertTrue(first["ended"])
        self.assertFalse(sv.pid_alive(first["pid"]))

    def test_kill_trusts_its_own_child_when_ps_does_not_show_the_session(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        reg = autopilot.registry(self.tasks)
        with mock.patch.object(sv, "run_alive", return_value=False):
            self.assertTrue(autopilot.kill(reg["runs"][0]), "the Popen this process holds is that session")
        self.assertTrue(reg["runs"][0]["kill_sent_ts"])
        self.steps_until(lambda: self.runs()[0]["ended"])

    def test_run_alive_asks_ps_for_the_whole_command_line(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return mock.Mock(stdout="claude -p --session-id abc")

        with mock.patch.object(sv.subprocess, "run", side_effect=fake_run):
            self.assertTrue(sv.run_alive({"pid": os.getpid(), "session": "abc", "ended": ""}))
        self.assertIn("-ww", seen[0], "procps cuts args= at 80 columns without a TTY; the session id comes later")

    # --- AP6: a card the dispatcher stopped does not hold the integration worktree -------------------

    def serial_t002(self) -> None:
        """T002 without [P], so T002 and T004 both need the integration worktree once T001 is done."""
        text = open(self.tasks).read().replace("- [ ] T002 [P] ", "- [ ] T002 ").replace("#### T002 [P] ", "#### T002 ")
        open(self.tasks, "w").write(text)
        self.finish("T001")
        self.disjoint()

    def test_a_card_stopped_for_silence_gives_the_integration_worktree_back(self):
        self.serial_t002()
        self.script_for({"T002": ["sleep"], "T003": ["done"], "T004": ["done"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        self.assertEqual([r["card"] for r in autopilot.live(autopilot.registry(self.tasks)) if r["card"] != "T003"],
                         ["T002"], "one card without [P] at a time")
        self.set_status("T002", "doing")  # as its session does at the start
        autopilot.change_settings(self.tasks, quiet_minutes=0.02)
        events = self.steps_until(lambda: "T004" in [c["card"] for c in self.launched()])
        self.assertIn("T002: stopped its session (silent for 0 min)", events)
        self.assertIn("T002", self.state()["autopilot"]["attention"])
        self.assertEqual(self.status("T002"), "todo", "like the owner's Stop, the card leaves `doing`")

    def test_a_silent_session_that_lingers_keeps_its_card_until_it_ends(self):
        self.serial_t002()
        self.script_for({"T002": ["stubborn"], "T003": ["done"], "T004": ["done"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        t002 = next(r for r in self.runs() if r["card"] == "T002")
        self.wait_log(t002, '"init"')
        self.set_status("T002", "doing")
        autopilot.change_settings(self.tasks, quiet_minutes=0.02)
        self.steps_until(lambda: "T002" in autopilot.registry(self.tasks)["attention"])
        for _ in range(5):  # it ignores SIGTERM: while it lives, it may still write RESUME
            autopilot.step(self.tasks)
            time.sleep(0.1)
        self.assertEqual(self.status("T002"), "doing")
        self.assertNotIn("T004", [c["card"] for c in self.launched()])
        with mock.patch.object(autopilot, "KILL_GRACE", 1):
            self.steps_until(lambda: "T004" in [c["card"] for c in self.launched()])
        self.assertFalse(sv.pid_alive(t002["pid"]))
        self.assertEqual(self.status("T002"), "todo")

    # --- AP10: a Chrome check that died ---------------------------------------------------------

    def test_a_chrome_check_that_died_is_run_again(self):
        autopilot.change_settings(self.tasks, chrome=True)
        reg = autopilot.registry(self.tasks)
        reg["chrome_check"] = {"running": True, "ts": time.time() - 400, "account": ""}  # its thread is gone
        s, cfg = self.state(), autopilot.settings(self.tasks)
        self.assertEqual(autopilot.room(s, reg, cfg, "T001"), "", "a stale check holds no session back")
        with mock.patch.object(autopilot, "probe_chrome") as probe:
            self.assertEqual(autopilot.chrome_problem(self.tasks, s, reg, cfg),
                             "checking that the sessions' Chrome reaches the app signed in")
            for _ in range(50):
                if probe.called:
                    break
                time.sleep(0.02)
        self.assertTrue(probe.called, "the check starts again")
        self.assertGreater(reg["chrome_check"]["ts"], time.time() - 60)
        reg["chrome_check"]["ts"] = time.time()  # a check running now still holds them
        self.assertEqual(autopilot.room(s, reg, cfg, "T001"), "checking that the sessions' Chrome reaches the app signed in")

    def test_a_chrome_check_with_an_unexpected_answer_records_its_failure(self):
        os.environ["FAKE_CHROME"] = "list"
        try:
            autopilot.change_settings(self.tasks, chrome=True)
            autopilot.probe_chrome(self.tasks, self.state(), autopilot.settings(self.tasks), "")
        finally:
            os.environ.pop("FAKE_CHROME")
        check = autopilot.registry(self.tasks)["chrome_check"]
        self.assertEqual((check["running"], check["ok"]), (False, False))
        self.assertIn("the check could not run", check["detail"])
        self.assertIn("Chrome check failed", autopilot.settings(self.tasks)["paused_reason"])

    def test_a_chrome_check_that_was_replaced_does_not_write_its_result(self):
        autopilot.change_settings(self.tasks, chrome=True)
        reg = autopilot.registry(self.tasks)
        reg["chrome_check"] = {"running": True, "ts": 1234.5, "account": ""}  # a newer check runs now
        autopilot.save_registry(self.tasks, reg)
        autopilot.probe_chrome(self.tasks, self.state(), autopilot.settings(self.tasks), "", started=1000.0)
        self.assertEqual(autopilot.registry(self.tasks)["chrome_check"]["ts"], 1234.5)
        autopilot.probe_chrome(self.tasks, self.state(), autopilot.settings(self.tasks), "", started=1234.5)
        check = autopilot.registry(self.tasks)["chrome_check"]
        self.assertEqual((check["running"], check["ok"]), (False, True))

    # --- AP14: the merge lock of a batch session ------------------------------------------------

    def test_a_batch_session_keeps_the_merge_lock_it_took_for_any_of_its_cards(self):
        self.add_batch()
        s = self.state()
        reg = autopilot.registry(self.tasks)
        reg["runs"] = [{"card": "T003B", "batch": "B1", "session": "s1", "pid": None, "started": autopilot.stamp(),
                        "ended": "", "result": "", "error": "", "log": "runs/B1-1.jsonl"}]
        lock = autopilot.merge_lock_path(self.tasks)
        os.makedirs(lock)
        open(os.path.join(lock, "holder"), "w").write("T003C 2026-10-09T10:00Z\n")  # its second card merges
        self.assertEqual(autopilot.clear_merge_lock(self.tasks, s, reg), "")
        self.assertTrue(os.path.isdir(lock), "the lock stays while its batch's session runs")
        self.set_status("T003C", "doing")
        reg["runs"][0]["ended"] = autopilot.stamp()  # it died mid-merge, its card still `doing`
        self.assertIn("cleared the merge lock T003C held", autopilot.clear_merge_lock(self.tasks, self.state(), reg))
        self.assertFalse(os.path.isdir(lock))

    # --- AP18: log lines that are not stream-json objects ------------------------------------------

    def test_malformed_log_lines_do_not_break_a_pass(self):
        self.script_for({"T001": ["garbage"]})
        autopilot.change_settings(self.tasks, max_parallel=1)
        self.steps_until(lambda: self.status("T001") == "done" and not autopilot.live(
            autopilot.registry(self.tasks)))
        view = autopilot.live_view(self.tasks, "T001")
        self.assertEqual(view["todos"], [], "the TodoWrite replaced what TaskCreate added")
        self.assertEqual(view["final"]["error"], False)

    def test_task_ids_land_on_the_todo_their_task_create_added(self):
        view = {"events": [], "dropped": 0, "tools": 0, "output_tokens": 0, "context": 0, "pending": {},
                "todos": [], "task_ids": {}, "said": "", "final": None}

        def tool(tid, name, arg):
            autopilot.absorb(view, {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": name, "input": arg}]}})

        def result(tid, text):
            autopilot.absorb(view, {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text}]}})

        tool("tc1", "TaskCreate", {"subject": "Gone soon"})
        tool("tw1", "TodoWrite", {"todos": [{"content": c} for c in ("a", "b", "c")]})
        result("tc1", "Task #1 created")  # its to-do was replaced: nothing to label
        tool("tc2", "TaskCreate", {"subject": "Write the test"})
        result("tc2", "Task #7 created")
        self.assertEqual([t.get("id", "") for t in view["todos"]], ["", "", "", "7"])
        tool("tu1", "TaskUpdate", {"taskId": "7", "status": "completed"})
        self.assertEqual(view["todos"][3]["status"], "completed")

    # --- AP17: the dispatcher thread outlives a failing pass ---------------------------------------

    def test_the_dispatcher_loop_survives_failing_features_and_output(self):
        calls = {"features": 0, "step": 0}

        def features():
            calls["features"] += 1
            if calls["features"] == 1:
                raise RuntimeError("tasks.md could not be read")
            return [self.tasks]

        def step(tasks):
            calls["step"] += 1
            if calls["step"] % 2:
                return ["T001: start · effort high"]
            raise RuntimeError("boom")

        def say(line):
            raise BrokenPipeError("the terminal went away")

        stop = threading.Event()
        with mock.patch.object(autopilot, "step", side_effect=step):
            worker = threading.Thread(target=autopilot.loop, args=(features, 0.01, stop, say), daemon=True)
            worker.start()
            for _ in range(300):
                if calls["step"] >= 4:
                    break
                time.sleep(0.01)
            alive = worker.is_alive()
            stop.set()
            worker.join(5)
        self.assertTrue(alive, "the loop keeps running")
        self.assertGreaterEqual(calls["step"], 4)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
