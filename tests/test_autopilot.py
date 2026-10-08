"""Tests for hooks/autopilot.py against a fake `claude` (tests/fake_claude.py).

    python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402

TASKS = """# Tasks: Demo

## 1. Session protocol

1. **One card per session.** First action: rename the session to `<NNN> T0nn <card title>`.

## 4. Cards

- [ ] T001 Re-verify and baseline
- [ ] T002 [P] Orders API — fulfills FR-1
- [ ] T003 [P] Login screen — fulfills FR-2
- [ ] T004 Storage — fulfills FR-3
- [ ] CPA Stage A merged and pinned
- [ ] T005 Sign the vendor contract
- [ ] T006 Settings screen — fulfills FR-4
- [ ] CPEND Feature done

### Stage 1 — Baseline

#### T001 — Re-verify and baseline
fulfills baselines · after: — · M · effort high · kind backend

**Start with:** `Demo · T001. Follow {tasks} §1, then card T001.`

### Stage 2 — Build

#### T002 [P] — Orders API
fulfills FR-1 · after: T001 (beside T003) · S · effort medium · kind backend

**Start with:** `Demo · T002. Follow {tasks} §1, then card T002.`

#### T003 [P] — Login screen
fulfills FR-2 · after: T001 · S · effort high · kind frontend · model sonnet

**Start with:** `Demo · T003. Follow {tasks} §1, then card T003.`

#### T004 — Storage
fulfills FR-3 · after: T001 · S · effort medium · kind backend

**Start with:** `Demo · T004. Follow {tasks} §1, then card T004.`

#### CPA — Stage A merged and pinned
after: T002–T004 · S · effort high · kind fullstack

**Start with:** `Demo · CPA. Follow {tasks} §1, then card CPA.`

### Stage 3 — Finish

#### T005 — Sign the vendor contract
after: CPA · S · kind owner

**Do:** the owner signs.

#### T006 — Settings screen
fulfills FR-4 · after: CPA · S · effort high · kind frontend

**Start with:** `Demo · T006. Follow {tasks} §1, then card T006.`

#### CPEND — Feature done
after: T005, T006 · S · effort low · kind fullstack

**Start with:** `Demo · CPEND. Follow {tasks} §1, then card CPEND.`

## 6. Supervisor

**Never unattended:** `Bash(git push:*)`, `Bash(fly deploy:*)`.
"""

RESUME = """# Demo - resume

## Deploy lock
free

## Decisions (spec Open Questions)
| # | question | recommended | needed before | answer | by, when |
| --- | --- | --- | --- | --- | --- |
| 1 | Which payment provider? | Stripe | T006 | | |

## Blockers

## Approvals
| # | card | step | why | status | answer |
| --- | --- | --- | --- | --- | --- |

