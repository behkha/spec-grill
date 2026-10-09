"""Tests for the integration of the review-fix branches: how their changes meet in one dispatcher, and the
follow-ups found while merging them.

    python3 -m pytest -q tests/test_integration_followups.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
import test_autopilot as base  # noqa: E402  (a module, so its Feature tests are not collected here again)


class Scratch(unittest.TestCase):
    """A feature on the fake `claude`, built as test_autopilot.Feature builds it: its setUp and helpers,
    without its tests. tearDown also SIGKILLs what a session left behind."""

    def tearDown(self):
        for run in autopilot.registry(self.tasks)["runs"]:
            if not run.get("ended") and run.get("pid"):
                with contextlib.suppress(OSError):
                    os.killpg(int(run["pid"]), signal.SIGKILL)
        for pid in self.children():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        autopilot.release(self.tasks)
        shutil.rmtree(self.root, ignore_errors=True)

    def children(self) -> list:
        """The pids fake_claude's "orphan" sessions left running (FAKE_CHILDREN)."""
        path = os.path.join(self.root, "children")
        return [int(x) for x in open(path).read().split()] if os.path.exists(path) else []

    def runs(self) -> list:
        return autopilot.registry(self.tasks)["runs"]

    def launched_cards(self) -> list:
        return [c["card"] for c in self.launched()]

    def steps_until(self, done, limit: float = 20.0) -> list:
        events, until = [], time.time() + limit
        while time.time() < until:
            events += autopilot.step(self.tasks)
            if done():
                return events
            time.sleep(0.1)
        self.fail(f"timed out: {events}")

    def serial_t002(self) -> None:
        """T002 without [P]: once T001 is done, T002 and T004 both need the integration worktree."""
        text = open(self.tasks).read().replace("- [ ] T002 [P] ", "- [ ] T002 ").replace("#### T002 [P] ", "#### T002 ")
        with open(self.tasks, "w") as handle:
            handle.write(text)
        self.finish("T001")
        self.disjoint()

    def status(self, cid: str) -> str:
        for _ in range(40):  # a session may be replacing RESUME while it is read
            found = self.state()["status"].get(cid)
            if found:
                return found
            time.sleep(0.05)
        return ""


for _name, _value in vars(base.Feature).items():
    if not _name.startswith(("test", "__")) and _name != "tearDown":
        setattr(Scratch, _name, _value)


def ended(proc, limit: float = 15.0) -> None:
    """Wait for a session this process started to exit."""
    until = time.time() + limit
    while proc.poll() is None and time.time() < until:
        time.sleep(0.05)


