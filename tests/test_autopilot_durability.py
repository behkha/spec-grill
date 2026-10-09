"""Durability tests for hooks/autopilot.py: partial reads of tasks.md, owner writes that race with a live
session, a corrupt runs.json, and usage-limit pauses that resume on their own.

    python3 -m pytest -q tests/test_autopilot_durability.py
"""

import datetime as dt
import json
import os
import stat
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
from test_autopilot import TASKS, RESUME, Feature  # noqa: E402,F401

# Feature's set-up and helpers without its tests (those run in test_autopilot.py)
Base = type("Base", (unittest.TestCase,), {k: v for k, v in vars(Feature).items()
                                          if not k.startswith("test") and k not in ("__dict__", "__weakref__")})
del Feature


def utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


class PartialReads(Base):
    """AP12: a pass that reads tasks.md while a session rewrites it must not forget state for good."""

    def keep(self) -> None:
        reg = autopilot.registry(self.tasks)
        reg["queued"] = ["T002"]
        reg["blocked_on"] = {"T003": ["T003: needs a key"]}
        autopilot.save_registry(self.tasks, reg)

    def test_an_empty_tasks_read_drops_nothing_and_does_not_pause(self):
        self.keep()
        good = open(self.tasks).read()
        open(self.tasks, "w").close()  # a session is half way through rewriting it
        autopilot.step(self.tasks)
        with open(self.tasks, "w") as handle:
            handle.write(good)
        reg = autopilot.registry(self.tasks)
        self.assertEqual(reg["queued"], ["T002"])
        self.assertEqual(reg["blocked_on"], {"T003": ["T003: needs a key"]})
        cfg = autopilot.settings(self.tasks)
        self.assertTrue(cfg["auto"], cfg["paused_reason"])
        self.assertEqual(self.launched(), [], "nothing starts on a pass that read no cards")

    def test_a_sharp_drop_in_cards_waits_a_pass_before_pruning(self):
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)  # a good read: 8 cards
        self.keep()
        reg = autopilot.registry(self.tasks)
        reg["queued"], reg["blocked_on"] = ["T006"], {"CPEND": []}
        autopilot.save_registry(self.tasks, reg)
        good = open(self.tasks).read()
        with open(self.tasks, "w") as handle:
            handle.write(good[: good.index("- [ ] T004")])  # cut off mid-write
        cut = sv.build(self.tasks, 4)
        self.assertLess(len(cut["cards"]), 4)
        autopilot.step(self.tasks)
        reg = autopilot.registry(self.tasks)
        self.assertEqual((reg["queued"], reg["blocked_on"]), (["T006"], {"CPEND": []}), "one short read prunes nothing")
        autopilot.step(self.tasks)  # the same short file twice: the owner really removed those cards
        reg = autopilot.registry(self.tasks)
        self.assertEqual((reg["queued"], reg["blocked_on"]), ([], {}))

    def test_a_read_cut_before_the_never_unattended_list_does_not_pause(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        good = open(self.tasks).read()
        with open(self.tasks, "w") as handle:
            handle.write(good[: good.index("## 6. Supervisor")])  # every card, but not §6 yet
        events = autopilot.step(self.tasks)
        self.assertTrue(autopilot.settings(self.tasks)["auto"], events)
        self.assertTrue(any("waiting a pass" in e for e in events), events)
        autopilot.step(self.tasks)  # still missing on the next pass: it really is gone
        self.assertIn("Never unattended", autopilot.settings(self.tasks)["paused_reason"])

    def test_a_blocker_that_ends_on_a_short_read_is_recorded_on_the_next_pass(self):
        self.script_for({"T001": ["blocked"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        for _ in range(50):
            if all(p.poll() is not None for p in autopilot.PROCS.values()):
                break
            time.sleep(0.1)
        good = open(self.tasks).read()
        open(self.tasks, "w").close()
        autopilot.step(self.tasks)  # reaps the session, but reads no cards
        with open(self.tasks, "w") as handle:
            handle.write(good)
        self.assertNotIn("T001", autopilot.registry(self.tasks)["blocked_on"])
        autopilot.step(self.tasks)
        self.assertEqual(autopilot.registry(self.tasks)["blocked_on"], {"T001": ["T001: needs a key"]})
        self.assertEqual(len(self.launched()), 1, "it waits for its blocker instead of running again")


class OwnerWrites(Base):
    """AP13: the owner's edits to RESUME.md and tasks.md must not wipe a session's write made at the same time."""

    def race(self, path: str, extra: str):
        """Make the next read of path return what it held, then let a 'session' change it at once."""
        real = autopilot.sv.read
        fired = []

        def read(p):
            text = real(p)
            if p == path and not fired:
                fired.append(1)
                with open(path, "a") as handle:  # the session writes between our read and our replace
                    handle.write(extra)
            return text
        autopilot.sv.read = read
        self.addCleanup(setattr, autopilot.sv, "read", real)
        return fired

    def test_a_concurrent_resume_edit_is_not_lost(self):
        s = sv.build(self.tasks, 4)
        os.chmod(s["resume"], 0o640)
        fired = self.race(s["resume"], "\n## Session note\nwritten by the session\n")
        autopilot.set_row(s, "status", {"card": "T001"}, {"status": "doing"})
        self.assertTrue(fired)
        text = open(s["resume"]).read()
        self.assertIn("written by the session", text, "the session's write survived")
        self.assertRegex(text, r"\| T001 \| x \| doing \|", "the owner's write landed")
        self.assertEqual(stat.S_IMODE(os.stat(s["resume"]).st_mode), 0o640, "the file keeps its mode")

    def test_a_concurrent_tasks_edit_is_not_lost_when_ticking(self):
        os.chmod(self.tasks, 0o600)
        self.race(self.tasks, "\n<!-- written by the session -->\n")
        autopilot.tick(self.tasks, "T005")
        text = open(self.tasks).read()
        self.assertIn("written by the session", text)
        self.assertIn("- [x] T005", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.tasks).st_mode), 0o600)

    def test_a_concurrent_blocker_edit_is_not_lost(self):
        s = sv.build(self.tasks, 4)
        text = open(s["resume"]).read().replace("## Blockers\n", "## Blockers\n- T003: needs a key\n")
        open(s["resume"], "w").write(text)
        self.race(s["resume"], "\nsession line\n")
        autopilot.resolve_blocker(sv.build(self.tasks, 4), "T003: needs a key")
        text = open(s["resume"]).read()
        self.assertIn("session line", text)
        self.assertIn("(resolved", text)


class Registry(Base):
    """AP19: a corrupt runs.json falls back to its .bak, or pauses; it never starts over from empty."""

    def runs(self) -> str:
        return os.path.join(autopilot.state_dir(self.tasks), "runs.json")

    def test_a_corrupt_registry_falls_back_to_the_backup(self):
        reg = autopilot.registry(self.tasks)
        reg["granted"] = {"T002": 1}
        autopilot.save_registry(self.tasks, reg)
        autopilot.save_registry(self.tasks, reg)  # the previous good file becomes runs.json.bak
        self.assertTrue(os.path.exists(self.runs() + ".bak"))
        with open(self.runs(), "w") as handle:
            handle.write('{"runs": [')  # a crash left it half written
        self.assertEqual(autopilot.registry(self.tasks)["granted"], {"T002": 1})
        autopilot.change_settings(self.tasks, auto=False)
        events = autopilot.step(self.tasks)
        self.assertTrue(any("runs.json.bak" in e for e in events), events)
        with open(self.runs()) as handle:
            self.assertEqual(json.load(handle)["granted"], {"T002": 1}, "the next save writes a good file again")

    def test_a_backup_that_lacks_a_logged_session_pauses(self):
        autopilot.save_registry(self.tasks, autopilot.registry(self.tasks))
        autopilot.save_registry(self.tasks, autopilot.registry(self.tasks))
        os.makedirs(os.path.join(autopilot.state_dir(self.tasks), "runs"), exist_ok=True)
        open(os.path.join(autopilot.state_dir(self.tasks), "runs", "T001-1.jsonl"), "w").close()  # spawned after the .bak
        with open(self.runs(), "w") as handle:
            handle.write("")
        autopilot.step(self.tasks)
        cfg = autopilot.settings(self.tasks)
        self.assertFalse(cfg["auto"])
        self.assertIn("state/runs/T001-1.jsonl", cfg["paused_reason"])
        self.assertEqual(self.launched(), [])

    def test_a_corrupt_registry_without_a_backup_pauses_and_is_kept(self):
        with open(self.runs(), "w") as handle:
            handle.write("{not json")
        autopilot.step(self.tasks)
        cfg = autopilot.settings(self.tasks)
        self.assertFalse(cfg["auto"])
        self.assertIn("runs.json", cfg["paused_reason"])
        self.assertEqual(self.launched(), [], "nothing starts from an empty registry")
        self.assertEqual(open(self.runs()).read(), "{not json", "the broken file is left for the owner")
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T001"})

    def test_saving_keeps_a_good_backup_only(self):
        autopilot.save_registry(self.tasks, autopilot.registry(self.tasks))
        with open(self.runs(), "w") as handle:
            handle.write("garbage")
        reg = autopilot.registry(self.tasks)  # recovered from nothing: no .bak yet, so it is corrupt
        self.assertTrue(reg.get("corrupt"))
        autopilot.save_registry(self.tasks, reg)
        self.assertEqual(open(self.runs()).read(), "garbage", "a corrupt registry is never saved over")


class UsageLimits(Base):
    """AP24: a usage-limit pause resumes on its own once its reset time has passed; a login one waits."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, autopilot, "RESUME_MIN_S", autopilot.RESUME_MIN_S)
        autopilot.RESUME_MIN_S = 0

    def pause_with(self, behaviour: str) -> dict:
        autopilot.change_settings(self.tasks, max_parallel=1)
        self.script_for({"T001": [behaviour, "done"]})
        for _ in range(60):
            autopilot.step(self.tasks)
            if not autopilot.settings(self.tasks)["auto"]:
                break
            time.sleep(0.15)
        cfg = autopilot.settings(self.tasks)
        self.assertFalse(cfg["auto"])
        self.assertIn("API error", cfg["paused_reason"])
        return cfg

    def test_a_usage_limit_with_a_reset_time_resumes_after_it(self):
        cfg = self.pause_with("limit=2")
        self.assertAlmostEqual(cfg["resume_after"], time.time() + 2, delta=3)
        self.assertIn("resumes on its own", cfg["paused_reason"])
        autopilot.step(self.tasks)
        self.assertFalse(autopilot.settings(self.tasks)["auto"], "not before the reset")
        time.sleep(max(0.0, cfg["resume_after"] - time.time()) + 0.2)
        events = autopilot.step(self.tasks)
        cfg = autopilot.settings(self.tasks)
        self.assertTrue(cfg["auto"])
        self.assertEqual((cfg["resume_after"], cfg["paused_reason"]), (0, ""))
        self.assertTrue(any("resumed" in e for e in events), events)
        self.assertEqual(len([c for c in self.wait_calls(2) if c["card"] == "T001"]), 2, "the card runs again")

    def test_a_usage_limit_without_a_time_waits_the_default(self):
        cfg = self.pause_with("limit")
        self.assertAlmostEqual(cfg["resume_after"], time.time() + autopilot.RESUME_DEFAULT_S, delta=10)

    def test_a_login_error_waits_for_the_owner(self):
        cfg = self.pause_with("apierror")
        self.assertEqual(cfg["resume_after"], 0)
        self.assertNotIn("resumes on its own", cfg["paused_reason"])

    def test_a_reset_already_past_still_waits_the_minimum(self):
        autopilot.RESUME_MIN_S = 120
        cfg = self.pause_with("limit=-30")
        self.assertAlmostEqual(cfg["resume_after"], time.time() + 120, delta=10)

    def test_the_owner_pausing_cancels_the_timer(self):
        self.pause_with("limit=1")
        autopilot.act(self.tasks, "settings", {"auto": False})
        time.sleep(1.2)
        autopilot.step(self.tasks)
        self.assertFalse(autopilot.settings(self.tasks)["auto"])


class ResetTimes(unittest.TestCase):
    def test_reset_times_in_the_cli_messages(self):
        now = utc(2026, 10, 9, 14, 0)
        at = lambda text: autopilot.reset_time(text, now)  # noqa: E731
        self.assertEqual(at("Claude AI usage limit reached|1791561600"), 1791561600)
        self.assertEqual(at("You've hit your session limit · resets 3pm (UTC)"), utc(2026, 10, 9, 15, 0).timestamp() + 60)
        self.assertEqual(at("5-hour limit reached ∙ resets 3:10pm (UTC)"), utc(2026, 10, 9, 15, 10).timestamp() + 60)
        self.assertEqual(at("limit reached · resets 1pm (UTC)"), utc(2026, 10, 10, 13, 0).timestamp() + 60, "tomorrow")
        self.assertEqual(at("limit reached · resets 1:55pm (UTC)"), now.timestamp(), "just passed: now")
        self.assertEqual(at("Weekly limit reached ∙ resets Oct 12, 9am (UTC)"), utc(2026, 10, 12, 9, 0).timestamp() + 60)
        self.assertEqual(at("You've hit your session limit · resets 3pm (Europe/London)"),
                         utc(2026, 10, 9, 14, 0).timestamp() + 60, "London is UTC+1 in October")
        self.assertIsNone(at("You've hit your usage limit"))
        self.assertIsNone(at("rate limited|99"))


if __name__ == "__main__":
    unittest.main()