## Status
| card | title | status | branch | commit | date (UTC) |
| --- | --- | --- | --- | --- | --- |
""" + "".join(f"| {c} | x | todo | - | - | - |\n" for c in
              ["T001", "T002", "T003", "T004", "CPA", "T005", "T006", "CPEND"])


class Feature(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="autopilot-test-")
        folder = os.path.join(self.root, "specs", "001-demo")
        os.makedirs(os.path.join(folder, "state"))
        self.tasks = os.path.join(folder, "tasks.md")
        with open(self.tasks, "w") as handle:
            handle.write(TASKS.replace("{tasks}", self.tasks))
        with open(os.path.join(folder, "state", "RESUME.md"), "w") as handle:
            handle.write(RESUME)
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.calls = os.path.join(self.root, "calls.jsonl")
        self.script = os.path.join(self.root, "script.json")
        wrapper = os.path.join(self.root, "claude")
        with open(wrapper, "w") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "fake_claude.py")}" "$@"\n')
        os.chmod(wrapper, 0o755)
        os.environ.update(FAKE_TASKS=self.tasks, FAKE_CALLS=self.calls, FAKE_SCRIPT=self.script)
        self.script_for({})
        autopilot.change_settings(self.tasks, auto=True, claude=wrapper, notify=False, max_parallel=3)

    def tearDown(self):
        for run in autopilot.registry(self.tasks)["runs"]:
            if not run["ended"]:
                autopilot.kill(run)
        autopilot.release(self.tasks)
        shutil.rmtree(self.root, ignore_errors=True)

    def script_for(self, script: dict):
        with open(self.script, "w") as handle:
            json.dump(script, handle)

    def launched(self) -> list:
        if not os.path.exists(self.calls):
            return []
        return [json.loads(line) for line in open(self.calls)]

    def wait_calls(self, n: int) -> list:
        """The fake sessions write their call as they start; wait until n have."""
        for _ in range(100):
            if len(self.launched()) >= n:
                break
            time.sleep(0.1)
        return self.launched()

    def settle(self, steps: int = 40) -> list:
        """Run the dispatcher until no session is live and a pass changes nothing."""
        events = []
        for _ in range(steps):
            got = autopilot.step(self.tasks)
            events += got
            if not autopilot.live(autopilot.registry(self.tasks)) and not got:
                return events
            time.sleep(0.15)
        self.fail(f"did not settle: {events}")

    def state(self) -> dict:
        return sv.build(self.tasks, 4)

    def test_runs_the_feature_through_approval_gate_and_owner_card(self):
        self.script_for({"T003": ["approval", "done"]})
        autopilot.step(self.tasks)
        first = self.wait_calls(1)
        self.assertEqual([c["card"] for c in first], ["T001"], "only T001 is ready at the start")
        args = first[0]["args"]
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("-n") + 1], "001 T001 Re-verify and baseline")
        deny = json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]
        self.assertEqual(deny, ["Bash(git push:*)", "Bash(fly deploy:*)"])
        self.assertIn("Follow", args[-1])

        self.settle()
        s = self.state()
        cards = [c["card"] for c in self.launched()]
        self.assertEqual(cards[:4], ["T001", "T002", "T003", "T004"], "T002–T004 run side by side after T001")
        t003 = [c for c in self.launched() if c["card"] == "T003"][0]["args"]
        self.assertEqual(t003[t003.index("--model") + 1], "sonnet")
        self.assertEqual([a["n"] for a in s["approvals"]], ["A1"], "T003 waits for the owner's yes")
        self.assertNotIn("CPA", cards, "CPA waits for T003")

        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T003", "verdict": "approved", "note": "go"})
        self.settle()
        resumed = [c for c in self.launched() if c["card"] == "T003"][1]["args"]
        self.assertIn("--resume", resumed)
        self.assertIn("approved", resumed[-1])
        s = self.state()
        self.assertEqual(s["status"]["T003"], "done")
        self.assertEqual(s["status"]["CPA"], "done")
        self.assertEqual(s["gates"], ["CPA"], "the next stage waits for the owner's review of CPA")
        self.assertNotIn("T006", [c["card"] for c in self.launched()])

        autopilot.act(self.tasks, "gate", {"gate": "CPA"})
        autopilot.act(self.tasks, "decision", {"n": "1", "answer": "Stripe"})
        self.settle()
        self.assertIn("T006", [c["card"] for c in self.launched()])
        self.assertNotIn("T005", [c["card"] for c in self.launched()], "an owner's card is never launched")
        self.assertEqual(self.state()["yours"], ["T005"])

        autopilot.act(self.tasks, "owner-done", {"card": "T005"})
        self.settle()
        s = self.state()
        self.assertEqual(len(s["done"]), 8, s["status"])
        self.assertIn("- [x] T005", open(self.tasks).read())
        self.assertRegex(open(s["resume"]).read(), r"\| 1 \| Which payment provider\? \| Stripe \| T006 \| Stripe \| owner, ")

    def test_retries_once_then_hands_the_card_to_the_owner(self):
        self.script_for({"T001": ["doing", "doing", "done"]})
        self.settle()
        runs = [c for c in self.launched() if c["card"] == "T001"]
        self.assertEqual(len(runs), 2)
        self.assertIn("--resume", runs[1]["args"], "a session that did work is resumed, not restarted")
        self.assertIn("T001", self.state()["autopilot"]["attention"])
        autopilot.act(self.tasks, "retry", {"card": "T001"})
        self.settle()
        self.assertEqual(self.state()["status"]["T001"], "done")

    def test_api_error_pauses_without_spending_an_attempt(self):
        self.script_for({"T001": ["apierror", "done"]})
        self.settle()
        s = self.state()
        self.assertFalse(s["autopilot"]["settings"]["auto"])
        self.assertIn("API error", s["autopilot"]["settings"]["paused_reason"])
        self.assertEqual(s["autopilot"]["attention"], {})
        autopilot.act(self.tasks, "settings", {"auto": True})
        autopilot.change_settings(self.tasks, max_parallel=1)
        autopilot.step(self.tasks)
        second = [c for c in self.wait_calls(2) if c["card"] == "T001"][1]["args"]
        self.assertIn("--session-id", second, "a session that never worked starts fresh")

    def test_budget_cap_goes_to_the_owner(self):
        self.script_for({"T001": ["budget"]})
        self.settle()
        self.assertIn("cap", self.state()["autopilot"]["attention"]["T001"])
        self.assertEqual(len(self.launched()), 1)

    def test_stop_kills_the_session(self):
        self.script_for({"T001": ["sleep"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        run = autopilot.registry(self.tasks)["runs"][0]
        self.assertTrue(sv.pid_alive(run["pid"]))
        autopilot.act(self.tasks, "stop", {"card": "T001"})
        for _ in range(30):
            autopilot.step(self.tasks)
            if autopilot.registry(self.tasks)["runs"][0]["ended"]:
                break
            time.sleep(0.1)
        self.assertTrue(autopilot.registry(self.tasks)["runs"][0]["ended"])
        self.assertEqual(self.state()["autopilot"]["attention"]["T001"], "you stopped its session")
        self.assertEqual(len(self.launched()), 1, "a stopped card is not restarted on its own")

    def test_one_card_without_p_at_a_time(self):
        self.script_for({"T001": ["done"], "T004": ["sleep"]})
        for _ in range(20):
            autopilot.step(self.tasks)
            if self.state()["status"]["T001"] == "done" and len(self.launched()) >= 4:
                break
            time.sleep(0.15)
        live = [r["card"] for r in autopilot.live(autopilot.registry(self.tasks))]
        serial = [c for c in live if not self.state()["cards"][c]["parallel"]]
        self.assertEqual(serial, ["T004"], "one card without [P] at a time; [P] cards may run beside it")
        cards = [c["card"] for c in self.launched()]
        self.assertEqual(sorted(cards), ["T001", "T002", "T003", "T004"])

    def set_meta(self, cid: str, old: str, new: str):
        text = open(self.tasks).read()
        head = text.index(f"#### {cid} ")
        line = text.index("\n", head) + 1
        end = text.index("\n", line)
        assert old in text[line:end], text[line:end]
        open(self.tasks, "w").write(text[:line] + text[line:end].replace(old, new) + text[end:])

    def resume_text(self) -> str:
        return open(self.state()["resume"]).read()

    def test_a_pipe_in_the_step_keeps_the_approval_readable(self):
        self.script_for({"T001": ["approval=psql -c 'select 1' | tee out.txt", "done"]})
        self.settle()
        s = self.state()
        self.assertEqual([(a["card"], a["status"]) for a in s["approvals"]], [("T001", "pending")])
        self.assertIn("| tee out.txt", s["approvals"][0]["step"].replace("\\|", "|"))
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "approved"})
        self.assertIn("\\| tee out.txt | the card's verify step | approved |", self.resume_text())
        self.settle()
        self.assertEqual(self.state()["status"]["T001"], "done")

    def test_approvals_with_the_same_number_stay_apart(self):
        self.script_for({"T002": ["approval#A1=git push", "done"], "T003": ["approval#A1=fly deploy", "done"],
                         "T004": ["done"]})
        self.settle()
        pending = sorted((a["card"], a["n"]) for a in self.state()["approvals"])
        self.assertEqual(pending, [("T002", "A1"), ("T003", "A1")])
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T003", "verdict": "approved", "note": "ok"})
        self.assertEqual([a["card"] for a in self.state()["approvals"]], ["T002"])
        self.settle()
        t003 = [c for c in self.launched() if c["card"] == "T003"]
        self.assertEqual(len(t003), 2)
        self.assertIn("fly deploy", t003[1]["args"][-1])
        self.assertEqual(len([c for c in self.launched() if c["card"] == "T002"]), 1, "T002's A1 is still unanswered")

    def test_approved_step_is_no_longer_denied_for_that_resume(self):
        self.script_for({"T001": ["approval=git push origin feature", "done"]})
        self.settle()
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "approved"})
        autopilot.step(self.tasks)
        args = self.wait_calls(2)[1]["args"]
        deny = json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]
        self.assertEqual(deny, ["Bash(fly deploy:*)"])

    def test_owner_and_kindless_cards_are_never_started(self):
        self.set_meta("T002", "kind backend", "kind: owner")
        self.set_meta("T004", " · kind backend", "")
        self.settle()
        cards = [c["card"] for c in self.launched()]
        self.assertNotIn("T002", cards)
        self.assertNotIn("T004", cards)
        s = self.state()
        self.assertEqual(s["cards"]["T002"]["kind"], "owner")
        self.assertIn("T004", s["autopilot"]["unkinded"])
        self.assertIn("no kind on T004", sv.render(s))

    def test_stop_wins_over_an_answered_approval(self):
        self.script_for({"T001": ["approval", "done"]})
        self.settle()
        reg = autopilot.registry(self.tasks)
        reg["attention"]["T001"] = "you stopped its session"
        autopilot.save_registry(self.tasks, reg)
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "rejected"})
        self.settle()
        self.assertEqual(len(self.launched()), 1)

    def test_an_answer_lost_to_an_api_error_is_delivered_again(self):
        self.script_for({"T001": ["approval", "apierror", "done"]})
        self.settle()
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "approved"})
        self.settle()
        self.assertFalse(self.state()["autopilot"]["settings"]["auto"])
        autopilot.act(self.tasks, "settings", {"auto": True})
        self.settle()
        third = [c for c in self.launched() if c["card"] == "T001"][2]["args"]
        self.assertIn("--resume", third)
        self.assertIn("approved", third[-1])
        first = [c for c in self.launched() if c["card"] == "T001"][0]["args"]
        self.assertEqual(third[third.index("--resume") + 1], first[first.index("--session-id") + 1])

    def test_owner_buttons_keep_the_rules(self):
        self.script_for({"T001": ["done"], "T002": ["sleep"], "T003": ["sleep"], "T004": ["sleep"]})
        autopilot.change_settings(self.tasks, max_parallel=1)
        for _ in range(30):
            autopilot.step(self.tasks)
            if self.state()["status"]["T001"] == "done" and autopilot.live(autopilot.registry(self.tasks)):
                break
            time.sleep(0.15)
        self.assertIn("queued", autopilot.act(self.tasks, "start", {"card": "T003"}))  # the limit is 1: it waits its turn
        self.assertEqual(self.state()["autopilot"]["queued"], ["T003"])
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "retry", {"card": "T001"})  # already done
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T006"})  # waits for CPA
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "stop", {})

    def test_empty_deny_list_pauses(self):
        text = open(self.tasks).read()
        open(self.tasks, "w").write(text.replace("**Never unattended:** `Bash(git push:*)`, `Bash(fly deploy:*)`.", ""))
        autopilot.step(self.tasks)
        self.assertEqual(self.launched(), [])
        self.assertIn("Never unattended", self.state()["autopilot"]["settings"]["paused_reason"])

    def test_a_stray_file_in_screens_is_ignored(self):
        os.makedirs(os.path.join(os.path.dirname(self.tasks), "state", "screens"))
        open(os.path.join(os.path.dirname(self.tasks), "state", "screens", "T003"), "w").write("x")
        self.assertEqual(self.state()["screens"], {})

    def test_only_the_dispatching_process_starts_sessions(self):
        self.assertTrue(autopilot.acquire(self.tasks))
        code = ("import sys; sys.path.insert(0, %r); import autopilot; "
                "print(autopilot.acquire(%r))") % (os.path.join(HERE, "..", "hooks"), self.tasks)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "False")
        self.assertTrue(self.state()["autopilot"]["dispatcher"])

    def test_a_card_waiting_for_a_decision_resumes_after_the_answer(self):
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"T001": ["decision", "done"]})
        self.settle()
        s = self.state()
        self.assertEqual(s["autopilot"]["attention"], {}, "waiting for the owner is not a failed try")
        self.assertIn("owner", {w["kind"] for w in s["waiting"]["T001"]})
        self.assertEqual(len(self.launched()), 1)
        autopilot.act(self.tasks, "decision", {"n": "2", "answer": "B"})
        self.settle()
        second = [c for c in self.launched() if c["card"] == "T001"][1]["args"]
        self.assertIn("--resume", second)
        self.assertEqual(self.state()["status"]["T001"], "done")

    def test_inline_backlog_cards_are_read(self):
        text = open(self.tasks).read().replace("## 6. Supervisor", """## 5. Backlog

