"""Tests for how hooks/autopilot.py starts a session (spawn): a launcher that cannot start, an argument the
OS refuses, the registry saved at once, the environment a session gets, the prompt after `--`, and a batch
that names no card.

    python3 -m pytest -q tests/test_autopilot_spawn.py
"""

import errno
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
import test_autopilot as base  # noqa: E402  (a module, so its Feature tests are not collected twice)

# Feature's fixture (setUp, tearDown) and helpers, without its tests: those run from test_autopilot.py
FeatureHelpers = type("FeatureHelpers", (unittest.TestCase,),
                      {k: v for k, v in vars(base.Feature).items() if not k.startswith("test_")})


class Spawn(FeatureHelpers):
    def runs(self) -> list:
        return autopilot.registry(self.tasks)["runs"]

    def test_a_launcher_that_cannot_start_pauses_without_spending_an_attempt(self):
        wrapper = autopilot.settings(self.tasks)["claude"]
        missing = os.path.join(self.root, "no-such-claude")
        autopilot.change_settings(self.tasks, claude=missing)
        events = autopilot.step(self.tasks)
        self.assertIn(f"needs you: autopilot paused: could not start {missing}", " ".join(events),
                      "the owner hears of the pause in the same pass")
        for _ in range(3):
            events += autopilot.step(self.tasks)
        cfg = autopilot.settings(self.tasks)
        self.assertFalse(cfg["auto"], events)
        self.assertTrue(cfg["paused_reason"].startswith(f"could not start {missing}: "), cfg["paused_reason"])
        self.assertIn("autopilot paused: could not start", " ".join(events))
        runs = self.runs()
        self.assertEqual(len(runs), 1, "paused after the first failure, not one failure per pass")
        self.assertFalse(any(sv.is_try(r) for r in runs), "a session that never started is not a try")
        self.assertEqual(autopilot.registry(self.tasks)["attention"], {})

        autopilot.change_settings(self.tasks, claude=wrapper, auto=True, paused_reason="")
        autopilot.step(self.tasks)
        first = self.wait_calls(1)
        self.assertEqual(first[0]["card"], "T001")
        self.assertIn("--session-id", first[0]["args"])

    def test_a_launcher_that_cannot_start_refuses_the_owners_start_and_pauses(self):
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)  # takes the dispatcher lock
        autopilot.change_settings(self.tasks, claude=os.path.join(self.root, "no-such-claude"), auto=True)
        with self.assertRaises(autopilot.Refused) as caught:
            autopilot.act(self.tasks, "start", {"card": "T001"})
        self.assertIn("could not start", str(caught.exception))
        self.assertFalse(autopilot.settings(self.tasks)["auto"])
        self.assertFalse(any(sv.is_try(r) for r in self.runs()))

    def test_an_argument_the_os_refuses_is_caught_and_the_card_goes_to_the_owner(self):
        text = open(self.tasks).read().replace("`Demo · T001. Follow", "`Demo · T001.\x00 Follow", 1)
        open(self.tasks, "w").write(text)
        events = autopilot.step(self.tasks)  # Popen raises ValueError (embedded null byte)
        self.assertTrue(any("failed" in e for e in events), events)
        runs = self.runs()
        self.assertEqual(len(runs), 1, "the failed start is recorded in runs.json")
        self.assertIn("null", runs[0]["error"])
        self.assertIn("null", autopilot.registry(self.tasks)["attention"]["T001"])
        self.assertFalse(sv.is_try(runs[0]), "it never started")
        self.assertTrue(autopilot.settings(self.tasks)["auto"], "one card's bad line does not pause the rest")
        self.assertEqual(self.launched(), [])

    def test_a_prompt_too_long_for_the_os_goes_to_the_owner_without_pausing(self):
        too_long = OSError(errno.E2BIG, "Argument list too long")
        with mock.patch.object(autopilot.subprocess, "Popen", side_effect=too_long):
            autopilot.step(self.tasks)
        self.assertIn("Argument list too long", autopilot.registry(self.tasks)["attention"]["T001"])
        self.assertTrue(autopilot.settings(self.tasks)["auto"])

    def test_each_session_is_saved_as_soon_as_it_starts(self):
        self.disjoint()
        self.finish("T001")
        self.script_for({"T002": ["sleep"]})
        real_room, calls = autopilot.room, []

        def room(*args):
            calls.append(args[3])
            if len(calls) == 2:
                raise RuntimeError("a crash after the first session started")
            return real_room(*args)

        with mock.patch.object(autopilot, "room", room), self.assertRaises(RuntimeError):
            autopilot.step(self.tasks)
        saved = [r for r in self.runs() if not r["ended"]]
        self.assertEqual([r["card"] for r in saved], ["T002"], "the live session survived the crash in runs.json")
        autopilot.step(self.tasks)
        self.assertEqual([c["card"] for c in self.launched()].count("T002"), 1, "no second session for T002")

    def test_sessions_do_not_inherit_the_api_key_or_claude_code_variables(self):
        dump = os.path.join(self.root, "env.jsonl")
        planted = {"ANTHROPIC_API_KEY": "sk-test", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                   "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_OAUTH_SCOPES": "x"}
        passed = {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_AUTH_TOKEN": "t"}
        with mock.patch.dict(os.environ, {**planted, **passed, "FAKE_ENV": dump}):
            autopilot.step(self.tasks)
            self.wait_calls(1)
        got = [json.loads(line) for line in open(dump)]
        session = next(g for g in got if g["card"] == "T001")
        check = next(g for g in got if g["args"] == ["auth", "status"])
        for seen in (session, check):  # the account check sees what the sessions will bill
            self.assertFalse(set(planted) & set(seen["env"]), seen["env"])
            self.assertTrue(set(passed) <= set(seen["env"]), "provider switches and a setup-token pass")
            self.assertIn("FAKE_TASKS", seen["env"])
        self.assertIn("SPEC_GRILL_CARD", session["env"])

        with mock.patch.dict(os.environ, planted):
            env = autopilot.session_env({"keep_env": ["ANTHROPIC_API_KEY"]}, SPEC_GRILL_CARD="T001")
            self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-test", "the owner kept it on purpose")
            self.assertNotIn("CLAUDECODE", env)
            self.assertEqual(env["SPEC_GRILL_CARD"], "T001")
            self.assertNotIn("ANTHROPIC_API_KEY", autopilot.session_env({"keep_env": "ANTHROPIC_API_KEY"}),
                             "keep_env is a list; anything else keeps nothing")

    def test_a_prompt_starting_with_a_dash_follows_a_double_dash(self):
        text = open(self.tasks).read().replace("`Demo · T001. Follow", "`-Demo · T001. Follow", 1)
        open(self.tasks, "w").write(text)
        autopilot.step(self.tasks)
        args = self.wait_calls(1)[0]["args"]
        self.assertEqual(args[-2:], ["--", f"-Demo · T001. Follow {self.tasks} §1, then card T001."])

    def test_starting_a_batch_that_names_no_card_is_refused(self):
        self.add_to_tasks("## 6. Supervisor", """## 5. Backlog

### Batches

- [ ] B9 Empty

| batch | name | cards, in order | effort | Start with |
| --- | --- | --- | --- | --- |
| B9 | Empty | T009Z | | `Demo · B9. Follow the cards of batch B9 in order.` |
""")
        autopilot.change_settings(self.tasks, auto=False)
        autopilot.step(self.tasks)  # takes the dispatcher lock
        with self.assertRaises(autopilot.Refused) as caught:
            autopilot.act(self.tasks, "start", {"card": "B9"})
        self.assertIn("names no card", str(caught.exception))
        self.assertEqual(self.runs(), [])

        self.script_for({"T001": ["sleep"]})
        autopilot.act(self.tasks, "start", {"card": "T001"})  # a serial unit holds the integration worktree
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "B9"})
        self.assertNotIn("B9", autopilot.registry(self.tasks)["queued"], "refused, not queued for ever")


if __name__ == "__main__":
    unittest.main()
