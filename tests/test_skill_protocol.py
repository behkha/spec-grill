"""SKILL.md's session protocol says what the hooks parse: the end lines, blockers, the locks, the close.

    python3 -m pytest -q tests/test_skill_protocol.py
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402

SKILL = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
BLOCK = SKILL.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
FLAT = re.sub(r"\s+", " ", SKILL)


def item(title: str) -> str:
    """One item of the template's §1, by its title, on one line."""
    flat = re.sub(r"\s+", " ", BLOCK)
    return re.search(rf"\d+\. \*\*{re.escape(title)}\*\*(.*?)(?= \d+\. \*\*|$)", flat).group(1)


def resume_template() -> str:
    return BLOCK.split("**RESUME**", 1)[1].split("```markdown", 1)[1].split("```", 1)[0]


class EndLines(unittest.TestCase):
    def test_skill_lists_every_end_line_the_autopilot_tells_sessions(self):
        prompts = autopilot.RULES + autopilot.PARALLEL_RULES
        markers = {f"AUTOPILOT: {m}" for m in re.findall(r"AUTOPILOT: ((?:WAITING FOR )?[A-Z]+)", prompts)}
        self.assertEqual(markers, {"AUTOPILOT: DONE", "AUTOPILOT: BLOCKED", "AUTOPILOT: SPLIT",
                                   "AUTOPILOT: WAITING FOR APPROVAL", "AUTOPILOT: WAITING FOR DECISION"})
        autopilot_section = SKILL.split("### Autopilot", 1)[1].split("## Templates", 1)[0]
        for marker in markers:
            self.assertIn(f"`{marker}", autopilot_section, marker)

    def test_one_approval_id_scheme(self):
        # the rules number approvals <card>.<n>; the skill's end line uses the same id
        self.assertIn("`<card>.<n>` (T012.1", autopilot.RULES)
        self.assertIn("`AUTOPILOT: WAITING FOR APPROVAL <card>.<n>`", SKILL)
        self.assertNotIn("APPROVAL A<n>", SKILL)
        resume = resume_template().replace(
            "| # | card | step | why | status | answer |",
            "| # | card | step | why | status | answer |\n| --- | --- | --- | --- | --- | --- |\n"
            "| T012.1 | T012 | deploy | verify | pending | |", 1)
        approvals = sv.parse_resume(resume)["approvals"]
        self.assertEqual([(a["n"], a["card"]) for a in approvals], [("T012.1", "T012")])
        self.assertTrue(sv.WAITED.search("AUTOPILOT: WAITING FOR APPROVAL T012.1"))


class Split(unittest.TestCase):
    def test_the_split_is_the_one_exception_everywhere(self):
        rule12 = re.search(r"12\. \*\*No follow-up cards from a card\.\*\*(.*?) 13\. \*\*", FLAT).group(1)
        self.assertIn("The one exception is a **split**", rule12)
        self.assertIn("`AUTOPILOT: SPLIT`", rule12)
        self.assertIn("a split under the Context budget item adds the one remainder card", item("Stay in scope; one fix, no follow-up cards; checks are fixed."))
        budget = item("Context budget.")
        for words in ("tick the", "original card done", "whose `after:` names the original card", "`AUTOPILOT: SPLIT`"):
            self.assertIn(words, budget)
        self.assertIn("A split counts as finished", FLAT)


class Paths(unittest.TestCase):
    def test_state_paths_are_the_feature_folder_by_absolute_path(self):
        where = item("Where things live.")
        self.assertIn("Every `state/…` path in this file (§1–§5) means that folder, read and written by its absolute path", where)
        self.assertNotIn("main merged in first", where)
        self.assertIn("merge the integration branch into your branch", where)
        self.assertNotIn("waits for the next checkpoint", where, "a [P] card merges under the lock, like a batch")
        self.assertIn("never tick it while the branch waits", where)
        self.assertIn("`mkdir <feature folder>/state/merge.lock`", SKILL)
        self.assertNotIn("(`mkdir state/merge.lock`;", SKILL)

    def test_the_lock_the_rules_name_is_the_one_the_supervisor_reads(self):
        root = tempfile.mkdtemp(prefix="skill-lock-")
        self.addCleanup(shutil.rmtree, root, True)
        tasks = os.path.join(root, "specs", "001-x", "tasks.md")
        lock = autopilot.merge_lock_path(tasks)
        self.assertTrue(os.path.isabs(lock))
        os.makedirs(lock)
        with open(os.path.join(lock, "holder"), "w") as handle:
            handle.write("B2 2026-01-01T00:00Z")
        self.assertEqual(sv.merge_lock(os.path.dirname(lock))["holder"], "B2")


class Resume(unittest.TestCase):
    def test_blockers_in_the_documented_forms_attach_to_their_card(self):
        precond = item("Preconditions.")
        self.assertIn("`- T0nn: <what is missing>`", precond)
        self.assertIn("`- <what is missing> before T0nn`", precond)
        resume = resume_template()
        self.assertEqual(sv.parse_resume(resume)["blockers"], [], "the template's placeholder blocks nobody")
        resume = resume.replace("## Blockers\n", "## Blockers\n- T005: the vendor's API key\n"
                                "- the owner's pick of a theme before T007\n", 1)
        self.assertEqual([b["cards"] for b in sv.parse_resume(resume)["blockers"]], [["T005"], ["T007"]])

    def test_deploy_lock_format(self):
        self.assertIn("`held by T0nn since <UTC time>`", item("Owner's yes."))
        self.assertIn("exactly `free`", item("Owner's yes."))
        resume = resume_template()
        self.assertEqual(sv.parse_resume(resume)["lock"], "free")
        held = resume.replace("## Deploy lock\nfree", "## Deploy lock\nheld by T002 since 2026-01-01T00:00Z", 1)
        self.assertEqual(sv.ID_RE.search(sv.parse_resume(held)["lock"]).group(0), "T002")


class Rules(unittest.TestCase):
    def test_t001_is_exempt_from_the_finish_commit(self):
        finish = item("Finish.")
        self.assertIn("T001 is the exception: it commits nothing", finish)

    def test_rename_in_the_cli_asks_the_user(self):
        self.assertIn("ask the user to run `/rename <name>`", item("One card or one batch per session."))

    def test_results_waits_for_the_same_thing_in_rule_16_and_the_template(self):
        said = "the last rollout card, or the last checkpoint when there is no rollout"
        self.assertIn(said.replace("the last rollout card", "last rollout card"), BLOCK)
        self.assertIn(said, FLAT)
        results = re.search(r"#### T009 — Results\n(after: [^\n]*)", BLOCK).group(1)
        self.assertIn("§5", results)
        text = ("# Tasks: X\n\n## 4. Cards\n\n- [x] CPA Stage\n- [ ] T009 Results\n\n"
                "#### T009 — Results\n" + results.replace("<last rollout card, or the last checkpoint when there is"
                                                         " no rollout>", "CPA") +
                "\n\n## 5. Backlog\n\n- [ ] T003B Fix\n  after: CPA · S · effort low · kind frontend\n")
        cards, _ = sv.parse_tasks(text)
        self.assertEqual(cards["T009"]["after"], ["CPA", "T003B"])


if __name__ == "__main__":
    unittest.main()