- [ ] T002B Fix the spacing — fulfills FR-1
  after: T001 · S · effort high · kind frontend · added by T001
  **Start with:** `Demo · T002B. Follow x §1, then card T002B.`
  **Do:** fix it.

## 6. Supervisor""")
        open(self.tasks, "w").write(text)
        card = self.state()["cards"]["T002B"]
        self.assertEqual((card["effort"], card["kind"], card["after"]), ("high", "frontend", ["T001"]))
        self.assertTrue(card["start_with"].startswith("Demo · T002B"))

    def test_live_view_follows_a_running_session(self):
        os.environ["FAKE_STEP"] = "0.6"
        self.script_for({"T001": ["work"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        time.sleep(1.0)
        first = autopilot.live_view(self.tasks, "T001")
        self.assertTrue(first["run"]["live"])
        self.assertEqual(len(first["todos"]), 4)
        self.assertGreater(first["tools"], 0)
        self.assertTrue(first["current"], "a tool call is in flight")
        seen = first["seq"]
        self.settle()
        rest = autopilot.live_view(self.tasks, "T001", after=seen)
        self.assertTrue(all(e["seq"] > seen for e in rest["events"]), "only events after the given sequence")
        self.assertEqual(rest["todo_done"], 3)
        self.assertEqual(rest["final"]["error"], False)
        self.assertEqual(rest["events"][-1]["kind"], "final")
        brief = autopilot.live_view(self.tasks, "T001", events=False)
        self.assertNotIn("events", brief)
        self.assertLessEqual(len(brief["recent"]), 8)
        os.environ.pop("FAKE_STEP")

    def test_chrome_is_opt_in_and_one_session_at_a_time(self):
        self.script_for({"T001": ["done"], "T002": ["sleep"], "T003": ["sleep"], "T004": ["sleep"]})
        autopilot.step(self.tasks)
        self.assertNotIn("--chrome", self.wait_calls(1)[0]["args"], "off by default")
        autopilot.act(self.tasks, "settings", {"chrome": True})
        for _ in range(40):
            autopilot.step(self.tasks)
            if self.state()["status"]["T001"] == "done" and autopilot.live(autopilot.registry(self.tasks)):
                break
            time.sleep(0.15)
        for _ in range(5):
            autopilot.step(self.tasks)
            time.sleep(0.1)
        self.assertEqual(len(autopilot.live(autopilot.registry(self.tasks))), 1, "one browser session at a time")
        later = [c for c in self.launched() if c["card"] != "T001"]
        self.assertTrue(later and all("--chrome" in c["args"] for c in later))
        self.assertIn("Claude in Chrome", later[0]["args"][later[0]["args"].index("--append-system-prompt") + 1])

    def test_commits_are_matched_to_their_cards(self):
        os.environ["FAKE_COMMIT"] = "1"
        self.script_for({"T001": ["done"]})
        autopilot.change_settings(self.tasks, max_parallel=1)
        for _ in range(40):
            autopilot.step(self.tasks)
            if self.state()["status"]["T001"] == "done":
                break
            time.sleep(0.15)
        os.environ.pop("FAKE_COMMIT")
        s = self.state()
        mine = [c for c in s["commits"] if c["card"] == "T001"]
        self.assertEqual(len(mine), 1)
        self.assertIn("(T001)", mine[0]["subject"])
        self.assertIn("Latest commit:", sv.render(s))
        self.assertTrue(any(c.startswith("new commit") for c in sv.changes({**sv.snapshot(s), "commits": []}, sv.snapshot(s))))

    def test_retry_with_no_room_queues_the_card(self):
        self.script_for({"T001": ["doing", "doing", "done"]})
        self.settle()
        self.assertIn("T001", self.state()["autopilot"]["attention"])
        autopilot.change_settings(self.tasks, max_parallel=1)
        blocker = str(uuid.uuid4())  # a live session that fills the only slot
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", blocker])
        reg = autopilot.registry(self.tasks)
        reg["runs"].append({"card": "T002", "session": blocker, "attempt": 1, "reason": "start", "pid": proc.pid,
                            "started": autopilot.stamp(), "ended": "", "exit": None, "cost": 0, "result": "",
                            "error": "", "log": "runs/none.jsonl"})
        autopilot.save_registry(self.tasks, reg)
        message = autopilot.act(self.tasks, "retry", {"card": "T001"})
        self.assertIn("queued", message)
        s = self.state()
        self.assertEqual(s["autopilot"]["queued"], ["T001"])
        self.assertNotIn("T001", s["autopilot"]["attention"])
        self.assertEqual(len(self.launched()), 2, "nothing starts while the slot is taken")
        proc.kill(); proc.wait()
        self.settle()
        self.assertEqual(len([c for c in self.launched() if c["card"] == "T001"]), 3, "the queued retry got its session")
        self.assertEqual(self.state()["status"]["T001"], "done")
        self.assertEqual(self.state()["autopilot"]["queued"], [])

    def test_a_blocked_card_waits_and_carries_on_when_its_blocker_is_resolved(self):
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"T001": ["blocked", "done"]})
        self.settle()
        s = self.state()
        self.assertEqual(s["autopilot"]["attention"], {}, "a recorded blocker is a wait, not a failed try")
        self.assertEqual(s["autopilot"]["blocked_on"], {"T001": ["T001: needs a key"]})
        self.assertEqual(len(self.launched()), 1, "nothing resumes while the blocker stands")
        autopilot.act(self.tasks, "resolve-blocker", {"text": "T001: needs a key"})
        self.assertIn("- (resolved ", self.resume_text())
        self.settle()
        second = [c for c in self.launched() if c["card"] == "T001"][1]["args"]
        self.assertIn("--resume", second)
        self.assertIn("has been cleared", second[-1])
        s = self.state()
        self.assertEqual(s["status"]["T001"], "done")
        self.assertEqual(s["autopilot"]["blocked_on"], {})

    def test_cards_stuck_on_blockers_are_converted(self):
        autopilot.change_settings(self.tasks, max_attempts=1)
        self.script_for({"T001": ["blocked", "done"]})
        self.settle()
        reg = autopilot.registry(self.tasks)  # what an older dispatcher left behind
        reg["blocked_on"] = {}
        reg["attention"]["T001"] = "1 sessions ended without finishing it; the last said: AUTOPILOT: BLOCKED"
        autopilot.save_registry(self.tasks, reg)
        text = self.resume_text().replace("- T001: needs a key\n", "")  # the owner fixed it by hand
        open(self.state()["resume"], "w").write(text)
        self.settle()
        self.assertEqual(self.state()["status"]["T001"], "done")

    def test_unblock_continues_a_card_whose_blocker_names_no_card(self):
        self.script_for({"T001": ["blocked", "done"]})
        self.settle()
        text = self.resume_text().replace("- T001: needs a key", "- the staging login is broken")
        open(self.state()["resume"], "w").write(text)
        reg = autopilot.registry(self.tasks)
        reg["blocked_on"]["T001"] = []
        autopilot.save_registry(self.tasks, reg)
        self.settle()
        self.assertEqual(len(self.launched()), 1, "no blocker to watch: it waits for the owner")
        self.assertIn("unblocked", autopilot.act(self.tasks, "unblock", {"card": "T001"}))
        self.settle()
        self.assertEqual(self.state()["status"]["T001"], "done")

    def test_a_blocker_written_twice_counts_once_and_approvals_are_numbered_per_card(self):
        text = self.resume_text().replace("## Blockers\n", "## Blockers\n- T004: needs a key\n- T004: needs a key\n")
        open(self.state()["resume"], "w").write(text)
        self.assertEqual(len(self.state()["blockers"]), 1)
        self.assertIn("`<card>.<n>`", autopilot.RULES)

    def runs_as(self, email: str):
        wrapper = os.path.join(self.root, "claude")
        text = open(self.tasks).read().replace("## 6. Supervisor\n", f"## 6. Supervisor\n\n**Runs as:** {email} via `{wrapper}`\n**App URL:** http://localhost:3000/\n")
        open(self.tasks, "w").write(text)
        autopilot.change_settings(self.tasks, claude="claude")  # the default: §6 decides
        return wrapper

    def test_sessions_use_the_launcher_and_account_section_6_names(self):
        wrapper = self.runs_as("owner@example.com")
        self.assertEqual(self.state()["autopilot"]["runs_as"]["app_url"], "http://localhost:3000/")
        autopilot.step(self.tasks)
        self.assertEqual(self.wait_calls(1)[0]["args"][0], "-p")
        reg = autopilot.registry(self.tasks)
        self.assertEqual((reg["account"]["email"], reg["account"]["launcher"]), ("owner@example.com", wrapper))
        self.assertTrue(self.state()["autopilot"]["settings"]["auto"])

    def test_the_wrong_account_pauses_before_any_session(self):
        self.runs_as("someone-else@example.com")
        autopilot.step(self.tasks)
        self.assertEqual(self.launched(), [])
        s = self.state()
        self.assertFalse(s["autopilot"]["settings"]["auto"])
        self.assertIn("expects someone-else@example.com", s["autopilot"]["settings"]["paused_reason"])
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "T001"})

    def test_a_session_that_hangs_after_its_result_is_stopped_and_pauses(self):
        autopilot.change_settings(self.tasks, result_grace_s=1)
        self.script_for({"T001": ["hang"]})
        autopilot.step(self.tasks)
        self.wait_calls(1)
        for _ in range(40):
            autopilot.step(self.tasks)
            if not autopilot.live(autopilot.registry(self.tasks)):
                break
            time.sleep(0.2)
        self.assertEqual(autopilot.live(autopilot.registry(self.tasks)), [], "the hung session was stopped")
        s = self.state()
        self.assertFalse(s["autopilot"]["settings"]["auto"])
        self.assertIn("session limit", s["autopilot"]["settings"]["paused_reason"])

    def test_spend_counts_each_session_once(self):
        reg = autopilot.registry(self.tasks)
        base = {"attempt": 1, "reason": "start", "pid": None, "started": autopilot.stamp(), "ended": autopilot.stamp(),
                "exit": 0, "result": "", "error": "", "log": "runs/x.jsonl"}
        reg["runs"] = [{**base, "card": "T001", "session": "a", "cost": 2.0}, {**base, "card": "T001", "session": "a", "cost": 5.0},
                       {**base, "card": "T002", "session": "b", "cost": 1.0}]
        autopilot.save_registry(self.tasks, reg)
        self.assertEqual(autopilot.spent(reg), 6.0)
        self.assertEqual(self.state()["autopilot"]["spent_usd"], 6.0)

    def test_a_failed_chrome_check_pauses_before_cards_block(self):
        os.environ["FAKE_CHROME"] = "bad"
        try:
            self.runs_as("owner@example.com")
            autopilot.act(self.tasks, "settings", {"chrome": True})
            for _ in range(40):
                autopilot.step(self.tasks)
                if not self.state()["autopilot"]["settings"]["auto"]:
                    break
                time.sleep(0.2)
            s = self.state()
            self.assertEqual(self.launched(), [], "no card session starts while the check fails")
            self.assertIn("Chrome check failed", s["autopilot"]["settings"]["paused_reason"])
            self.assertFalse(s["autopilot"]["chrome_check"]["ok"])
        finally:
            os.environ.pop("FAKE_CHROME")

    def test_follow_up_cards_with_a_digit_suffix_are_seen_and_run(self):
        text = open(self.tasks).read().replace("## 6. Supervisor", """## 5. Backlog

- [ ] T002B2 Second look at the API — fulfills FR-1
  after: T002 · S · effort medium · kind backend · added by T002B
  **Start with:** `Demo · T002B2. Follow x §1, then card T002B2.`

#### T002R2A — A planned split of a follow-up
after: T002B2 · S · effort low · kind backend

**Start with:** `Demo · T002R2A. Follow x §1, then card T002R2A.`

## 6. Supervisor""")
        open(self.tasks, "w").write(text.replace("- [ ] CPEND Feature done", "- [ ] CPEND Feature done\n- [ ] T002R2A A planned split of a follow-up"))
        resume = self.resume_text() + "| T002B2 | x | todo | - | - | - |\n| T002R2A | x | todo | - | - | - |\n"
        open(self.state()["resume"], "w").write(resume)
        s = self.state()
        self.assertIn("T002B2", s["cards"])
        self.assertEqual(s["cards"]["T002R2A"]["after"], ["T002B2"])
        self.assertEqual(s["total"], 10)
        self.assertNotIn("T002B2", " ".join(s["drift"]))
        self.settle()
        launched = [c["card"] for c in self.launched()]
        self.assertIn("T002B2", launched)
        self.assertIn("T002R2A", launched)
        hook = os.path.join(HERE, "..", "hooks", "card-rename.py")
        out = subprocess.run([sys.executable, hook], input=json.dumps({"cwd": self.root, "prompt": f"Demo · T002B2. Follow {self.tasks} §1, then card T002B2."}),
                             capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "SPEC_GRILL_AUTOPILOT"}).stdout
        self.assertIn("T002B2", out)

    def test_report_without_autopilot_has_no_gates(self):
        os.remove(os.path.join(os.path.dirname(self.tasks), "state", "autopilot.json"))
        s = self.state()
        self.assertFalse(s["autopilot"]["used"])
        self.assertEqual(s["gates"], [])
        self.assertIn("Balance: backend 0/3, frontend 0/2", sv.render(s))


