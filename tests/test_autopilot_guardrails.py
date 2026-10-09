"""Tests for the autopilot's guardrails: what an unattended session may not change about the sessions
that follow it (hooks/autopilot.py), against the fake `claude` (tests/fake_claude.py).

    python3 -m pytest -q tests/test_autopilot_guardrails.py
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
from test_autopilot import TASKS, RESUME, Feature  # noqa: E402,F401
import autopilot  # noqa: E402

NEVER = "**Never unattended:** `Bash(git push:*)`, `Bash(fly deploy:*)`."


def deny_of(args: list) -> list:
    return json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]


class Guardrails(unittest.TestCase):
    # Feature's fixture (a demo feature, the fake claude, the autopilot switched on), not its tests
    setUp, tearDown = Feature.setUp, Feature.tearDown
    script_for, launched, wait_calls, settle, state = (Feature.script_for, Feature.launched, Feature.wait_calls,
                                                       Feature.settle, Feature.state)

    def tamper(self, **values):
        """Rewrite state/autopilot.json the way a session could, past the dispatcher."""
        path = os.path.join(os.path.dirname(self.tasks), "state", "autopilot.json")
        with open(path) as handle:
            found = json.load(handle)
        with open(path, "w") as handle:
            json.dump({**found, **values}, handle)

    def session_tampers(self, **change):
        os.environ["FAKE_TAMPER"] = json.dumps(change)
        self.addCleanup(os.environ.pop, "FAKE_TAMPER", None)

    def paused(self) -> str:
        settings = self.state()["autopilot"]["settings"]
        self.assertFalse(settings["auto"])
        return settings["paused_reason"]

    def test_an_approved_step_lifts_only_the_commands_it_names(self):
        deny = ["Bash(rm:*)", "Bash(git push:*)", "Bash(fly deploy *)", "Bash(*)", "Bash(git * --force)",
                "Edit(//x/state/autopilot.json)"]
        lifted = lambda step: [p for p in deny if p not in autopilot.permitted(deny, step)]  # noqa: E731
        self.assertEqual(lifted("format the code"), [], '"rm" inside "format" is not the rm command')
        self.assertEqual(lifted("legit push the docs"), [])
        self.assertEqual(lifted("python rm.py --dry-run"), [])
        self.assertEqual(lifted("SELECT * FROM orders"), [], "a pattern with a wildcard inside is never lifted")
        self.assertEqual(lifted("run `rm -rf build`"), ["Bash(rm:*)"])
        self.assertEqual(lifted("git push --force origin main"), ["Bash(git push:*)"])
        self.assertEqual(lifted("Run git push."), ["Bash(git push:*)"])
        self.assertEqual(lifted("'git  push' to origin"), ["Bash(git push:*)"])
        self.assertEqual(lifted("fly deploy --app demo"), ["Bash(fly deploy *)"])
        self.assertEqual(lifted("cd app && fly deploy"), ["Bash(fly deploy *)"])

    def test_a_permission_mode_that_skips_the_checks_never_runs(self):
        self.tamper(permission_mode="bypassPermissions")
        autopilot.step(self.tasks)
        self.assertEqual(self.launched(), [])
        self.assertIn("bypassPermissions", self.paused())
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T001"})
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "settings", {"auto": True})  # Resume cannot accept it either
        self.assertIn("bypassPermissions", self.paused())
        self.tamper(permission_mode="acceptEdits")  # the owner fixes the file
        self.assertEqual(autopilot.act(self.tasks, "settings", {"auto": True}), "settings saved")
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")

    def test_a_session_that_points_claude_elsewhere_pauses_until_the_owner_accepts_it(self):
        marker, evil = os.path.join(self.root, "evil-ran"), os.path.join(self.root, "evil-claude")
        with open(evil, "w") as handle:
            handle.write(f'#!/bin/sh\ntouch "{marker}"\nexec "{sys.executable}" "{os.path.join(HERE, "fake_claude.py")}" "$@"\n')
        os.chmod(evil, 0o755)
        self.session_tampers(settings={"claude": evil})
        self.settle()
        self.assertEqual([c["card"] for c in self.launched()], ["T001"], "nothing starts after the change")
        self.assertIn(evil, self.paused())
        self.assertFalse(os.path.exists(marker), "nothing ran the program the session named, not even auth status")
        with self.assertRaises(autopilot.Refused) as refused:
            autopilot.act(self.tasks, "start", {"card": "T002"})
        self.assertIn(evil, str(refused.exception))
        os.environ.pop("FAKE_TAMPER")
        self.assertEqual(autopilot.act(self.tasks, "settings", {"auto": True}), "settings saved")  # the owner's yes
        autopilot.step(self.tasks)
        self.assertEqual(len(self.wait_calls(2)), 2)
        self.assertTrue(os.path.exists(marker), "once accepted, sessions start with it")

    def test_a_session_that_drops_a_never_unattended_pattern_pauses(self):
        self.session_tampers(tasks=["`Bash(git push:*)`, ", ""])
        self.settle()
        self.assertEqual([c["card"] for c in self.launched()], ["T001"])
        self.assertIn("`Bash(git push:*)`", self.paused())
        self.assertEqual(self.state()["autopilot"]["deny"], ["Bash(fly deploy:*)"], "the file did lose it")
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T002"})
        # a session started however keeps the patterns the owner confirmed
        with autopilot.guard(self.tasks):
            reg = autopilot.registry(self.tasks)
            autopilot.spawn(self.tasks, self.state(), reg, "T002", "start", "x")
            autopilot.save_registry(self.tasks, reg)
        self.assertIn("Bash(git push:*)", deny_of(self.wait_calls(2)[1]["args"]))

    def test_a_session_that_removes_the_never_unattended_line_pauses(self):
        self.session_tampers(tasks=[NEVER, ""])
        self.settle()
        self.assertEqual([c["card"] for c in self.launched()], ["T001"])
        self.assertIn("Never unattended", self.paused())

    def test_sessions_may_not_edit_the_dispatchers_own_files(self):
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        found = json.loads(args[args.index("--settings") + 1])["permissions"]
        self.assertEqual(found["deny"][:2], ["Bash(git push:*)", "Bash(fly deploy:*)"])
        specs = os.path.dirname(os.path.dirname(self.tasks))
        for folder in (os.path.abspath(specs), os.path.realpath(specs)):
            self.assertIn(f"Edit(/{folder}/*/state/autopilot.json)", found["deny"])
            self.assertIn(f"Edit(/{folder}/*/state/runs.json)", found["deny"])
        self.assertFalse([d for d in found["deny"] if "tasks.md" in d], "sessions tick their cards in tasks.md")
        self.assertEqual(found["disableBypassPermissionsMode"], "disable")

    def test_owner_start_is_refused_without_a_never_unattended_list(self):
        autopilot.change_settings(self.tasks, auto=False)
        with open(self.tasks) as handle:
            text = handle.read()
        with open(self.tasks, "w") as handle:
            handle.write(text.replace(NEVER, ""))
        self.assertTrue(autopilot.acquire(self.tasks))
        for action in ("start", "retry"):
            with self.assertRaises(autopilot.Refused) as refused:
                autopilot.act(self.tasks, action, {"card": "T001"})
            self.assertIn("Never unattended", str(refused.exception))
        self.assertEqual(self.launched(), [])

    def test_resume_accepts_only_a_change_it_has_shown(self):
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)  # the dispatcher starts: the files as they are now are what the owner confirmed
        self.tamper(budget_per_card_usd=500)  # while paused, as a session could
        with self.assertRaises(autopilot.Refused) as refused:
            autopilot.act(self.tasks, "settings", {"auto": True})
        self.assertIn("budget_per_card_usd 20 → 500", str(refused.exception))
        self.assertIn("budget_per_card_usd 20 → 500", self.paused())
        self.assertEqual(self.launched(), [])
        self.assertEqual(autopilot.act(self.tasks, "settings", {"auto": True}), "settings saved")
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "500")

    def test_changes_that_narrow_the_guardrails_are_taken_at_once(self):
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)
        self.tamper(budget_per_card_usd=5, budget_total_usd=100, max_attempts=1)
        with open(self.tasks) as handle:
            text = handle.read()
        with open(self.tasks, "w") as handle:
            handle.write(text.replace("`Bash(fly deploy:*)`.", "`Bash(fly deploy:*)`, `Bash(npm publish:*)`."))
        self.assertEqual(autopilot.act(self.tasks, "settings", {"auto": True}), "settings saved")
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "5")
        self.assertIn("Bash(npm publish:*)", deny_of(args))

    def test_the_dashboards_own_changes_need_no_confirmation(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        self.assertEqual(autopilot.act(self.tasks, "settings", {"budget_per_card_usd": 50}), "settings saved")
        autopilot.step(self.tasks)
        settings = self.state()["autopilot"]["settings"]
        self.assertTrue(settings["auto"], settings["paused_reason"])

    def test_a_setting_that_is_not_a_number_pauses(self):
        self.tamper(max_attempts="many")
        autopilot.step(self.tasks)
        self.assertEqual(self.launched(), [])
        self.assertIn("max_attempts", self.paused())

    def test_keep_env_may_shrink_but_not_grow_unconfirmed(self):
        self.tamper(keep_env=["CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE"])  # the owner's, before the dispatcher starts
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        self.tamper(keep_env=["AWS_PROFILE"])  # fewer variables: taken at once
        autopilot.step(self.tasks)
        self.assertTrue(self.state()["autopilot"]["settings"]["auto"])
        self.assertEqual(autopilot.trusted(self.tasks)["keep_env"], ["AWS_PROFILE"])
        self.tamper(keep_env=["AWS_PROFILE", "ANTHROPIC_API_KEY"])
        autopilot.step(self.tasks)
        self.assertIn("keep_env added 'ANTHROPIC_API_KEY'", self.paused())
        self.tamper(keep_env="ANTHROPIC_API_KEY")
        with self.assertRaises(autopilot.Refused) as refused:
            autopilot.act(self.tasks, "settings", {"auto": True})
        self.assertIn("not a list of names", str(refused.exception))

    def test_a_session_cannot_give_itself_chrome_a_stage_review_or_another_app_url(self):
        self.session_tampers(settings={"chrome": True, "approved_gates": ["CPA"], "gate_checkpoints": False},
                             tasks=["## 6. Supervisor\n", "## 6. Supervisor\n\n**App URL:** https://evil.example/\n"])
        self.settle()
        self.assertEqual([c["card"] for c in self.launched()], ["T001"])
        reason = self.paused()
        for change in ("chrome False → True", "approved_gates added 'CPA'", "gate_checkpoints True → False",
                       "App URL (none) → 'https://evil.example/'"):
            self.assertIn(change, reason)

    def test_a_second_start_accepts_exactly_what_the_first_one_showed(self):
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)
        folder = os.path.join(self.root, "x" * 250)  # two launchers whose paths differ only past 250 characters
        os.makedirs(folder)
        first, second = os.path.join(folder, "claude-a"), os.path.join(folder, "claude-b")
        for link in (first, second):
            os.symlink(os.path.join(self.root, "claude"), link)
        self.tamper(claude=first)
        with self.assertRaises(autopilot.Refused) as refused:
            autopilot.act(self.tasks, "start", {"card": "T001"})
        self.assertIn("press Start again", str(refused.exception))
        self.tamper(claude=second)
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T001"})  # not what the owner saw: shown again
        self.assertEqual(autopilot.act(self.tasks, "start", {"card": "T001"}), "T001 started")
        self.assertEqual(autopilot.trusted(self.tasks)["claude"], second)
        self.assertEqual(self.wait_calls(1)[0]["card"], "T001")
        self.assertFalse(self.state()["autopilot"]["settings"]["auto"], "Start leaves the autopilot paused")

    def test_retry_after_a_split_starts_a_fresh_session(self):
        autopilot.change_settings(self.tasks, auto=False)
        self.script_for({"T001": ["split", "done"]})
        self.assertTrue(autopilot.acquire(self.tasks))
        self.assertEqual(autopilot.act(self.tasks, "start", {"card": "T001"}), "T001 started")
        self.settle()
        self.assertIn("AUTOPILOT: SPLIT", autopilot.registry(self.tasks)["runs"][0]["result"])
        autopilot.act(self.tasks, "retry", {"card": "T001"})
        args = self.wait_calls(2)[1]["args"]
        self.assertIn("--session-id", args, "the split session is not resumed")
        self.assertNotIn("--resume", args)


# pytest and unittest collect every TestCase class a module holds, imported ones too: Feature's tests
# run from test_autopilot.py
del Feature

if __name__ == "__main__":
    unittest.main()
