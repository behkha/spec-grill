"""Tests for the integration of the review-fix branches: how their changes meet in one dispatcher, and the
follow-ups found while merging them.

    python3 -m pytest -q tests/test_integration_followups.py
"""

from __future__ import annotations

import contextlib
import json
import os
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


if __name__ == "__main__":
    unittest.main()