class Template(unittest.TestCase):
    """The tasks.md template in SKILL.md stays readable by the supervisor and the autopilot."""

    def test_template_cards_and_resume_parse(self):
        skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        block = skill.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
        cards, order = sv.parse_tasks(block)
        self.assertEqual(cards["T003"]["kind"], "frontend")
        self.assertTrue(cards["T003"]["parallel"])
        self.assertEqual(cards["T003"]["effort"], "high")
        self.assertEqual(cards["T001"]["kind"], "fullstack")
        self.assertTrue(all(cards[c]["kind"] for c in ("T001", "CP0", "T002", "T003", "CPA", "CPEND")))
        self.assertIn("Bash(git push:*)", sv.unattended_deny(block))
        resume = block.split("**RESUME**", 1)[1].split("```markdown", 1)[1].split("```", 1)[0]
        resume = resume.replace("| # | card | step | why | status | answer |",
                                "| # | card | step | why | status | answer |\n| --- | --- | --- | --- | --- | --- |\n"
                                "| A1 | T003 | deploy | verify | pending | |", 1)
        parsed = sv.parse_resume(resume)
        self.assertEqual(parsed["approvals"][0]["card"], "T003")


class Server(unittest.TestCase):
    def test_actions_need_the_page_token(self):
        root = tempfile.mkdtemp(prefix="autopilot-serve-")
        folder = os.path.join(root, "specs", "001-demo")
        os.makedirs(os.path.join(folder, "state"))
        tasks = os.path.join(folder, "tasks.md")
        open(tasks, "w").write(TASKS.replace("{tasks}", tasks))
        open(os.path.join(folder, "state", "RESUME.md"), "w").write(RESUME)
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "hooks", "supervisor.py"), tasks,
                                 "--serve", "--port", "18765"], stdout=subprocess.PIPE, text=True)
        try:
            url = re.search(r"http://\S+/", proc.stdout.readline()).group(0)
            page = urllib.request.urlopen(url).read().decode()
            token = re.search(r'name="supervisor-token" content="([^"]+)"', page).group(1)
            body = json.dumps({"f": "001-demo", "action": "decision", "n": "1", "answer": "Stripe"}).encode()

            def post(headers):
                request = urllib.request.Request(url + "api/act", data=body, method="POST",
                                                 headers={"Content-Type": "application/json", **headers})
                try:
                    return urllib.request.urlopen(request).status
                except urllib.error.HTTPError as error:
                    return error.code

            self.assertEqual(post({}), 403)
            self.assertEqual(post({"X-Supervisor-Token": token, "Origin": "https://evil.example"}), 403)
            self.assertEqual(post({"X-Supervisor-Token": token}), 200)
            headers = urllib.request.urlopen(url).headers
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertFalse(os.path.exists(os.path.join(folder, "state", "autopilot.json")),
                             "answering a decision does not switch the feature onto the autopilot")
            self.assertIn("| Stripe | owner, ", open(os.path.join(folder, "state", "RESUME.md")).read())
        finally:
            proc.terminate()
            proc.wait()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
