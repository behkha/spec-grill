"""Tests for hooks/card-rename.py, run as Claude Code runs it: JSON on stdin, a subprocess.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "card-rename.py")
SYSTEM_PYTHON = "/usr/bin/python3"  # macOS ships 3.9 here

TASKS = """# Tasks: Demo

(The supervisor of §6 is told to rename the session to `<NNN> Supervisor`; the cards follow §1.)

## 1. Session protocol

1. **One card per session.** First action: rename the session to `<NNN> T0nn <card title>`.

## 4. Cards

- [ ] T001 Re-verify and baseline
- [ ] T002 [P] Orders API — fulfills FR-1
- [x] T010B
- [x] T011B done 2026-10-01 (abc123; hand-off T011B.md)
- [ ] B3 Login polish
- [x] T012B [P]
- [x] T004B shipped early
- [ ] T005 Add `--dry-run` flag

#### T002 [P] — Orders API

**Start with:** `Demo · T002. Follow {tasks} §1, then card T002.`

### T010B — Bare backlog card

#### T011B — Finished backlog card

#### T012B [P] — Bare parallel card

#### T004B — Retry jitter
"""


class CardRenameHook(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="card rename ")  # a space in every path
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = os.path.join(self.root, "repo")
        self.folder = os.path.join(self.repo, "specs", "001-demo")
        os.makedirs(self.folder)
        os.mkdir(os.path.join(self.repo, ".git"))  # the repository's root, as far as the hook looks
        self.tasks = os.path.join(self.folder, "tasks.md")
        with open(self.tasks, "w", encoding="utf-8") as handle:
            handle.write(TASKS.format(tasks=self.tasks))

    def run_hook(self, stdin, python=sys.executable, env=None):
        env = {k: v for k, v in os.environ.items() if k != "SPEC_GRILL_AUTOPILOT"} if env is None else env
        data = stdin if isinstance(stdin, bytes) else stdin.encode("utf-8")
        done = subprocess.run([python, HOOK], input=data, capture_output=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stderr, b"")
        return done.stdout.decode("utf-8")

    def context(self, prompt, cwd=None, **kwargs):
        out = self.run_hook(json.dumps({"prompt": prompt, "cwd": cwd or self.repo}), **kwargs)
        if not out.strip():
            return ""
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def start(self, card, label="Demo", path=None):
        return f"{label} · {card}. Follow {path or self.tasks} §1, then card {card}."

    def test_a_start_line_names_the_session_from_tasks_md(self):
        context = self.context(self.start("T002"))
        self.assertIn('rename this session to "001 T002 Orders API"', context)

    def test_python_39_imports_and_runs_the_hook(self):  # PY39
        if not os.path.exists(SYSTEM_PYTHON):
            self.skipTest(f"no {SYSTEM_PYTHON}")
        context = self.context(self.start("T002"), python=SYSTEM_PYTHON)
        self.assertIn('"001 T002 Orders API"', context)
        self.assertEqual(self.run_hook('{"prompt": "hello"}', python=SYSTEM_PYTHON), "")

    def test_a_long_uppercase_run_does_not_stall_the_hook(self):  # CR20
        began = time.monotonic()
        self.assertEqual(self.context("Demo · " + "A" * 40 + " is what I typed"), "")
        self.assertEqual(self.context("Demo · " + "AB12" * 5000), "")
        self.assertLess(time.monotonic() - began, 5)

    def test_every_kind_of_card_id_matches(self):  # CR20: the flat id still takes every id in use
        for card in ("T001", "T012A", "T042B2", "T042R2A", "CPA", "CP0", "CPEND", "B3"):
            self.assertIn(card, self.context(self.start(card)), card)
        batch = f"Demo · B3. Follow {self.tasks} §1, then the cards of batch B3 (§5, Batches) in order."
        self.assertIn('"001 B3 Login polish"', self.context(batch))

    def test_a_path_with_a_space_and_a_label_with_a_dot_match(self):  # CR21
        self.assertIn(" ", self.tasks)
        context = self.context(self.start("T002", label="Webhook retries · v2"))
        self.assertIn('"001 T002 Orders API"', context)
        os.remove(self.tasks)  # no tasks.md: the name falls back to the whole label
        context = self.context(self.start("T002", label="Webhook retries · v2"))
        self.assertIn('"Webhook retries · v2 · T002"', context)
        long_label = "Checkout " * 10  # past the old 80-character cap, within the name's 120
        self.assertIn("T002", self.context(self.start("T002", label=long_label.strip())))

    def test_a_draft_and_a_sentence_end_still_match(self):  # CR21
        draft = os.path.join(self.folder, "tasks.draft.md")
        shutil.copy(self.tasks, draft)
        self.assertIn('"001 T002 Orders API"', self.context(f"Demo · T002. Follow {draft} §1."))
        self.assertIn('"001 T002 Orders API"', self.context(f"Demo · T002. Follow {self.tasks}."))

    def test_ordinary_prompts_pass_through(self):  # CR21: the looser pattern stays narrow
        for prompt in (
            "fix the login bug",
            "Demo · T002 is broken, follow up on tasks.md",
            "Demo · t002. Follow tasks.md",
            "Demo · T002. Follow the guide in tasks.mdx §1",
            "Demo · T002. Follow /x/mytasks.md/notes §1",
            f"Please read this.\nDemo · T002. Follow {self.tasks} §1, then card T002.",
            "",
        ):
            self.assertEqual(self.context(prompt), "", prompt)

    def test_a_bare_checklist_line_takes_the_heading_title(self):  # CR22
        self.assertIn('"001 T010B Bare backlog card"', self.context(self.start("T010B")))
        self.assertIn('"001 T011B Finished backlog card"', self.context(self.start("T011B")))

    def test_the_heading_beats_the_checklist_line(self):  # CR22, as the supervisor reads titles
        self.assertIn('"001 T012B Bare parallel card"', self.context(self.start("T012B")))
        self.assertIn('"001 T004B Retry jitter"', self.context(self.start("T004B")))

    def test_the_card_pattern_is_not_the_supervisors(self):
        self.assertIn('"001 T001 Re-verify and baseline"', self.context(self.start("T001")))

    def test_a_backticked_title_makes_a_clean_name(self):  # as autopilot.session_name strips them
        context = self.context(self.start("T005"))
        self.assertIn('"001 T005 Add --dry-run flag"', context)
        self.assertIn("`/rename 001 T005 Add --dry-run flag`", context)

    def test_a_leading_blank_line_or_a_wrapped_line_still_match(self):
        self.assertIn("Orders API", self.context("\n  " + self.start("T002")))
        self.assertIn("Orders API", self.context(f"Demo · T002.\nFollow {self.tasks} §1, then card T002."))

    def test_prose_after_follow_is_not_a_path(self):
        self.assertEqual(self.context("Login bug · T002. Follow up with QA on what tasks.md says"), "")

    def test_odd_input_exits_cleanly(self):  # CR23
        for stdin in ("[]", "null", '"text"', "42", '{"prompt": null}', '{"prompt": 7}',
                      '{"prompt": "x", "cwd": null}', "{", "", b"\xff\xfe"):
            self.assertEqual(self.run_hook(stdin), "", stdin)
        prompt = self.start("T002")
        self.assertIn("Orders API", self.context(prompt, cwd=None))
        self.assertIn("T002", self.run_hook(json.dumps({"prompt": prompt, "cwd": 3})))

    def test_a_stale_absolute_path_is_found_by_its_specs_part(self):  # CR24
        stale = "/old/checkout/elsewhere/specs/001-demo/tasks.md"
        self.assertIn('"001 T002 Orders API"', self.context(self.start("T002", path=stale)))
        inside = os.path.join(self.repo, "src", "deep")
        os.makedirs(inside)
        self.assertIn('"001 T002 Orders API"', self.context(self.start("T002", path=stale), cwd=inside))
        self.assertIn('"001 T002 Orders API"', self.context(self.start("T002", path="…/specs/001-demo/tasks.md"),
                                                            cwd=inside))

    def test_the_walk_up_stops_at_the_repository_root(self):  # CR24
        other = os.path.join(self.root, "specs", "009-other")
        os.makedirs(other)
        with open(os.path.join(other, "tasks.md"), "w", encoding="utf-8") as handle:
            handle.write("- [ ] T001 Another project's card\n")
        context = self.context(self.start("T001", path="…/specs/009-other/tasks.md"))
        self.assertNotIn("Another project", context)
        self.assertIn('"Demo · T001"', context)

    def test_the_walk_up_goes_past_a_worktrees_git_file(self):  # CR24: to the main checkout around it
        tree = os.path.join(self.repo, ".claude", "worktrees", "agent-x")
        os.makedirs(tree)
        with open(os.path.join(tree, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: /nowhere\n")
        context = self.context(self.start("T002", path="…/specs/001-demo/tasks.md"), cwd=tree)
        self.assertIn('"001 T002 Orders API"', context)

    def test_the_cli_is_told_to_ask_the_user_to_rename(self):  # SK13
        context = self.context(self.start("T002"))
        self.assertIn("set_session_title", context)
        self.assertIn("can't run slash commands", context)
        self.assertIn("asking the user to run `/rename 001 T002 Orders API`", context)
        self.assertNotIn("CLI: /rename.", context)

    def test_the_supervisor_line_and_the_autopilot(self):
        sup = f"Demo · SUP. Follow {self.tasks} §6."
        self.assertIn('"001 Supervisor"', self.context(sup))
        env = {**os.environ, "SPEC_GRILL_AUTOPILOT": "1"}
        self.assertEqual(self.run_hook(json.dumps({"prompt": self.start("T002")}), env=env), "")


if __name__ == "__main__":
    unittest.main()
