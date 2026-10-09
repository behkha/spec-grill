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


if __name__ == "__main__":
    unittest.main()
