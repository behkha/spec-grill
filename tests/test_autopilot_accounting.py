"""How the autopilot counts and classifies its sessions: claimed waits, failed answers, errors read from
the CLI's fields, a retried capped card, a session resumed by hand, and AUTOPILOT: SPLIT.

    python3 -m pytest -q tests/test_autopilot_accounting.py
"""

import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
import test_autopilot as base  # noqa: E402  (not `from … import Feature`: its tests would run twice)

HELPERS = ("setUp", "tearDown", "script_for", "launched", "wait_calls", "settle", "state", "resume_text",
           "add_to_tasks", "add_batch", "batch")


class Accounting(unittest.TestCase):
    BATCH = base.Feature.BATCH

    def runs(self, cid: str) -> list:
        return [c for c in self.launched() if c["card"] == cid]

    def bounded(self, passes: int = 40) -> None:
        """Run passes until nothing is live and a pass changes nothing; fail (a loop) when it never settles."""
        for _ in range(passes):
            got = autopilot.step(self.tasks)
            if not autopilot.live(autopilot.registry(self.tasks)) and not got:
                return
            time.sleep(0.15)
        self.fail(f"never settled: {len(self.launched())} sessions started")

    # AP3: a session that says it waits, with nothing in RESUME to wait for, is a try

    def test_a_claimed_approval_with_no_row_counts_as_a_try(self):
        self.script_for({"T001": ["claimapproval"]})
        self.bounded()
        self.assertEqual(len(self.runs("T001")), 2, "max_attempts sessions, then the owner")
        why = self.state()["autopilot"]["attention"]["T001"]
        self.assertIn("2 sessions ended without finishing it", why)
        self.assertIn("no pending approval or open decision", why)
        self.assertTrue(all(r.get("unbacked") for r in autopilot.registry(self.tasks)["runs"]))

    def test_a_claimed_decision_with_no_row_counts_as_a_try(self):
        self.script_for({"T001": ["claimdecision"]})
        self.bounded()
        self.assertEqual(len(self.runs("T001")), 2)
        self.assertIn("T001", self.state()["autopilot"]["attention"])

    def test_a_wait_backed_by_resume_is_still_free(self):
        reg = {"runs": [{"result": "AUTOPILOT: WAITING FOR DECISION 2", "reason": "start", "ended": "x"}]}
        self.assertFalse(sv.is_try(reg["runs"][0]), "a wait RESUME backs is not a try")
        self.assertTrue(sv.is_try({**reg["runs"][0], "unbacked": True}))
        self.assertFalse(sv.is_try({"result": "AUTOPILOT: BLOCKED", "reason": "start"}))

    # U11-1: the end line names the approval by its `<card>.<n>` id, and such a wait is backed by its row

    def test_an_approval_named_card_dot_n_is_a_backed_wait(self):
        self.assertIn("`AUTOPILOT: WAITING FOR APPROVAL <card>.<n>`", autopilot.RULES)
        self.assertNotIn("A<n>", autopilot.RULES)
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"T001": ["waitapproval", "done"]})
        self.bounded()
        run = autopilot.registry(self.tasks)["runs"][0]
        self.assertFalse(run.get("unbacked"))
        self.assertFalse(sv.is_try(run))
        s = self.state()
        self.assertEqual(s["autopilot"]["attention"], {})
        self.assertEqual([(a["n"], a["card"]) for a in s["approvals"]], [("T001.1", "T001")])
        autopilot.act(self.tasks, "approval", {"n": "T001.1", "card": "T001", "verdict": "approved"})
        self.bounded()
        self.assertEqual(self.state()["status"]["T001"], "done")

    # U11-2: every session takes the merge lock at its absolute path; U11-3: a [P] merge backs off

    def test_the_merge_lock_is_named_by_its_absolute_path(self):
        self.script_for({"T001": ["done"]})
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        rules = args[args.index("--append-system-prompt") + 1]
        lock = autopilot.merge_lock_path(self.tasks)
        self.assertTrue(os.path.isabs(lock))
        self.assertIn(f"`mkdir {lock}`", rules)
        self.assertIn(f"`rm -rf {lock}`", rules)
        self.assertNotIn("mkdir state/merge.lock", rules)
        self.assertIn("`git status --short --untracked-files=no` in the integration\n  worktree",
                      autopilot.PARALLEL_RULES)
        self.assertIn("back off", autopilot.PARALLEL_RULES)

    # AP4: an answer that never reached a working session is re-sent only after an API error

    def test_a_resume_that_cannot_start_does_not_respawn_forever(self):
        self.script_for({"T001": ["approval", "noconversation"]})
        self.bounded()
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "approved"})
        self.bounded()
        runs = autopilot.registry(self.tasks)["runs"]
        self.assertEqual([r["reason"] for r in runs], ["start", "answer A1", "answer A1"],
                         "the answer is sent again at most max_attempts times")
        self.assertIn("No conversation found", runs[1]["error"], "the CLI's own words say why")
        self.assertFalse(runs[1].get("api_error"))
        self.assertTrue(sv.is_try(runs[1]), "an answer that did no work is a try")
        s = self.state()
        self.assertIn("could not take your answer to A1", s["autopilot"]["attention"]["T001"])
        self.assertTrue(s["autopilot"]["settings"]["auto"])

    def test_an_answer_run_that_worked_is_not_a_try(self):
        run = {"reason": "answer A1", "ended": "x", "worked": True, "result": "AUTOPILOT: DOING"}
        self.assertFalse(sv.is_try(run))
        self.assertTrue(sv.is_try({**run, "worked": False}))
        self.assertFalse(sv.is_try({**run, "worked": False, "ended": ""}), "a live run is not judged yet")

    # AP9: errors are classified from the CLI's fields and its own words, never the session's prose

    def test_a_card_about_a_login_page_is_not_an_api_error(self):
        self.script_for({"T001": ["loginfail"]})
        self.bounded()
        s = self.state()
        self.assertTrue(s["autopilot"]["settings"]["auto"], s["autopilot"]["settings"]["paused_reason"])
        runs = autopilot.registry(self.tasks)["runs"]
        self.assertEqual(len(runs), 2, "two failed tries")
        self.assertFalse(any(r.get("api_error") or r.get("capped") for r in runs))
        why = s["autopilot"]["attention"]["T001"]
        self.assertIn("2 sessions ended without finishing it", why)
        self.assertNotIn("cap", why.split(";")[0])

    def test_the_cli_refusing_to_log_in_still_pauses(self):
        self.script_for({"T001": ["noauth", "done"]})
        self.bounded()
        s = self.state()
        self.assertFalse(s["autopilot"]["settings"]["auto"])
        self.assertIn("Please run /login", s["autopilot"]["settings"]["paused_reason"])
        self.assertEqual(s["autopilot"]["attention"], {})

    def test_the_budget_cap_is_read_from_the_result_subtype(self):
        self.script_for({"T001": ["budget"]})
        self.bounded()
        run = autopilot.registry(self.tasks)["runs"][0]
        self.assertTrue(run["capped"])
        self.assertIn("cap", self.state()["autopilot"]["attention"]["T001"])
        old = {"error": "max_budget: Reached the budget"}  # a run an older dispatcher recorded
        self.assertTrue(autopilot.hit_cap(old))
        self.assertFalse(autopilot.hit_cap({"error": "error_during_execution: Context budget is fine"}))

    # AP11: Retry on a capped card that has to wait for a slot is not undone

    def test_a_queued_retry_of_a_capped_card_starts(self):
        self.script_for({"T001": ["budget", "done"]})
        self.bounded()
        self.assertIn("cap", self.state()["autopilot"]["attention"]["T001"])
        autopilot.change_settings(self.tasks, max_parallel=1)
        blocker = base.uuid.uuid4().hex
        proc = base.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", blocker])
        reg = autopilot.registry(self.tasks)
        reg["runs"].append({"card": "T002", "session": blocker, "attempt": 1, "reason": "start", "pid": proc.pid,
                            "started": autopilot.stamp(), "ended": "", "exit": None, "cost": 0, "result": "",
                            "error": "", "log": "runs/none.jsonl"})
        autopilot.save_registry(self.tasks, reg)
        self.assertIn("queued", autopilot.act(self.tasks, "retry", {"card": "T001"}))
        for _ in range(3):  # passes while the slot is taken must not take the retry back
            autopilot.step(self.tasks)
        s = self.state()
        self.assertNotIn("T001", s["autopilot"]["attention"])
        self.assertEqual(s["autopilot"]["queued"], ["T001"])
        proc.kill()
        proc.wait()
        self.bounded()
        self.assertEqual(len(self.runs("T001")), 2, "the queued retry got its session")
        self.assertEqual(self.state()["status"]["T001"], "done")

    # AP22: a session the owner resumed by hand is not resumed again beside it

    def test_a_session_resumed_by_hand_is_left_to_the_owner(self):
        home = tempfile.mkdtemp(prefix="claude-home-")
        self.addCleanup(base.shutil.rmtree, home, True)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = home
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR") if old is None
                        else os.environ.update(CLAUDE_CONFIG_DIR=old))
        self.script_for({"T001": ["doing", "done"]})
        autopilot.change_settings(self.tasks, max_parallel=1)
        autopilot.step(self.tasks)
        self.wait_calls(1)
        autopilot.change_settings(self.tasks, auto=False)  # paused: the owner picks the card up in a terminal
        self.bounded()
        run = autopilot.registry(self.tasks)["runs"][0]
        transcript = os.path.join(home, "projects", "-some-project", f"{run['session']}.jsonl")
        os.makedirs(os.path.dirname(transcript))
        open(transcript, "w").write("{}\n")
        later = time.time() + 600
        os.utime(transcript, (later, later))
        self.assertEqual(self.state()["status"]["T001"], "doing")
        autopilot.act(self.tasks, "settings", {"auto": True})
        self.bounded()
        self.assertEqual(len(self.runs("T001")), 1, "not resumed in a second process")
        self.assertIn("continued outside the autopilot", self.state()["autopilot"]["attention"]["T001"])
        autopilot.act(self.tasks, "retry", {"card": "T001"})  # the owner hands it back
        self.bounded()
        self.assertEqual(self.state()["status"]["T001"], "done")

    # SPLIT: the card is finished with a remainder; the run is not a try and is not resumed

    def test_a_split_card_is_finished_and_its_remainder_runs(self):
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"T001": ["split"]})
        self.bounded()
        s = self.state()
        self.assertEqual((s["status"]["T001"], s["status"]["T001R"]), ("done", "done"))
        self.assertEqual(len(self.runs("T001")), 1, "never resumed")
        self.assertEqual(s["autopilot"]["attention"], {})
        self.assertFalse(sv.is_try(autopilot.registry(self.tasks)["runs"][0]))

    def test_a_split_that_left_its_card_open_is_ticked_from_its_hand_off(self):
        self.script_for({"T001": ["splitopen"]})
        self.bounded()
        s = self.state()
        self.assertEqual(s["status"]["T001"], "done")
        self.assertIn("- [x] T001 ", open(self.tasks).read())
        self.assertEqual(len(self.runs("T001")), 1)
        self.assertEqual(s["status"]["T001R"], "done", "the remainder ran after it")

    def test_a_split_with_no_hand_off_goes_to_the_owner(self):
        self.script_for({"T001": ["splitbare"]})
        self.bounded()
        s = self.state()
        self.assertEqual(s["status"]["T001"], "doing")
        self.assertIn("SPLIT", s["autopilot"]["attention"]["T001"])
        self.assertEqual(len(self.runs("T001")), 1)

    def test_a_split_batch_starts_a_fresh_session_for_its_other_cards(self):
        self.add_batch()
        self.script_for({"B1": ["splitopen", "done"]})
        self.bounded()
        s = self.state()
        self.assertEqual((s["status"]["T003B"], s["status"]["T003C"]), ("done", "done"))
        runs = self.runs("B1")
        self.assertEqual(len(runs), 2)
        self.assertIn("--session-id", runs[1]["args"], "a split session is not resumed: a fresh one goes on")
        self.assertEqual(self.batch(s)["status"], "done")
        split = next(r for r in autopilot.registry(self.tasks)["runs"] if r.get("batch") == "B1")
        self.assertFalse(sv.is_try(split), "a split is not a failed try")
        self.assertEqual(s["autopilot"]["attention"], {})

    def test_a_split_session_is_not_resumed_after_a_failed_fresh_start(self):
        self.add_batch()
        self.script_for({"B1": ["splitopen", "noconversation"]})
        self.bounded()
        runs = self.runs("B1")
        self.assertEqual(len(runs), 3, "the fresh sessions are tries: max_attempts of them")
        self.assertFalse([r for r in runs if "--resume" in r["args"]], "the split conversation is never resumed")
        self.assertIn("B1", self.state()["autopilot"]["attention"])

    def test_a_batch_whose_session_ticked_the_split_card_goes_on_fresh(self):  # U11-4, §1's way
        self.add_batch()
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"B1": ["split", "done"]})
        self.bounded()
        s = self.state()
        self.assertEqual((s["status"]["T003B"], s["status"]["T003C"]), ("done", "done"))
        runs = self.runs("B1")
        self.assertEqual(len(runs), 2)
        self.assertIn("--session-id", runs[1]["args"])
        self.assertEqual(s["autopilot"]["attention"], {})


for _name in HELPERS:
    setattr(Accounting, _name, getattr(base.Feature, _name))


if __name__ == "__main__":
    unittest.main()
