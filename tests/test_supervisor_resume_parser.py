"""Tests for how hooks/supervisor.py reads state/RESUME.md and tasks.md §6's "Runs as" line.

    python3 -m pytest -q tests/test_supervisor_resume_parser.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
from test_autopilot import RESUME, TASKS  # noqa: E402
import supervisor as sv  # noqa: E402

ORDER = ["T001", "T002", "T003", "T004", "CPA", "T005", "T006", "CPEND"]
HEAD = RESUME.split("## Status")[0]
STATUS = ("## Status\n| card | title | status | branch | commit | date (UTC) |\n"
          "| --- | --- | --- | --- | --- | --- |\n")


def rows(**said) -> str:
    """The status table's rows: todo unless said ("T002": "doing" or ("done", "<commit>"))."""
    out = ""
    for c in ORDER:
        st, commit = (said.get(c, "todo"), "-") if not isinstance(said.get(c), tuple) else said[c]
        out += f"| {c} | x | {st} | - | {commit} | - |\n"
    return out


class Built(unittest.TestCase):
    """A feature folder like test_autopilot.Feature's, without the autopilot."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="resume-parser-test-")
        folder = os.path.join(self.root, "specs", "001-demo")
        os.makedirs(os.path.join(folder, "state"))
        self.tasks = os.path.join(folder, "tasks.md")
        with open(self.tasks, "w") as handle:
            handle.write(TASKS.replace("{tasks}", self.tasks))
        subprocess.run(["git", "init", "-q", self.root], check=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, resume: str) -> dict:
        with open(os.path.join(os.path.dirname(self.tasks), "state", "RESUME.md"), "w") as handle:
            handle.write(resume)
        return sv.build(self.tasks, 24)


class StatusWords(Built):
    def test_markdown_and_synonyms_normalise(self):
        cases = {"**Doing**": "doing", "in progress": "doing", "In-Progress": "doing", "_WIP_": "doing",
                 "started": "doing", "in review": "doing", "`done`": "done", "Completed": "done",
                 "finished.": "done", "Done (merged)": "done", "blocked (vendor)": "blocked", "on hold": "blocked",
                 "To do": "todo", "not started": "todo", "": "", "-": "", "—": "",
                 "xyz": "xyz", "pending review": "pending review", "not done": "not done"}
        for cell, want in cases.items():
            self.assertEqual(sv.status_word(cell), want, cell)

    def test_card_worked_on_by_hand_is_doing_not_ready(self):
        s = self.build(HEAD + STATUS + rows(T001="done", T002="**Doing**", T003="in progress"))
        self.assertEqual((s["status"]["T002"], s["status"]["T003"]), ("doing", "doing"))
        self.assertNotIn("T002", s["ready"])
        self.assertNotIn("T003", s["ready"])
        self.assertIn("T002", s["doing"])

    def test_unknown_status_is_drift(self):
        s = self.build(HEAD + STATUS + rows(T001="done", T004="xyz"))
        self.assertEqual(s["status"]["T004"], "todo")  # its tick in tasks.md stands in
        self.assertTrue(any(d.startswith("T004 has unknown status 'xyz' in RESUME") for d in s["drift"]), s["drift"])
        self.assertFalse(any("unknown status" in d and "T004" not in d for d in s["drift"]))


class Ranges(Built):
    def test_decision_holds_every_card_of_a_range(self):
        resume = HEAD.replace("| T006 |", "| T002–T004 |") + STATUS + rows(T001="done")
        s = self.build(resume)
        self.assertEqual(s["open_decisions"][0]["before"], ["T002", "T003", "T004"])
        self.assertEqual(s["ready"], [])
        self.assertIn({"kind": "owner", "on": "decision 1: Which payment provider?"}, s["waiting"]["T003"])

    def test_blocker_ranges(self):
        self.assertEqual(sv.blocker_subjects("T002–T004: need fixtures", ORDER), ["T002", "T003", "T004"])
        self.assertEqual(sv.blocker_subjects("Fixtures before T002-T004.", ORDER), ["T002", "T003", "T004"])
        self.assertEqual(sv.blocker_subjects("T002 - T003 waiting on fixtures", ORDER), ["T002", "T003"])

    def test_blocker_range_holds_the_middle_card(self):
        resume = HEAD.replace("## Blockers\n", "## Blockers\n- Seed data before T002–T004\n") + STATUS + rows(T001="done")
        s = self.build(resume)
        self.assertTrue(any(w["kind"] == "blocker" for w in s["waiting"]["T003"]))


class KnownCards(Built):
    def test_word_like_an_id_is_no_card(self):
        self.assertEqual(sv.blocker_subjects("CPU quota exceeded before deploy", ORDER), [])
        self.assertEqual(sv.blocker_subjects("CPP toolchain missing: T004 can't build", ORDER), ["T004"])
        resume = HEAD.replace("| T006 |", "| CPU T006 |") + STATUS + rows()
        self.assertEqual(sv.parse_resume(resume, ORDER)["decisions"][0]["before"], ["T006"])

    def test_blocker_on_a_word_is_shown_without_a_card(self):
        resume = HEAD.replace("## Blockers\n", "## Blockers\n- CPU quota exceeded\n") + STATUS + rows(T001="done")
        s = self.build(resume)
        self.assertEqual([(b["text"], b["cards"]) for b in s["blockers"]], [("CPU quota exceeded", [])])

    def test_status_row_for_an_unknown_id_is_drift_not_a_card(self):
        resume = HEAD + STATUS + rows() + "| CPU | x | doing | - | - | - |\n| T099 | x | todo | - | - | - |\n"
        s = self.build(resume)
        self.assertNotIn("CPU", s["status"])
        self.assertIn("RESUME has a row for CPU, which tasks.md does not define", s["drift"])
        self.assertIn("RESUME has a row for T099, which tasks.md does not define", s["drift"])

    def test_without_the_order_nothing_is_filtered(self):
        self.assertEqual(sv.blocker_subjects("T037 (2026-01-02): vendor"), ["T037"])
        self.assertEqual(sv.blocker_subjects("x before T005"), ["T005"])


class BlockerWording(unittest.TestCase):
    def test_one_card_anywhere(self):
        self.assertEqual(sv.blocker_subjects("waiting on the API key for T004", ORDER), ["T004"])
        self.assertEqual(sv.blocker_subjects("waiting on T004's fixtures.", ORDER), ["T004"])

    def test_aside_in_parentheses_is_not_the_card_held_up(self):
        # a finished card named in passing would otherwise turn the blocker into history and hide it
        self.assertEqual(sv.blocker_subjects("Stripe test key missing (T003 shipped the stub)", ORDER), [])

    def test_two_cards_anywhere_is_ambiguous(self):
        self.assertEqual(sv.blocker_subjects("waiting on T003's result to plan T004", ORDER), [])

    def test_explicit_forms_win(self):
        self.assertEqual(sv.blocker_subjects("T005: waiting on T004's API key", ORDER), ["T005"])
        self.assertEqual(sv.blocker_subjects("T004's key before T006", ORDER), ["T006"])
        self.assertEqual(sv.blocker_subjects("T005 waits on the contract from T004's vendor", ORDER), ["T005"])

    def test_blocker_clears_when_its_card_is_done(self):
        resume = (HEAD.replace("## Blockers\n", "## Blockers\n- waiting on the API key for T004\n")
                  + STATUS + rows(T001="done", T004="done"))
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "specs", "001-demo")
            os.makedirs(os.path.join(folder, "state"))
            tasks = os.path.join(folder, "tasks.md")
            with open(tasks, "w") as handle:
                handle.write(TASKS.replace("{tasks}", tasks).replace("- [ ] T004", "- [x] T004"))
            with open(os.path.join(folder, "state", "RESUME.md"), "w") as handle:
                handle.write(resume)
            self.assertEqual(sv.build(tasks, 24)["blockers"], [])


class Templates(unittest.TestCase):
    def test_placeholder_decision_row_is_not_open(self):
        resume = HEAD.replace("| 1 | Which", "| <n> | <question> | <recommended> | <T0nn> | | |\n| 1 | Which") + STATUS
        self.assertEqual([d["n"] for d in sv.parse_resume(resume, ORDER)["decisions"]], ["1"])

    def test_empty_decision_row_is_skipped(self):
        resume = HEAD.replace("| 1 | Which", "|  |  |  |  |  |  |\n| 1 | Which") + STATUS
        self.assertEqual([d["n"] for d in sv.parse_resume(resume, ORDER)["decisions"]], ["1"])


class Structure(unittest.TestCase):
    def test_repeated_section_adds_to_the_first(self):
        resume = (HEAD + "## Status\n| card | title | status | branch | commit | date |\n"
                  "| --- | --- | --- | --- | --- | --- |\n| T001 | x | done | - | - | - |\n\n## Notes\nx\n\n"
                  "## Status\n| card | title | status | branch | commit | date |\n"
                  "| --- | --- | --- | --- | --- | --- |\n| T002 | x | doing | - | - | - |\n")
        status = sv.parse_resume(resume, ORDER)["status"]
        self.assertEqual((status["T001"]["status"], status["T002"]["status"]), ("done", "doing"))

    def test_a_cards_first_row_counts_like_the_writer(self):
        # autopilot.set_row edits a card's first row; reading the same one keeps the two in step
        resume = HEAD + STATUS + rows(T002="doing") + "| T002 | x | todo | - | - | - |\n"
        self.assertEqual(sv.parse_resume(resume, ORDER)["status"]["T002"]["status"], "doing")

    def test_second_table_header_is_not_a_row(self):
        lines = ["| a | b |", "| --- | --- |", "| 1 | 2 |", "", "notes", "| c | d |", "| --- | --- |", "| 3 | 4 |"]
        self.assertEqual(sv.table(lines), [{"a": "1", "b": "2"}])
        self.assertEqual(sv.tables(lines), [(["a", "b"], [{"a": "1", "b": "2"}]), (["c", "d"], [{"c": "3", "d": "4"}])])

    def test_short_row_still_counts(self):
        resume = HEAD + STATUS + rows() + "| T099 | Login |\n"
        self.assertIn("T099", sv.parse_resume(resume, ORDER)["status"])

    def test_last_deploy_lock_written_counts(self):
        resume = HEAD + "## Deploy lock\nT003 (deploying)\n\n" + STATUS + rows()
        parsed = sv.parse_resume(resume, ORDER)
        self.assertEqual((parsed["lock"], parsed["lock_free"]), ("T003 (deploying)", False))
        empty = HEAD.replace("## Deploy lock\nfree\n", "## Deploy lock\n\n") + STATUS
        self.assertEqual(sv.parse_resume(empty, ORDER)["lock"], "")

    def test_other_table_under_status_is_ignored(self):
        resume = HEAD + STATUS + rows() + "\n| note | when |\n| --- | --- |\n| T005 moved | today |\n"
        status = sv.parse_resume(resume, ORDER)["status"]
        self.assertEqual(sorted(status), sorted(ORDER))

    def test_blank_line_inside_a_table_continues_it(self):
        lines = ["| card | status |", "| --- | --- |", "| T001 | done |", "", "| T002 | doing |"]
        self.assertEqual(sv.table(lines), [{"card": "T001", "status": "done"}, {"card": "T002", "status": "doing"}])


class Commits(Built):
    def test_recorded_commit_is_the_whole_hash(self):
        git = ["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "unrelated subject"], check=True)
        sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cards, _ = sv.parse_tasks(TASKS)
        commits = sv.recent_commits(os.path.dirname(self.tasks), cards, {"T002": {"commit": f"merged {sha[:7]}"}})
        self.assertEqual([c["card"] for c in commits], ["T002"])
        # a row for a card tasks.md does not define owns no commit
        commits = sv.recent_commits(os.path.dirname(self.tasks), cards, {"CPU": {"commit": sha[:7]}})
        self.assertEqual([c["card"] for c in commits], [""])


class DeployLock(Built):
    def test_free_with_a_note_is_free(self):
        for body in ("free", "Free", "**free**", "`free` (released by T003)", "free (released by T003)", "free."):
            self.assertTrue(sv.lock_free(body), body)
        for body in ("T003", "held by T003", "T003 (deploying) — free after", "freeze until Monday"):
            self.assertFalse(sv.lock_free(body), body)
        self.assertTrue(sv.lock_free(""))

    def test_released_lock_is_no_drift_and_not_shown(self):
        resume = (HEAD.replace("## Deploy lock\nfree", "## Deploy lock\nfree (released by T003)")
                  + STATUS + rows(T001="done", T003="done"))
        s = self.build(resume)
        self.assertTrue(s["lock_free"])
        self.assertFalse(any("deploy lock" in d for d in s["drift"]), s["drift"])
        self.assertNotIn("Deploy lock:", sv.render(s))

    def test_held_lock_by_a_finished_card_is_drift(self):
        resume = (HEAD.replace("## Deploy lock\nfree", "## Deploy lock\nT003 (deploying)")
                  + STATUS + rows(T001="done", T003="done"))
        s = self.build(resume)
        self.assertFalse(s["lock_free"])
        self.assertIn("the deploy lock is still held by T003, which is done", s["drift"])
        self.assertIn("Deploy lock: T003 (deploying)", sv.render(s))


class RunsAs(unittest.TestCase):
    def test_trailing_note_after_the_launcher(self):
        self.assertEqual(sv.runs_as("**Runs as:** me@example.com via `~/.local/bin/claude-work` (work account)\n"),
                         {"email": "me@example.com", "launcher": "~/.local/bin/claude-work", "app_url": ""})

    def test_plain_forms_still_parse(self):
        self.assertEqual(sv.runs_as("**Runs as:** me@example.com via `~/bin/cw`\n")["launcher"], "~/bin/cw")
        self.assertEqual(sv.runs_as("**Runs as:** `me@example.com` via ~/bin/cw.\n")["launcher"], "~/bin/cw")
        self.assertEqual(sv.runs_as("**Runs as**: me@example.com\n"),
                         {"email": "me@example.com", "launcher": "", "app_url": ""})

    def test_note_before_via_and_after_email(self):
        found = sv.runs_as("**Runs as:** me@example.com (work) via `~/bin/cw` — set up on the laptop\n")
        self.assertEqual((found["email"], found["launcher"]), ("me@example.com", "~/bin/cw"))

    def test_via_in_a_note_is_not_the_launcher(self):
        found = sv.runs_as("**Runs as:** me@example.com (reached via Slack) via `~/bin/cw`\n")
        self.assertEqual(found["launcher"], "~/bin/cw")
        self.assertEqual(sv.runs_as("**Runs as:** me@example.com — set up via the onboarding doc\n")["launcher"], "")

    def test_launcher_with_arguments_is_not_taken(self):
        # one argv word only: "claude --profile work" would be run as a single program name
        found = sv.runs_as("**Runs as:** me@example.com via `claude --profile work`\n")
        self.assertEqual((found["email"], found["launcher"]), ("me@example.com", ""))

    def test_via_on_the_next_line_is_not_the_launcher(self):
        found = sv.runs_as("**Runs as:** me@example.com\n\nRun it via `make deploy`.\n")
        self.assertEqual((found["email"], found["launcher"]), ("me@example.com", ""))


if __name__ == "__main__":
    unittest.main()
