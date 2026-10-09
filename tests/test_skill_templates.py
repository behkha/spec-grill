"""SKILL.md's frontmatter and templates pass the checks SKILL.md itself sets (Phase 4's "Checks before
presenting"), so a tasks.md drafted from them starts out clean."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import supervisor as sv  # noqa: E402

SKILL = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
FIELDS = ("Start with", "Read", "Do", "Done when", "Verify", "Touches")


def template(name: str) -> str:
    """A fenced template under `### <name>` in SKILL.md's Templates section."""
    fence = "````markdown" if name == "tasks.md" else "```markdown"
    return SKILL.split(f"### {name}\n", 1)[1].split(fence, 1)[1].split("\n" + fence[:-8], 1)[0]


def template_cards() -> dict:
    """§4's cards in the tasks.md template, by id, with their bodies."""
    cards = template("tasks.md").split("## 4. Cards", 1)[1].split("## 5. Backlog", 1)[0]
    parts = re.split(r"^#### (\S+)(?: \[P\])? — .*$", cards, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


class Frontmatter(unittest.TestCase):
    def test_description_fits_and_does_not_contradict_itself(self):
        description = re.search(r"^description: (.+)$", SKILL.split("---", 2)[1], re.M).group(1)
        self.assertLess(len(description), 1000, "the limit is 1,024 characters; keep room to spare")
        self.assertNotIn("ONLY when", description)
        self.assertNotIn("Also use it when", description)
        for trigger in ("spec this out", "grill me", "spec-driven development", "names this skill",
                        "show the dashboard", "start the supervisor", "run the cards on autopilot"):
            self.assertIn(trigger, description)


class Templates(unittest.TestCase):
    def test_every_template_card_has_the_required_fields(self):
        cards = template_cards()
        self.assertEqual(list(cards), ["T001", "CP0", "T002", "T003", "T004", "CPA", "T009", "CPEND"])
        for cid, body in cards.items():
            meta = body.strip().split("\n", 1)[0]
            self.assertRegex(meta, r"·\s*kind (backend|frontend|fullstack|owner)\b", cid)
            for field in FIELDS:
                self.assertRegex(body, rf"\*\*{field}:\*\*", f"{cid} has no {field}")
            self.assertNotIn("see above", body, cid)

    def test_every_template_card_starts_with_its_own_line(self):
        for cid, body in template_cards().items():
            self.assertIn(f"**Start with:** `<Feature> · {cid}. Follow <absolute path>/tasks.md §1, then card {cid}.`",
                          body)

    def test_filled_template_parses_with_kinds_and_start_lines(self):
        cards, order = sv.parse_tasks(template("tasks.md"))
        self.assertEqual(order, ["T001", "CP0", "T002", "T003", "T004", "CPA", "T009", "CPEND"])
        for cid in order:
            self.assertTrue(cards[cid]["kind"], cid)
            self.assertTrue(cards[cid]["start_with"], cid)
        self.assertEqual(cards["CPEND"]["after"], ["T009"], "the feature is done only after its results")

    def test_first_cards_of_a_stage_name_the_previous_checkpoint(self):
        cards, order = sv.parse_tasks(template("tasks.md"))
        unchecked = []
        for i, cid in enumerate(order):
            anchors = [j for j, x in enumerate(order[:i]) if x.startswith("CP") or x == "T001"]
            if not anchors or not cards[cid]["after"]:
                unchecked.append(cid)  # T001, or an after: written as a placeholder
                continue
            latest = anchors[-1]
            self.assertTrue(any(d in order and order.index(d) >= latest for d in cards[cid]["after"]),
                            f"{cid} waits on {cards[cid]['after']}, not on {order[latest]} or a card after it,"
                            " so the stage review would not hold it")
        self.assertEqual(unchecked, ["T001", "T009"], "only T001 and the Results card's placeholder after:")

    def test_spec_and_constitution_templates(self):
        spec = template("spec.md")
        for ident in ("FR-1", "NFR-1", "SC-1", "UX-1"):
            self.assertRegex(spec, rf"(?m)^- {ident}\b", ident)
        self.assertIn("## Open Questions", template("constitution.md"))

    def test_backlog_ids_for_checkpoint_findings_parse(self):
        for cid in ("T004B", "T004C", "T004B2"):
            self.assertRegex(cid, rf"^{sv.ID}$")
        backlog = re.sub(r"\s+", " ", template("tasks.md").split("## 5. Backlog", 1)[1].split("## 6.", 1)[0])
        self.assertIn("only the checkpoint session numbers them", backlog)

    def test_card_rename_hook_path_is_not_hard_coded(self):
        handoff = SKILL.split("## Phase 5", 1)[1].split("## Supervisor mode", 1)[0]
        self.assertNotIn("~/.claude/skills/spec-grill/hooks/card-rename.py", handoff)
        self.assertIn('"python3 <skill dir>/hooks/card-rename.py"', handoff)


if __name__ == "__main__":
    unittest.main()
