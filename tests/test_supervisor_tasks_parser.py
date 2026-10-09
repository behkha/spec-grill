"""Tests for supervisor.py's tasks.md parser: lenient headings, checklist and meta lines, and the drift
it reports for what it cannot use (loops, self-dependencies, duplicates, unparsed headings).

    python3 -m pytest -q tests/test_supervisor_tasks_parser.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import supervisor as sv  # noqa: E402
from test_autopilot import RESUME, TASKS  # noqa: E402

BASE = "# Tasks: X\n\n## 4. Cards\n\n- [ ] T001 One\n- [ ] T002 Two\n\n"


def drift_of(text: str, status: dict = None) -> list:
    cards, order = sv.parse_tasks(text)
    return sv.tasks_drift(text, cards, order, status or {})


class Feature(unittest.TestCase):
    """The fixture feature (TASKS and RESUME), edited, read through build() as the dashboard reads it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="parser-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        folder = os.path.join(self.root, "specs", "001-demo")
        os.makedirs(os.path.join(folder, "state"))
        self.tasks = os.path.join(folder, "tasks.md")
        self.text = TASKS.replace("{tasks}", self.tasks)
        with open(os.path.join(folder, "state", "RESUME.md"), "w") as handle:
            handle.write(RESUME)

    def build(self, text: str) -> dict:
        with open(self.tasks, "w") as handle:
            handle.write(text)
        return sv.build(self.tasks, 4)

    def test_the_fixture_shows_no_parser_drift(self):
        s = self.build(self.text)
        self.assertEqual(drift_of(self.text), [])
        self.assertFalse([d for d in s["drift"] if "cycle" in d or "itself" in d or "defined" in d
                          or "heading" in d], s["drift"])

    def test_a_cycle_is_drift(self):
        text = self.text.replace("fulfills FR-1 · after: T001 (beside T003)", "fulfills FR-1 · after: T004")
        text = text.replace("fulfills FR-3 · after: T001", "fulfills FR-3 · after: T002")
        s = self.build(text)
        self.assertIn("after: cycle T002 → T004 → T002 (each waits for the next): none of them can start", s["drift"])
        self.assertEqual(sum("cycle" in d for d in s["drift"]), 1, "one loop, reported once")

    def test_a_cycle_of_finished_cards_is_history(self):
        text = BASE + "#### T001 — One\nafter: T002 · S\n\n#### T002 — Two\nafter: T001 · S\n"
        self.assertTrue([d for d in drift_of(text) if "cycle" in d])
        self.assertFalse([d for d in drift_of(text, {"T001": "done", "T002": "waived"}) if "cycle" in d])
        self.assertFalse([d for d in drift_of(text, {"T001": "done", "T002": "todo"}) if "cycle" in d],
                         "T002 waits only on a finished card: it can start")

    def test_overlapping_cycles_are_each_reported(self):
        text = ("# X\n\n- [ ] T001 a\n- [ ] T002 b\n- [ ] T003 c\n\n#### T001 — a\nafter: T002 · S\n\n"
                "#### T002 — b\nafter: T001, T003 · S\n\n#### T003 — c\nafter: T002 · S\n")
        loops = [d for d in drift_of(text) if "cycle" in d]
        self.assertEqual(len(loops), 2, loops)
        self.assertEqual([d for d in drift_of(text, {"T001": "done"}) if "cycle" in d],
                         ["after: cycle T002 → T003 → T002 (each waits for the next): none of them can start"])

    def test_a_longer_cycle_reads_from_its_first_card(self):
        text = ("# X\n\n- [ ] T001 a\n- [ ] T002 b\n- [ ] T003 c\n\n#### T001 — a\nafter: — · S\n\n"
                "#### T002 — b\nafter: T003 · S\n\n#### T003 — c\nafter: T001, T002 · S\n")
        self.assertEqual(drift_of(text),
                         ["after: cycle T002 → T003 → T002 (each waits for the next): none of them can start"])

    def test_a_card_naming_itself_is_dropped_and_reported(self):
        text = self.text.replace("fulfills FR-3 · after: T001 · S", "fulfills FR-3 · after: T001, T004 · S · blocks: T004")
        s = self.build(text)
        self.assertEqual(s["cards"]["T004"]["after"], ["T001"])
        self.assertIn("T004 names itself in after:; ignored", s["drift"])
        self.assertIn("T004 names itself in blocks:; ignored", s["drift"])
        self.assertFalse([d for d in s["drift"] if "cycle" in d], s["drift"])

    def test_a_heading_with_a_hyphen_or_colon_is_a_card_heading(self):
        for sep in (" -", ":", " –", " :"):
            text = self.text.replace("#### T004 — Storage", f"#### T004{sep} Storage")
            s = self.build(text)
            self.assertEqual((s["cards"]["T004"]["after"], s["cards"]["T004"]["size"], s["cards"]["T004"]["title"]),
                             (["T001"], "S", "Storage"), sep)
            self.assertFalse([d for d in s["drift"] if "heading" in d], s["drift"])

    def test_an_unparsed_card_heading_is_drift(self):
        s = self.build(self.text.replace("#### T004 — Storage", "#### T004 Storage"))
        self.assertIn("'#### T004 Storage' is not read as a card heading: write it `#### T004 — <title>`, "
                      "or its after:, size and fields are ignored", s["drift"])

    def test_a_range_heading_is_no_card_heading_and_no_drift(self):
        text = self.text.replace("### Stage 2 — Build", "### T002 – T004 Build")
        s = self.build(text)
        self.assertEqual(s["cards"]["T002"]["title"], "Orders API")
        self.assertFalse([d for d in s["drift"] if "heading" in d or "defined" in d], s["drift"])

    def test_a_checklist_note_inside_a_card_is_not_that_cards_line(self):
        text = self.text.replace("after: T002–T004 · S · effort high · kind fullstack\n",
                                 "after: T002–T004 · S · effort high · kind fullstack\n  - [x] T002 reviewed\n"
                                 "  - [ ] T099 nothing\n")
        s = self.build(text)
        self.assertNotIn("T099", s["cards"])
        self.assertFalse(s["cards"]["T002"]["ticked"])
        self.assertFalse([d for d in s["drift"] if "defined" in d or "T099" in d], s["drift"])

    def test_a_duplicate_card_is_drift(self):
        text = self.text.replace("#### T006 — Settings screen", "#### T005 — Settings screen")
        s = self.build(text)
        self.assertIn("T005 is defined twice (card headings); they are read as one card", s["drift"])
        text = self.text.replace("- [ ] T006 Settings screen", "- [ ] T005 Settings screen")
        self.assertIn("T005 is defined twice (checklist lines); they are read as one card", drift_of(text))