class Merged(Scratch):
    """Where the branches' changes meet."""

    def test_sessions_and_the_account_check_inherit_only_the_confirmed_keep_env(self):
        # #4's session_env() reads #12's confirmed settings: a keep_env a session wrote never reaches a
        # session or `auth status` before the owner accepts it
        envs = os.path.join(self.root, "env.jsonl")
        with mock.patch.dict(os.environ, {"FAKE_ENV": envs, "ANTHROPIC_API_KEY": "sk-test"}):
            s = self.state()
            autopilot.trusted(self.tasks, autopilot.settings(self.tasks), s)  # what this dispatcher confirmed
            path = os.path.join(autopilot.state_dir(self.tasks), "autopilot.json")
            with open(path) as handle:
                cfg = json.load(handle)
            with open(path, "w") as handle:  # what a session could write
                json.dump({**cfg, "keep_env": ["ANTHROPIC_API_KEY"]}, handle)
            cfg = autopilot.settings(self.tasks)
            self.assertIn("keep_env added 'ANTHROPIC_API_KEY'", "; ".join(autopilot.widened(self.tasks, s, cfg)))
            self.assertNotIn("keep_env", autopilot.confirmed_settings(self.tasks, cfg, s))
            reg = autopilot.registry(self.tasks)
            autopilot.account_problem(s, reg, cfg, 0)
            autopilot.spawn(self.tasks, s, reg, "T001", "start", autopilot.start_line(s, "T001"))
            autopilot.save_registry(self.tasks, reg)
            self.wait_calls(1)
        calls = [json.loads(line) for line in open(envs)]
        self.assertEqual([c["args"][:2] if c["card"] is None else c["card"] for c in calls], [["auth", "status"], "T001"])
        for call in calls:
            self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        ended(autopilot.PROCS[reg["runs"][-1]["session"]])

    def test_a_split_that_ends_on_a_pass_that_waits_is_judged_on_the_next_whole_pass(self):
        # #9's partial-read guard returns before #10's judge(): the run stays "unsettled" and is judged later
        self.script_for({"T001": ["splitopen"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        ended(autopilot.PROCS[self.runs()[0]["session"]])
        real, seen = autopilot.whole, []

        def first_read_partial(*args):
            seen.append(1)
            return False if len(seen) == 1 else real(*args)

        with mock.patch.object(autopilot, "whole", first_read_partial):
            autopilot.step(self.tasks)
        run = self.runs()[0]
        self.assertTrue(run["ended"])
        self.assertTrue(run.get("unsettled"), "not judged on a read it does not trust")
        self.assertEqual(self.status("T001"), "doing")
        autopilot.step(self.tasks)
        self.assertEqual(self.status("T001"), "done", "split with its hand-off written: marked done")
        self.assertNotIn("unsettled", self.runs()[0])
        self.assertNotIn("T001", autopilot.registry(self.tasks)["attention"])
        self.settle()


class GiveUp(Scratch):
    """Item 1: a unit the dispatcher hands to the owner after its attempts no longer holds the worktree."""

    def test_a_serial_card_that_runs_out_of_attempts_lets_the_next_serial_card_start(self):
        self.serial_t002()
        self.script_for({"T002": ["doing"], "T003": ["done"], "T004": ["done"]})  # T002 never finishes
        events = self.steps_until(lambda: "T004" in self.launched_cards())
        self.assertEqual(self.launched_cards().count("T002"), 2, "max_attempts sessions, then the owner")
        self.assertIn("2 sessions ended without finishing it", autopilot.registry(self.tasks)["attention"]["T002"])
        self.assertEqual(self.status("T002"), "todo", "given up on: it leaves `doing`")
        self.assertTrue(all(r["ended"] for r in self.runs() if r["card"] == "T002"), "freed only once reaped")
        self.assertIn("T004: start · effort medium", " ".join(events))
        self.settle()
        self.assertEqual(self.status("T004"), "done")
        self.assertEqual(self.status("T002"), "todo", "still the owner's: only Retry starts it again")

    def test_unbacked_waits_that_run_out_of_attempts_free_the_card_too(self):
        self.serial_t002()
        self.script_for({"T002": ["claimapproval"], "T003": ["done"], "T004": ["done"]})  # says it waits; no row
        self.steps_until(lambda: "T004" in self.launched_cards())
        why = autopilot.registry(self.tasks)["attention"]["T002"]
        self.assertIn("RESUME had no pending approval or open decision", why)
        self.assertEqual(self.status("T002"), "todo")
        self.settle()

    def test_the_owners_retry_after_a_give_up_goes_on_with_the_card(self):
        self.serial_t002()
        self.script_for({"T002": ["doing", "doing", "done"], "T003": ["done"], "T004": ["done"]})
        self.steps_until(lambda: "T004" in self.launched_cards())
        self.settle()
        self.assertEqual(autopilot.act(self.tasks, "retry", {"card": "T002"}), "T002 started")
        self.settle()
        self.assertEqual(self.status("T002"), "done")
        self.assertNotIn("T002", autopilot.registry(self.tasks)["attention"])


def alive(pid: int) -> bool:
    """A process that exists and is not a zombie."""
    if not sv.pid_alive(pid):
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class Leftovers(Scratch):
    """Item 2: what a session leaves running in its process group is stopped once the session is reaped."""

    def setUp(self):
        base.Feature.setUp(self)
        environ = mock.patch.dict(os.environ, {"FAKE_CHILDREN": os.path.join(self.root, "children")})
        environ.start()
        self.addCleanup(environ.stop)

    def child_of_t001(self) -> int:
        """Run T001's session to its end (not reaped yet) and return the child it left."""
        autopilot.step(self.tasks)
        self.wait_calls(1)
        run = self.runs()[0]
        self.assertEqual((run["card"], run["pgid"]), ("T001", run["pid"]), "the session leads its own group")
        for _ in range(150):
            if self.children():
                break
            time.sleep(0.1)
        ended(autopilot.PROCS[run["session"]])
        child = self.children()[0]
        self.assertTrue(alive(child), "the child outlives its session")
        self.assertEqual(os.getpgid(child), run["pgid"], "in the session's process group")
        return child

    def test_a_child_left_in_the_sessions_group_is_stopped_when_the_session_is_reaped(self):
        self.script_for({"T001": ["orphan"], "T002": ["done"], "T003": ["done"], "T004": ["done"]})
        child = self.child_of_t001()
        events = autopilot.step(self.tasks)
        self.assertIn("T001: stopped what its session left running in its process group", events)
        for _ in range(100):
            if not alive(child):
                break
            time.sleep(0.05)
        self.assertFalse(alive(child), "SIGTERM to the group stopped it")
        self.settle()

    def test_a_child_that_ignores_sigterm_gets_sigkill_on_a_later_pass(self):
        self.script_for({"T001": ["orphan-stubborn"], "T002": ["done"], "T003": ["done"], "T004": ["done"]})
        child = self.child_of_t001()
        with mock.patch.object(autopilot, "GROUP_GRACE", 0.5):
            first = autopilot.step(self.tasks)
            self.assertIn("T001: stopped what its session left running in its process group", first)
            time.sleep(0.2)
            self.assertTrue(alive(child), "it ignores SIGTERM; the pass did not wait for it")
            self.assertTrue(self.runs()[0].get("leftovers_ts"))
            events = self.steps_until(lambda: not alive(child))
        self.assertIn("T001: what its session left running outlived SIGTERM; sent it SIGKILL", events)
        self.assertNotIn("leftovers_ts", self.runs()[0], "one SIGKILL, then it is done with the group")
        self.settle()

    def test_a_group_whose_id_is_in_use_or_out_of_range_is_never_signalled(self):
        with mock.patch.object(autopilot.os, "killpg") as killpg:
            for run in ({"pid": os.getpid()},  # a live process holds the id: it is not the ended session's
                        {"pgid": os.getpgrp(), "pid": 99999999}, {"pid": 0}, {"pid": 1}, {"pid": -5},
                        {"pid": 2 ** 40}, {"pid": "x"}, {"pid": None}, {}):
                self.assertFalse(autopilot.signal_leftovers(run, signal.SIGTERM), run)
            killpg.assert_not_called()
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.assertFalse(autopilot.signal_leftovers({"pid": gone.pid}, signal.SIGTERM), "nothing is left of it")

    def test_a_sigkill_long_overdue_is_dropped(self):
        reg = {"runs": [{"card": "T001", "ended": "x", "pid": 4242, "leftovers_ts": 1000.0}]}
        with mock.patch.object(autopilot, "signal_leftovers", return_value=True) as signal_it:
            self.assertEqual(autopilot.stop_leftovers(reg, 1000.0 + autopilot.GROUP_GRACE + autopilot.GROUP_STALE + 1), [])
            signal_it.assert_not_called()
        self.assertNotIn("leftovers_ts", reg["runs"][0])


class HugeNumbers(Scratch):
    """Item 5a: a number too large for a float, or none at all, is refused or paused on, never a crash."""

    def test_the_dashboard_cannot_save_a_number_a_float_cannot_hold(self):
        for value in (10 ** 400, float("inf"), float("-inf"), float("nan"), "9" * 400, [], None, "12x"):
            with self.subTest(value=repr(value)[:20]):
                with self.assertRaises(autopilot.Refused) as caught:
                    autopilot.act(self.tasks, "settings", {"max_parallel": value})
                self.assertEqual(str(caught.exception), "max_parallel must be a whole number")
        self.assertEqual(autopilot.act(self.tasks, "settings", {"max_parallel": "4", "budget_total_usd": 1e300}),
                         "settings saved")
        self.assertEqual(autopilot.settings(self.tasks)["max_parallel"], 4)

    def test_a_huge_or_non_numeric_setting_in_the_file_pauses_the_autopilot(self):
        self.assertIsNone(autopilot.amount(10 ** 400))
        path = os.path.join(autopilot.state_dir(self.tasks), "autopilot.json")
        for key, value in (("max_parallel", 10 ** 400), ("quiet_minutes", "forty"), ("max_run_hours", None)):
            with self.subTest(key=key):
                autopilot.change_settings(self.tasks, auto=True, paused_reason="")
                with open(path) as handle:
                    cfg = json.load(handle)
                with open(path, "w") as handle:  # by hand, or by a session
                    json.dump({**cfg, key: value}, handle)
                events = autopilot.step(self.tasks)  # no exception: the stop checks use the default meanwhile
                said = autopilot.settings(self.tasks)
                self.assertFalse(said["auto"])
                self.assertIn(f"sets {key} to", said["paused_reason"])
                self.assertTrue(any("autopilot paused" in e for e in events), events)
                with open(path, "w") as handle:
                    json.dump(cfg, handle)
        self.settle()

    def test_the_stop_checks_of_a_live_session_survive_a_broken_setting(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        path = os.path.join(autopilot.state_dir(self.tasks), "autopilot.json")
        with open(path) as handle:
            cfg = json.load(handle)
        with open(path, "w") as handle:
            json.dump({**cfg, "quiet_minutes": "forty", "max_run_hours": 10 ** 400, "result_grace_s": []}, handle)
        autopilot.step(self.tasks)  # no TypeError or OverflowError from the silence and run-time checks
        self.assertFalse(autopilot.settings(self.tasks)["auto"])
        self.assertEqual(len(autopilot.live(autopilot.registry(self.tasks))), 1, "the session runs on")


HAND_EDITED = {  # runs.json after a careless hand edit
    "runs": [1, "x", None,
             {"card": "T001", "session": "s1", "cost": "n/a", "started": "2026-01-01 00:00Z", "ended": "2026-01-01 00:10Z",
              "log": "runs/T001-1.jsonl", "result": None, "error": 7, "slot": "x"},
             {"card": "T001", "session": "s1", "cost": float("nan"), "ended": "2026-01-01 00:20Z", "started_ts": "soon"},
             {"card": "T003", "session": "s2", "cost": float("inf"), "ended": True},
             {"card": "T004", "session": "s3", "cost": 1.5, "ended": "2026-01-01 00:30Z", "kill_sent_ts": [], "slot": 2.0},
             {"session": "s4", "cost": 10 ** 400, "ended": "2026-01-01 00:40Z"}],
    "attention": ["T001"], "queued": "T001", "manual": [{"card": "T002"}, "T005"], "handled": None,
    "granted": {"T001": "x", "T002": -3, "T003": 2}, "blocked_on": {"T001": "x", "T002": ["a", 1]},
    "notified": {}, "account": [], "chrome_check": "running",
}


class HandEditedRegistry(Scratch):
    """Item 5b: the dispatcher reads runs.json through the normalizer build() uses (sv.registry_of)."""

    def write_registry(self, data) -> None:
        with open(os.path.join(autopilot.state_dir(self.tasks), "runs.json"), "w") as handle:
            json.dump(data, handle)  # NaN and Infinity as Python's json writes them

    def test_the_dispatcher_and_the_report_read_one_normalized_registry(self):
        self.write_registry(HAND_EDITED)
        reg = autopilot.registry(self.tasks)
        self.assertEqual([r["card"] for r in reg["runs"]], ["T001", "T001", "T003", "T004", ""])
        self.assertEqual([r["cost"] for r in reg["runs"]], [0.0, 0.0, 0.0, 1.5, 0.0])
        first = reg["runs"][0]
        self.assertEqual((first["result"], first["error"], first["slot"], first["batch"]), ("", "7", 0, ""))
        self.assertEqual(reg["runs"][1]["started_ts"], 0.0)
        self.assertEqual((reg["runs"][3]["kill_sent_ts"], reg["runs"][3]["slot"]), (0.0, 2))
        self.assertEqual((reg["attention"], reg["queued"], reg["manual"], reg["handled"], reg["notified"]),
                         ({}, [], ["T005"], [], []))
        self.assertEqual(reg["granted"], {"T001": 0, "T002": 0, "T003": 2})
        self.assertEqual(reg["blocked_on"], {"T001": [], "T002": ["a", "1"]})
        self.assertEqual((reg["account"], reg["chrome_check"]), (None, None))
        self.assertEqual(autopilot.spent(reg), 1.5)
        s = self.state()
        self.assertEqual(s["autopilot"]["spent_usd"], 1.5)
        json.dumps(s, allow_nan=False)  # the dashboard's JSON.parse takes it
        self.assertEqual(json.loads(json.dumps(sv.registry_of(json.loads(json.dumps(HAND_EDITED))))), reg)

    def test_a_pass_over_a_hand_edited_registry_runs(self):
        self.write_registry(HAND_EDITED)
        autopilot.step(self.tasks)  # no KeyError, TypeError or ValueError on the odd entries
        # "blocked_on": {"T001": "x"} reads as a blocker it did not name: it waits for the owner's Unblock
        self.assertEqual(autopilot.registry(self.tasks)["blocked_on"].get("T001"), [])
        self.assertEqual(self.launched_cards(), [])
        self.assertIn("T001 unblocked", autopilot.act(self.tasks, "unblock", {"card": "T001"}))
        self.settle()
        self.assertEqual(self.launched_cards()[0], "T001")
        self.assertEqual(self.status("T001"), "done")

    def test_a_live_run_with_odd_fields_is_watched_not_tripped_over(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        reg = autopilot.registry(self.tasks)
        reg["runs"][0].update(started="yesterday", started_ts=None, slot="first", cost="n/a")
        autopilot.save_registry(self.tasks, reg)
        autopilot.step(self.tasks)
        view = autopilot.live_view(self.tasks, "T001", events=False)
        self.assertTrue(view["run"]["live"])
        self.assertEqual(autopilot.port_slot(self.state(), autopilot.registry(self.tasks), "T002"), 1)

    def test_a_result_line_that_is_not_an_object_is_no_result(self):
        log = os.path.join(self.root, "x.jsonl")
        with open(log, "w") as handle:
            handle.write('["\\"type\\":\\"result\\""]\n{"type": "result", "result": "ok", "total_cost_usd": "n/a"}\n')
        found = autopilot.result_of(log)
        self.assertEqual(found["result"]["result"], "ok")
        with open(log, "w") as handle:
            handle.write('["\\"type\\":\\"result\\""]\n')
        self.assertIsNone(autopilot.result_of(log)["result"])


class Lessons(Scratch):
    """Item 5d: --lessons reads costs and times as safely as the report does."""

    def test_lessons_take_odd_costs_as_zero(self):
        runs = [{"card": "T001", "session": "a", "cost": "n/a", "started": "2026-01-01 00:00Z", "ended": "2026-01-01 01:00Z",
                 "reason": "start"},
                {"card": "T002", "session": "b", "cost": float("nan"), "started": "2026-01-01 00:00Z",
                 "started_ts": float("inf"), "ended": "2026-01-01 02:00Z", "reason": "start"},
                {"card": "T003", "session": "c", "cost": float("inf"), "started": "x", "ended": "2026-01-01 00:30Z"},
                {"card": "T004", "session": "d", "cost": 10 ** 400, "started_ts": "soon", "started": "2026-01-01 00:00Z",
                 "ended": "2026-01-01 00:30Z"},
                {"card": "T004", "session": "e", "cost": 1.25, "started": "2026-01-01 01:00Z", "ended": "2026-01-01 01:30Z"},
                "not a run"]
        with open(os.path.join(autopilot.state_dir(self.tasks), "runs.json"), "w") as handle:
            json.dump({"runs": runs, "attention": "x"}, handle)
        text = sv.lessons(self.state())
        rows = {line.split(" | ")[0][2:]: line.split(" | ") for line in text.splitlines() if line.startswith("| T00")}
        self.assertEqual([rows[c][8] for c in ("T001", "T002", "T003", "T004")], ["0.0", "0.0", "0.0", "1.25"])
        self.assertEqual([rows[c][9] for c in ("T001", "T002", "T003", "T004")], ["1.0", "2.0", "0.0", "1.0"])
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("inf", text.lower())


class OneWriter(Scratch):
    """Item 5e: one atomic writer (supervisor.save) for the supervisor's and the autopilot's state files."""

    def test_the_autopilot_writes_its_files_with_the_supervisors_writer(self):
        self.assertFalse(hasattr(autopilot, "save_json"), "no second copy of the writer")
        with mock.patch.object(sv, "save", wraps=sv.save) as save:
            autopilot.change_settings(self.tasks, notify=True)
            autopilot.save_registry(self.tasks, autopilot.registry(self.tasks))
        written = [(os.path.basename(c.args[0]), c.kwargs.get("backup", False)) for c in save.call_args_list]
        self.assertEqual(written, [("autopilot.json", False), ("runs.json", True)])

    def test_every_write_is_flushed_to_disk_and_renamed_whole(self):
        path = os.path.join(self.root, "state", "x.json")
        with mock.patch.object(sv.os, "fsync", wraps=os.fsync) as fsync:
            sv.save(path, {"a": "é"})
        self.assertEqual(fsync.call_count, 2, "the file, then its folder (the rename)")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"a": "é"})
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["x.json"])

    def test_runs_json_keeps_its_last_good_copy(self):
        path = os.path.join(self.root, "state", "runs.json")
        sv.save(path, {"runs": [1]}, backup=True)
        self.assertFalse(os.path.exists(path + ".bak"), "nothing to keep yet")
        sv.save(path, {"runs": [2]}, backup=True)
        self.assertEqual(sv.read_json(path + ".bak", None), {"runs": [1]})
        with open(path, "w") as handle:
            handle.write("{half")
        sv.save(path, {"runs": [3]}, backup=True)
        self.assertEqual(sv.read_json(path + ".bak", None), {"runs": [1]}, "a corrupt file never replaces the .bak")
        self.assertEqual(sv.read_json(path, None), {"runs": [3]})
        with mock.patch.object(sv.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sv.save(path, {"runs": [4]}, backup=True)
        self.assertEqual(sv.read_json(path, None), {"runs": [3]})
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["runs.json", "runs.json.bak"], "no temporary file left")


class Pids(unittest.TestCase):
    """Item 5c: a pid of another account is alive; one no pid_t holds is no pid, never an OverflowError."""

    def test_a_process_of_another_account_is_alive(self):
        self.assertTrue(sv.pid_alive(os.getpid()))
        if os.getuid() != 0:
            self.assertTrue(sv.pid_alive(1), "pid 1 runs as root: os.kill answers EPERM")
        with mock.patch.object(sv.os, "kill", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertTrue(sv.pid_alive(4242))
        with mock.patch.object(sv.os, "kill", side_effect=ProcessLookupError(3, "No such process")):
            self.assertFalse(sv.pid_alive(4242))

    def test_what_is_no_pid_is_not_alive(self):
        with mock.patch.object(sv.os, "kill") as kill:
            for pid in (2 ** 31, 2 ** 40, 10 ** 400, 0, -1, True, None, "x", "", [], float("inf"), float("nan")):
                self.assertFalse(sv.pid_alive(pid), repr(pid))
            kill.assert_not_called()  # 0 and -1 would signal a whole process group, or everything
        self.assertTrue(sv.pid_alive(str(os.getpid())), "a pid written as text")

    def test_a_run_with_a_pid_past_pid_t_is_not_alive_and_is_never_signalled(self):
        for pid in (2 ** 40, 10 ** 400):
            run = {"pid": pid, "session": "abc", "ended": ""}
            self.assertFalse(sv.run_alive(run))
            self.assertFalse(autopilot.kill(run))
            self.assertFalse(autopilot.kill(run, signal.SIGKILL))


def doc(name: str) -> str:
    with open(os.path.join(HERE, "..", name), encoding="utf-8") as handle:
        return re.sub(r"\s+", " ", handle.read())


class Docs(unittest.TestCase):
    """Items 5f and 5g: the README and SKILL.md say what the merged code does."""

    def test_the_dashboard_opens_from_the_keyed_link_not_the_bare_url(self):
        readme, skill = doc("README.md"), doc("SKILL.md")
        self.assertNotIn("It opens `http://127.0.0.1:8765`", readme)
        self.assertIn("`http://127.0.0.1:8765/?k=…`", readme)
        self.assertIn("`--open` opens that link", readme)
        self.assertIn("The bare `http://127.0.0.1:8765` shows only a locked page", readme)
        self.assertIn("opened from the link it prints", skill)
        self.assertNotIn("open its URL", skill)
        self.assertIn("the bare URL shows a locked page", skill)


if __name__ == "__main__":
    unittest.main()