class Lines(unittest.TestCase):
    def test_id_regex_is_linear_and_keeps_its_language(self):
        start = time.time()
        for n in (24, 40, 4000):
            self.assertEqual(sv.ID_RE.findall("T1" + "A" * n + "a"), [])
        self.assertLess(time.time() - start, 0.1)
        self.assertEqual(sv.ID_RE.findall("T001 T012A T042B2 T042R2A CPA CP0 CPEND T0nn T12a"),
                         ["T001", "T012A", "T042B2", "T042R2A", "CPA", "CP0", "CPEND"])

    def test_bold_meta_line_is_read(self):
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\n**after:** T001 · **S** · **effort** high · **kind:** backend\n\n"
                                         "**Start with:** `Go · T002.`\n**Touches:** `api/x.ts`.\n")
        c = cards["T002"]
        self.assertEqual((c["after"], c["size"], c["effort"], c["kind"], c["start_with"], c["touches"]),
                         (["T001"], "S", "high", "backend", "Go · T002.", ["api/x.ts"]))

    def test_capitalised_meta_fields_are_read(self):
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\n**After:** T001 · **Effort:** High · **Size:** M\n")
        c = cards["T002"]
        self.assertEqual((c["after"], c["effort"], c["size"]), (["T001"], "high", "M"))

    def test_bold_fields_still_end_the_meta_lines(self):
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\n\n**Start with:** `X · after: T001 · L ·`\n")
        self.assertEqual((cards["T002"]["after"], cards["T002"]["size"]), ([], ""))

    def test_nested_brackets_are_no_dependency(self):
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: T001 (beside (x) T004), (T036 if German) · S\n")
        self.assertEqual((cards["T002"]["after"], cards["T002"]["conditional"]), (["T001", "T036"], ["T036"]))
        self.assertEqual(sv.ids_in("T001 (a (b (c) T004) T005) T006", ["T001", "T006"]), ["T001", "T006"])
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: T001 (T036 (de only) if German) · S\n")
        self.assertEqual(cards["T002"]["conditional"], ["T036"], "a nested bracket keeps its condition")

    def test_a_comma_list_stays_with_its_phase_until_an_id_is_local(self):
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: phase 11's T034, T035 · S\n")
        self.assertEqual(cards["T002"]["after"], [])
        self.assertEqual([d["card"] for d in cards["T002"]["external"]], ["T034", "T035"])
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: phase 11's T034, T001 · S\n")
        self.assertEqual(cards["T002"]["after"], ["T001"], "T001 is this feature's")
        self.assertEqual([d["card"] for d in cards["T002"]["external"]], ["T034"])
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: phase 11's T034, T035, T001, T036 · S\n")
        self.assertEqual((cards["T002"]["after"], [d["card"] for d in cards["T002"]["external"]]),
                         (["T001", "T036"], ["T034", "T035"]), "the list ends at the first local id")
        cards, _ = sv.parse_tasks(BASE + "#### T002 — Two\nafter: phase 11's T021–T023, CPD · S\n")
        self.assertEqual(cards["T002"]["after"], ["CPD"], "a checkpoint after a card list is this feature's")
        self.assertEqual([(d["card"], d.get("through")) for d in cards["T002"]["external"]], [("T021", "T023")])

    def test_a_card_parallel_after_its_title(self):
        cards, _ = sv.parse_tasks("# X\n\n- [ ] T012 Title [P] — fulfills FR-1\n- [ ] T013 Other\n")
        self.assertEqual((cards["T012"]["parallel"], cards["T012"]["title"]), (True, "Title"))
        self.assertFalse(cards["T013"]["parallel"])

    def test_checklist_lines_are_read_leniently(self):
        for line in ("- [ ] T012: Title", "- [ ]\tT012 Title", "  - [ ] T012 Title", "* [ ] T012 Title",
                     "- [ ] T012 [P] Title", "- [ ] T012: [P] Title"):
            cards, order = sv.parse_tasks("# X\n\n" + line + "\n")
            self.assertEqual((order, cards["T012"]["title"]), (["T012"], "Title"), line)
        self.assertTrue(sv.parse_tasks("# X\n\n- [ ] T012 [P] Title\n")[0]["T012"]["parallel"])
        self.assertEqual(sv.parse_tasks("# X\n\n- T012 Title\n- [ ] T012abc\n")[1], [])

    def test_an_indented_checklist_line_starts_its_own_inline_card(self):
        text = ("## 5. Backlog\n\n  - [ ] T003B First\n    after: T001 · S\n    **Touches:** `a/x.ts`.\n"
                "  - [ ] T003C Second\n    after: T003B · M\n    **Touches:** `b/y.ts`.\n")
        cards, order = sv.parse_tasks(text)
        self.assertEqual(order, ["T003B", "T003C"])
        self.assertEqual((cards["T003B"]["touches"], cards["T003C"]["after"], cards["T003C"]["size"]),
                         (["a/x.ts"], ["T003B"], "M"))

    def test_batch_parallel_after_its_name(self):
        table = "| batch | name | cards | effort | Start with |\n|---|---|---|---|---|\n| B3 | x | T001 | | `s` |\n\n"
        for line, parallel, ticked in (("- [ ] B3 name [P]", True, False), ("- [ ] B3 [P] name", True, False),
                                       ("- [ ] B3 name", False, False), ("  * [x] B3: name", False, True)):
            text = table + line + "\n- [ ] T001 One\n"
            cards, order = sv.parse_tasks(text)
            b = sv.parse_batches(text, cards, order)[0][0]
            self.assertEqual((b["parallel"], b["ticked"], b["name"]), (parallel, ticked, "x"), line)

    def test_the_template_shows_no_parser_drift(self):
        skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        block = skill.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
        self.assertEqual(drift_of(block), [])

    def test_a_long_checklist_parses_in_linear_time(self):
        text = "# X\n\n" + "".join(f"- [ ] T{i:05d} card\n" for i in range(1, 20001)) + "filler\n" * 5000
        start = time.time()
        self.assertEqual(len(sv.parse_tasks(text)[1]), 20000)
        self.assertLess(time.time() - start, 2)


class DefaultFeature(unittest.TestCase):
    def test_the_newest_feature_is_picked_by_number(self):
        root = tempfile.mkdtemp(prefix="pick-")
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("9-a", "10-b", "notes"):
            os.makedirs(os.path.join(root, "specs", name))
            open(os.path.join(root, "specs", name, "tasks.md"), "w").close()
        here = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, here)
        self.assertEqual(os.path.basename(os.path.dirname(sv.find_tasks(None))), "10-b")


if __name__ == "__main__":
    unittest.main()
