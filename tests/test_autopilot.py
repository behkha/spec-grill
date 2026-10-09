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

    def disjoint(self) -> None:
        """Touches that don't overlap, so T002–T004 may run side by side (a card naming no Touches runs alone)."""
        for cid, path in (("T002", "`api/orders.py`"), ("T003", "`ui/login.tsx`"), ("T004", "`db/store.py`")):
            self.touches(cid, path)

    def test_runs_the_feature_through_approval_gate_and_owner_card(self):
        self.disjoint()
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
        self.assertIn("The last session ended with:", runs[1]["args"][-1])
        self.assertIn("Tried, did not work", runs[1]["args"][-1])
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
        autopilot.set_row(sv.build(self.tasks, 4), "status", {"card": "T001"}, {"status": "doing"})
        autopilot.act(self.tasks, "stop", {"card": "T001"})
        self.assertEqual(sv.build(self.tasks, 4)["status"]["T001"], "todo",
                         "a stopped card gives the integration worktree back")
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
        self.disjoint()
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


    WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", self.root, *args], check=True, capture_output=True, text=True,
                              env={**os.environ, **self.WHO}).stdout.strip()

    def commit(self, path: str, message: str) -> str:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a") as handle:
            handle.write(message + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def finish(self, cid: str, commit: str = "-", handoff: str = "") -> None:
        text = re.sub(rf"\| {cid} \| x \| todo \| - \| - \|", f"| {cid} | x | done | b | {commit} |", self.resume_text())
        open(self.state()["resume"], "w").write(text)
        text = open(self.tasks).read().replace(f"- [ ] {cid} ", f"- [x] {cid} ")
        open(self.tasks, "w").write(text)
        folder = os.path.join(os.path.dirname(self.tasks), "state", "handoff")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, f"{cid}.md"), "w").write(f"# {cid}\n{handoff}")

    CHECKS = "| criterion | verdict | evidence | correction |\n| --- | --- | --- | --- |\n"

    def add_to_tasks(self, before: str, block: str) -> None:
        text = open(self.tasks).read().replace(before, block + "\n" + before, 1)
        open(self.tasks, "w").write(text)

    def add_to_resume(self, block: str) -> None:
        text = self.resume_text().replace("## Status", block + "\n\n## Status", 1)
        open(self.state()["resume"], "w").write(text)

    def pins(self) -> list:
        return [d for d in self.state()["drift"] if "pinning" in d]

    def with_pins(self, *rows: str) -> None:
        self.add_to_tasks("## 4. Cards", "## 2. Templates\n\n" + self.CHECKS + "\n## 3. Traceability\n\n"
                          "| id | requirement | cards |\n| --- | --- | --- |\n"
                          "| NFR-2 | invariants have a pinning test | T009 |\n\n"
                          "| rule | **Pinning test** | card |\n| --- | --- | --- |\n" + "".join(rows))

    def test_a_pinning_test_changed_by_another_card_is_drift(self):
        # a requirement row that says "pinning test" sits above the pins table; a second pins table follows
        self.with_pins("| totals never go negative | `tests/test_totals.py` | T002 |\n")
        self.add_to_tasks("## 4. Cards", "More pins:\n\n| rule | pinning test | card |\n| --- | --- | --- |\n"
                          "| ids are unique | `tests/test_ids.py` | T003 |\n")
        self.assertEqual(sv.pinned_tests(open(self.tasks).read()),
                         {"tests/test_totals.py": "T002", "tests/test_ids.py": "T003"})
        self.finish("T002", self.commit("tests/test_totals.py", "test: pin totals (T002)"))
        self.assertFalse(self.pins(), "the writer may change its own pin")
        sha = self.commit("tests/test_totals.py", "fix: storage (T004)")
        self.finish("T004", sha)
        drift = self.pins()
        self.assertEqual(len(drift), 1, drift)
        self.assertIn(f"T004's commit {sha[:7]}", drift[0])
        self.assertIn("tests/test_totals.py", drift[0])
        self.commit("tests/test_ids.py", "fix: ids (T004)")
        self.assertEqual(len(self.pins()), 2, "every pins table is read")

    def test_pin_changes_are_found_on_merged_branches_and_by_resume_commit(self):
        self.with_pins("| totals | `./tests/test_totals.py::test_never_negative` | T002 |\n",
                       "| shapes | `tests/pins/*.py` | T002 |\n")
        self.commit("tests/test_totals.py", "test: pin totals (T002)")
        main = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("checkout", "-qb", "t004")
        branch = self.commit("tests/test_totals.py", "fix: storage (T004)")
        self.git("checkout", "-q", main)
        self.git("merge", "-q", "--no-ff", "-m", "merge the storage branch", "t004")
        self.git("branch", "-qD", "t004")  # only the merge reaches it now
        drift = self.pins()
        self.assertEqual(len(drift), 1, drift)
        self.assertIn(f"T004's commit {branch[:7]} changes the pinning test tests/test_totals.py", drift[0])
        # no card in the subject: RESUME's commit column names it (short and full hash: one line)
        tidy = self.commit("tests/pins/shape.py", "chore: tidy")
        self.finish("T003", f"{tidy[:7].upper()}, {tidy}")
        self.assertEqual(len([d for d in self.pins() if tidy[:7] in d]), 1, self.pins())
        self.assertIn("T003", [d for d in self.pins() if tidy[:7] in d][0])
        self.commit("tests/pins/other.py", "chore: nobody's")  # no card to name: skipped, not guessed
        self.assertEqual(len(self.pins()), 2, self.pins())

    def test_reviewed_pins_and_old_features_are_not_drift(self):
        self.add_to_tasks("## 4. Cards", "| rule | pinning test | card |\n| --- | --- | --- |\n"
                          "| totals | `tests/test_totals.py` | T002 |\n")
        first = self.commit("tests/test_totals.py", "fix: storage (T004)")
        self.assertFalse(self.pins(), "a feature without the Checks template gets no new drift")
        self.add_to_tasks("## 4. Cards", "## 2. Templates\n\n" + self.CHECKS)
        self.assertEqual(len(self.pins()), 1)
        self.add_to_resume(f"## Pins\nPins reviewed up to 0000000\nPins reviewed up to `{first[:9]}`")
        self.assertFalse(self.pins(), "a checkpoint reviewed it (the last line wins)")
        later = self.commit("tests/test_totals.py", "fix: storage again (T004)")
        self.assertEqual([d.split(" changes")[0] for d in self.pins()], [f"T004's commit {later[:7]}"])

    def test_cards_finished_before_checks_from_are_exempt(self):
        table = "| criterion | verdict | evidence | correction |\n| --- | --- | --- | --- |\n"
        text = open(self.tasks).read().replace("## 6. Supervisor", "## 2. Templates\n\n**Checks from:** 2026-10-09\n\n" + table + "\n## 6. Supervisor")
        open(self.tasks, "w").write(text)
        self.finish("T001")
        self.finish("T002")
        resume = self.resume_text().replace("| T001 | x | done | b | - | - |", "| T001 | x | done | b | - | 2026-10-01 |")
        resume = resume.replace("| T002 | x | done | b | - | - |", "| T002 | x | done | b | - | 2026-10-10 |")
        open(self.state()["resume"], "w").write(resume)
        drift = self.state()["drift"]
        self.assertFalse([d for d in drift if d.startswith("T001")], "finished before the cutover")
        self.assertIn("T002 is done but its hand-off has no Checks table (criterion | verdict | evidence)", drift)

    def test_done_cards_list_their_open_checks(self):
        self.add_to_tasks("## 6. Supervisor", "## 2. Templates\n\n" + self.CHECKS)
        table = "## Checks\n" + self.CHECKS
        self.finish("T001", handoff="- Built: x\n| step | result |\n| --- | --- |\n| a | b |\n")
        self.finish("T002", handoff=table + "| tests green | **PASS** | `pytest` 12 passed | - |\n"
                                            "| api \\| health | ✅ | `curl` 200 | - |\n")
        old = sv.snapshot(self.state())
        self.finish("T004", handoff=table + "| loads in 1 s | unresolved | - | no data yet |\n"
                                            "| saves | pass | N/A | - |\n| exports | Passed. | <file path> | - |\n")
        self.finish("CPA", handoff=table + "| merged | ❌ | `make check` 2 failed | one fix |\n")
        self.finish("T003", handoff=table + "| copy reviewed | waived | - | - |\n")
        self.finish("T006", handoff=table + "| pays | waived | - | - |\n")
        open(os.path.join(os.path.dirname(self.state()["resume"]), "design-review.md"), "w").write(
            "- 2026-10-09 CPA checkout: make check fails twice\n")
        autopilot.act(self.tasks, "decision", {"n": "1", "answer": "skip the payment check"})  # T006's decision
        s = self.state()
        self.assertIn("T001 is done but its hand-off has no Checks table (criterion | verdict | evidence)", s["drift"])
        self.assertFalse([d for d in s["drift"] if re.match(r"(T002|T003|T004|T006|CPA) ", d)], s["drift"])
        got = {(c["card"], c["criterion"]): (c["verdict"], c["note"]) for c in s["open_checks"]}
        self.assertEqual(got, {
            ("T004", "loads in 1 s"): ("unresolved", "no data yet"),
            ("T004", "saves"): ("pass", "no evidence"),
            ("T004", "exports"): ("pass", "no evidence"),
            ("CPA", "merged"): ("fail", "in design-review.md"),
            ("T003", "copy reviewed"): ("waived", "waived without an owner's decision"),
        })
        report = sv.render(s)
        self.assertIn('Open checks (the owner decides):\n  T003 "copy reviewed": waived', report)
        self.assertIn('\n  T004 "loads in 1 s": unresolved (no data yet)\n', report)
        self.assertIn('Open checks at the close (the owner decides):', sv.lessons(s))
        self.assertIn('open check: T004 "saves": pass (no evidence)', sv.changes(old, sv.snapshot(s)))
        self.assertFalse([c for c in sv.changes(old, sv.snapshot(s)) if "T006" in c and "open check" in c])

    def test_checks_tables_are_found_and_read_like_a_person_would(self):
        notes = "## Notes\n| criterion | note |\n| --- | --- |\n| x | y |\n\n"
        checks = sv.handoff_checks(notes + "**Checks**\n| **Criterion** (Done when) | **Verdict** | Evidence |\n"
                                           "| --- | --- | --- |\n| a | ok | `ls` |\n")
        self.assertEqual([(c["criterion"], c["verdict"]) for c in checks], [("a", "pass")])
        checks = sv.handoff_checks("| step | result |\n| --- | --- |\n| 1 | 2 |\n\n"
                                   "| Criterion (Done when) | `verdict` | evidence |\n| --- | --- | --- |\n| b | fail | - |\n")
        self.assertEqual([(c["criterion"], c["verdict"]) for c in checks], [("b", "fail")])
        self.assertIsNone(sv.handoff_checks("| step | result |\n| --- | --- |\n| 1 | 2 |\n"))
        # a "|" left unescaped in the criterion: the verdict is found further right
        row = sv.handoff_checks(self.CHECKS + "| a | b | pass | `pytest` ok | - |\n")[0]
        self.assertEqual((row["criterion"], row["verdict"], row["evidence"]), ("a | b", "pass", "`pytest` ok"))
        row = sv.handoff_checks(self.CHECKS + "| a \\| b | pass | `pytest` ok | - |\n")[0]
        self.assertEqual((row["criterion"], row["verdict"]), ("a \\| b", "pass"))
        self.assertEqual(sv.handoff_checks(self.CHECKS + "| a | partly | x | - |\n")[0]["verdict"], "")
        verdicts = {"**PASS**": "pass", "`pass`": "pass", "Passed.": "pass", "✅": "pass", "✅ ok": "pass",
                    "_green_": "pass", "Yes": "pass", "❌": "fail", "❌ pass": "fail", "Failed": "fail",
                    "*unresolved*": "unresolved", "waived": "waived", "partly": "", "": ""}
        self.assertEqual({v: sv.verdict(v) for v in verdicts}, verdicts)
        evidence = {"N/A": False, "na": False, "**None**": False, "TBD": False, "–": False, "—": False,
                    "`-`": False, "<command and its decisive output line>": False, "?": False,
                    "`pytest` 12 passed": True, "state/screens/T003/phone.png": True}
        self.assertEqual({e: sv.has_evidence(e) for e in evidence}, evidence)

    def test_lessons_measure_what_each_card_took(self):
        self.script_for({"T001": ["doing", "done"]})
        self.settle()
        tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"  # "ended" is UTC; read as local time it is 7 hours off
        time.tzset()
        try:
            out = sv.lessons(self.state())
        finally:
            if tz is None:
                os.environ.pop("TZ")
            else:
                os.environ["TZ"] = tz
            time.tzset()
        head = next(line for line in out.splitlines() if line.startswith("| card "))
        row = dict(zip([h.strip() for h in head.split("|")], [c.strip() for c in
                   next(line for line in out.splitlines() if line.startswith("| T001 ")).split("|")]))
        self.assertEqual((row["effort"], row["runs"], row["tries"]), ("high", "2", "2"), row)
        self.assertTrue(0 <= float(row["hours"]) < 1, row)
        self.assertIn("| effort | cards run | tries per card |", out)
        self.assertIn('Cards run by hand have no measurements ("-").', out)
        self.assertRegex(next(line for line in out.splitlines() if line.startswith("| T005 ")), r"\| - \| - \| - \| - \|")

    def lessons_row(self, cid: str) -> list:
        out = sv.lessons(self.state())
        return [c.strip() for c in next(line for line in out.splitlines() if line.startswith(f"| {cid} ")).split("|")]

    def test_lessons_say_what_the_owner_was_needed_for(self):
        self.script_for({"T001": ["approval", "done"]})
        self.settle()
        autopilot.act(self.tasks, "approval", {"n": "A1", "card": "T001", "verdict": "approved", "note": ""})
        self.settle()
        row = self.lessons_row("T001")
        self.assertEqual((row[7], row[8], row[11]), ("2", "1", "1 approval"), row)  # the answer is not a try

    def test_lessons_count_retries_and_flatten_the_owner_column(self):
        self.script_for({"T001": ["doing", "doing", "done"]})
        self.settle()
        reg = autopilot.registry(self.tasks)
        reg["attention"]["T001"] = "2 sessions ended | without\nfinishing it; " + "x" * 200
        autopilot.save_registry(self.tasks, reg)
        cell = sv.cells(next(line for line in sv.lessons(self.state()).splitlines() if line.startswith("| T001 ")))[-1]
        self.assertTrue(cell.startswith("2 sessions ended \\| without finishing it;"), cell)
        self.assertLessEqual(len(cell), 80)
        autopilot.act(self.tasks, "retry", {"card": "T001"})
        self.settle()
        row = self.lessons_row("T001")
        self.assertEqual((row[7], row[8], row[11]), ("3", "3", "owner retry"), row)

    BATCH = """## 5. Backlog

### Batches

- [ ] B1 Login polish

| batch | name | cards, in order | effort | Start with |
| --- | --- | --- | --- | --- |
| B1 | Login polish | T003B, T003C | | `Demo · B1. Follow {tasks} §1, then the cards of batch B1 (§5, Batches) in order.` |

- [ ] T003B Empty state copy
  added by T003 · after: T003 · S · effort medium · kind frontend
  **Start with:** `Demo · T003B. Follow {tasks} §1, then card T003B.`
- [ ] T003C Focus ring
  added by T003 · after: T003B · S · effort high · kind frontend · model sonnet
  **Start with:** `Demo · T003C. Follow {tasks} §1, then card T003C.`

"""

    def add_batch(self) -> None:
        self.add_to_tasks("## 6. Supervisor", self.BATCH.replace("{tasks}", self.tasks))
        open(self.state()["resume"], "a").write("| T003B | x | todo | - | - | - |\n| T003C | x | todo | - | - | - |\n")

    def batch(self, s: dict, bid: str = "B1") -> dict:
        return next(b for b in s["batches"] if b["id"] == bid)

    def test_batches_are_parsed_with_their_tick(self):
        self.add_batch()
        s = self.state()
        b = self.batch(s)
        self.assertEqual((b["name"], b["cards"], b["effort"], b["ticked"]), ("Login polish", ["T003B", "T003C"], "high", False))
        self.assertTrue(b["start_with"].startswith("Demo · B1. Follow ") and b["start_with"].endswith("in order."))
        self.assertNotIn("B1", s["cards"], "a batch is not a card")
        self.assertEqual(s["total"], 10, "progress stays per card")
        self.assertEqual(s["batch_of"], {"T003B": "B1", "T003C": "B1"})
        self.assertFalse([d for d in s["drift"] if "batch" in d], s["drift"])
        text = open(self.tasks).read()
        cards, order = sv.parse_tasks(text.replace("- [ ] B1 ", "- [x] B1 "))
        parsed = sv.parse_batches(text.replace("- [ ] B1 ", "- [x] B1 "), cards, order)[0][0]
        self.assertEqual((parsed["ticked"], parsed["effort"]), (True, ""), "the row names no effort; build() fills it in")
        bad = text.replace("| B1 | Login polish | T003B, T003C | |", "| B1 | Login polish | T003B, T009Z | medium |")
        bad = bad.replace("| --- | --- | --- | --- | --- |\n", "| --- | --- | --- | --- | --- |\n"
                          "| B2 | Seconds | T003B, T003C | low | no line |\n", 1)
        batches, drift = sv.parse_batches(bad, cards, order)
        self.assertEqual([(b["id"], b["cards"], b["effort"]) for b in batches],
                         [("B2", ["T003B", "T003C"], "low"), ("B1", [], "medium")])
        self.assertEqual(drift, ["batch B2 has no Start with line (a backticked line in its table row)",
                                 "T003B is in two batches (B2, B1); it runs with B2",
                                 "batch B1 names T009Z, which tasks.md does not define",
                                 "batch B1 names no card"])

    def test_a_ready_batch_replaces_its_cards(self):
        self.add_batch()
        for cid in ("T001", "T002"):
            self.finish(cid)
        s = self.state()
        b = self.batch(s)
        self.assertEqual((b["status"], b["waits"]), ("waiting", [{"kind": "card", "on": "T003", "status": "todo",
                                                                  "conditional": False}]))
        self.assertEqual(s["ready_batches"], [], "a batch waiting on a card outside it is not ready")
        old = sv.snapshot(s)
        self.finish("T003")
        s = self.state()
        self.assertNotIn("T003B", s["ready"])
        self.assertNotIn("T003C", s["ready"], "T003C waits only on T003B, inside its batch")
        self.assertEqual([x["id"] for x in s["ready_batches"]], ["B1"])
        self.assertEqual(self.batch(s)["waits"], [])
        self.assertIn("batch B1 is now ready", sv.changes(old, sv.snapshot(s)))
        report = sv.render(s)
        self.assertIn("  B1 Login polish · batch of 2 (T003B, T003C) · effort high\n    Start with: Demo · B1. Follow ", report)
        self.assertNotIn("  T003B Empty state copy", report.split("Waiting:")[0])

    def test_a_batch_is_done_by_its_cards_or_its_tick_and_drift_says_when_they_disagree(self):
        self.add_batch()
        for cid in ("T001", "T002", "T003", "T003B", "T003C"):
            self.finish(cid)
        s = self.state()
        self.assertEqual(self.batch(s)["status"], "done")
        self.assertIn("every card of batch B1 is finished but its line in tasks.md is not ticked", s["drift"])
        self.tick_batch()
        self.assertFalse([d for d in self.state()["drift"] if "B1" in d])

    def tick_batch(self) -> None:
        text = open(self.tasks).read().replace("- [ ] B1 ", "- [x] B1 ")
        with open(self.tasks, "w") as handle:
            handle.write(text)

    def test_a_batch_ticked_with_a_card_still_open_is_drift(self):
        self.add_batch()
        for cid in ("T001", "T002", "T003", "T003B"):
            self.finish(cid)
        self.tick_batch()
        s = self.state()
        self.assertEqual(self.batch(s)["status"], "done")
        self.assertIn("batch B1 is ticked in tasks.md but T003C is todo", s["drift"])
        self.assertIn("T003C", s["ready"], "its batch is over: the card left open runs on its own")

    def test_the_autopilot_runs_a_batch_in_one_session(self):
        self.add_batch()
        self.settle()
        launched = [c["card"] for c in self.launched()]
        self.assertEqual(launched.count("B1"), 1)
        self.assertNotIn("T003B", launched, "a batch's cards never start on their own")
        self.assertNotIn("T003C", launched)
        args = next(c for c in self.launched() if c["card"] == "B1")["args"]
        self.assertEqual(args[args.index("-n") + 1], "001 B1 Login polish")
        self.assertEqual(args[args.index("--effort") + 1], "high", "no effort in the row: the highest of its cards")
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[-1], self.batch(self.state())["start_with"])
        rules = args[args.index("--append-system-prompt") + 1]
        self.assertIn("for batch B1 (cards T003B, T003C, in this order", rules)
        self.assertIn("Never start a card or batch beyond the one you were", rules)
        s = self.state()
        self.assertEqual((s["status"]["T003B"], s["status"]["T003C"], self.batch(s)["status"]), ("done", "done", "done"))
        run = next(r for r in autopilot.registry(self.tasks)["runs"] if r.get("batch"))
        self.assertEqual((run["card"], run["batch"]), ("T003B", "B1"))
        self.assertFalse([d for d in s["drift"] if "B1" in d], s["drift"])

    def test_an_interrupted_batch_resumes_its_session(self):
        self.add_batch()
        self.script_for({"B1": ["doing", "done"]})
        self.settle()
        runs = [c for c in self.launched() if c["card"] == "B1"]
        self.assertEqual(len(runs), 2)
        first, second = runs[0]["args"], runs[1]["args"]
        self.assertEqual(second[second.index("--resume") + 1], first[first.index("--session-id") + 1])
        self.assertIn("batch B1 is not finished: cards still open: T003C (RESUME says doing)", second[-1])
        s = self.state()
        self.assertEqual(self.batch(s)["status"], "done")
        reg = autopilot.registry(self.tasks)
        self.assertEqual([r["card"] for r in reg["runs"] if r.get("batch") == "B1"], ["T003B", "T003C"])
        out = sv.lessons(s)
        self.assertIn("| B1 Login polish | T003B, T003C | high | 2 | 2 | 0.25 |", out)
        self.assertRegex(next(line for line in out.splitlines() if line.startswith("| T003B ")),
                         r"\| in B1 \| in B1 \| in B1 \| in B1 \|")

    def test_the_owner_starts_a_batch_from_any_of_its_cards(self):
        self.add_batch()
        for cid in ("T001", "T002", "T003", "T004"):
            self.finish(cid)
        autopilot.change_settings(self.tasks, auto=False)
        self.script_for({"B1": ["sleep"]})
        self.assertTrue(autopilot.acquire(self.tasks))
        self.assertEqual(autopilot.act(self.tasks, "start", {"card": "T003C"}), "B1 started")
        self.assertEqual([c["card"] for c in self.wait_calls(1)], ["B1"])
        self.assertTrue(self.batch(self.state())["live"])
        with self.assertRaises(autopilot.Refused):
            autopilot.act(self.tasks, "start", {"card": "B1"})  # it already has a live session

    def test_a_continued_session_hears_how_the_last_one_ended(self):
        error = autopilot.ending({"error": "api_error: " + "x" * 400 + " TAIL", "result": "ignored"})
        self.assertTrue(error.startswith("api_error: xxx") and "TAIL" not in error and len(error) == 300, error)
        result = autopilot.ending({"error": "", "result": "HEAD\n" + "y" * 400 + "\nAUTOPILOT: SPLIT"})
        self.assertTrue(result.endswith("y AUTOPILOT: SPLIT") and "HEAD" not in result, result)
        self.assertIn("stopped or interrupted", autopilot.ending({"error": "", "result": ""}))

    STAGE_BATCH = ("| batch | name | cards, in order | effort | Start with |\n| --- | --- | --- | --- | --- |\n"
                   "| B1 | Orders and storage | {cards} | | `Demo · B1. Follow {tasks} §1, then the cards of batch B1"
                   " (Stage 2's batch table) in order.` |\n\n")

    def add_stage_batch(self, cards: str = "T002, T004", line: str = "- [ ] B1 Orders and storage") -> None:
        """A planned batch in §4: its table under Stage 2's heading, its line after its last card."""
        text = open(self.tasks).read()
        text = text.replace("### Stage 2 — Build\n\n", "### Stage 2 — Build\n\n"
                            + self.STAGE_BATCH.format(cards=cards, tasks=self.tasks))
        text = text.replace("- [ ] T004 Storage — fulfills FR-3\n", f"- [ ] T004 Storage — fulfills FR-3\n{line}\n")
        with open(self.tasks, "w") as handle:
            handle.write(text)

    def test_a_stage_batch_in_section_4_is_one_unit(self):
        self.add_stage_batch()
        s = self.state()
        b = self.batch(s)
        self.assertEqual((b["name"], b["cards"], b["effort"], b["stage"], b["parallel"]),
                         ("Orders and storage", ["T002", "T004"], "medium", "Stage 2 — Build", False))
        self.assertEqual((b["status"], [w["on"] for w in b["waits"]]), ("waiting", ["T001"]))
        self.assertEqual(s["total"], 8, "a batch is not a card")
        self.finish("T001")
        s = self.state()
        self.assertEqual([x["id"] for x in s["ready_batches"]], ["B1"])
        self.assertEqual(s["ready"], ["T003"], "T002 and T004 run only with B1")
        self.assertIn("  B1 Orders and storage · batch of 2 (T002, T004) · effort medium", sv.render(s))
        self.assertFalse([d for d in s["drift"] if "B1" in d], s["drift"])
        # [P] on the checklist line is read, and kept out of the batch's name
        text = open(self.tasks).read().replace("- [ ] B1 Orders", "- [ ] B1 [P] Orders")
        cards, order = sv.parse_tasks(text)
        parsed = sv.parse_batches(text, cards, order)[0][0]
        self.assertEqual((parsed["parallel"], parsed["name"]), (True, "Orders and storage"))

    def test_the_autopilot_runs_a_stage_batch_and_ticks_its_line_in_section_4(self):
        self.add_stage_batch()
        self.settle()
        launched = [c["card"] for c in self.launched()]
        self.assertEqual(launched.count("B1"), 1)
        self.assertFalse({"T002", "T004"} & set(launched), "a stage batch's cards never start on their own")
        args = next(c for c in self.launched() if c["card"] == "B1")["args"]
        self.assertEqual(args[args.index("-n") + 1], "001 B1 Orders and storage")
        self.assertEqual(args[-1], self.batch(self.state())["start_with"])
        self.assertNotIn("§1 item", " ".join(args), "the prompts name §1's items")
        self.assertIn("§1's Finish item", args[args.index("--append-system-prompt") + 1])
        self.assertIn("- [x] B1 Orders and storage", open(self.tasks).read())
        s = self.state()
        self.assertEqual((s["status"]["T002"], s["status"]["T004"], self.batch(s)["status"]), ("done", "done", "done"))
        self.assertFalse([d for d in s["drift"] if "B1" in d], s["drift"])

    def test_a_batch_an_outside_card_sits_inside_is_drift(self):
        self.set_meta("T003", "after: T001", "after: T002")
        self.set_meta("T004", "after: T001", "after: T003")
        self.add_stage_batch()
        self.finish("T001")
        s = self.state()
        self.assertIn("batch B1 can never run: T003, outside it, waits for T002 while T004 waits for T003;"
                      " put T003 in the batch or take T004 out", s["drift"])
        self.assertEqual(self.batch(s)["status"], "waiting")

    def test_a_batch_holding_a_card_that_runs_alone_is_drift(self):
        self.add_stage_batch(cards="T001, CPA, T005")
        drift = self.state()["drift"]
        self.assertIn("batch B1 holds T001, the first card: it runs on its own, never in a batch", drift)
        self.assertIn("batch B1 holds CPA, a checkpoint: it runs on its own, never in a batch", drift)
        self.assertIn("batch B1 holds T005, an owner's card: it runs on its own, never in a batch", drift)

    def test_a_batch_over_the_size_budget_is_drift(self):
        self.set_meta("T003", " · S · ", " · M · ")
        self.set_meta("T004", " · S · ", " · M · ")
        self.add_stage_batch(cards="T002, T003, T004")
        self.assertIn("batch B1 is too big for one session: T002 S + T003 M + T004 M = 5"
                      " (at most 4, counting S as 1 and M as 2)", self.state()["drift"])
        self.set_meta("T004", " · M · ", " · L · ")
        drift = self.state()["drift"]
        self.assertIn("batch B1 holds T004, an L card: split it before it goes in a batch", drift)
        self.assertFalse([d for d in drift if "too big" in d], "an L is named, not summed")
        self.set_meta("T004", " · L · ", " · S · ")
        self.assertFalse([d for d in self.state()["drift"] if "B1" in d], "S + M + S = 4 fits")

    LOOSE = """## 5. Backlog

- [ ] T003B Empty state copy
  added by T003 · after: T003 · S · effort medium · kind frontend
  **Start with:** `Demo · T003B. Follow {tasks} §1, then card T003B.`
  **Touches:** `ui/tasks/list.tsx`.
- [ ] T003C Focus ring
  added by T003 · after: T003 · S · effort medium · kind frontend
  **Start with:** `Demo · T003C. Follow {tasks} §1, then card T003C.`
  **Touches:** `ui/tasks/{{row.tsx,list.tsx}}`. **Never:** change the copy.
- [ ] T003D Row spacing
  added by T003 · after: T003 · S · effort medium · kind frontend
  **Start with:** `Demo · T003D. Follow {tasks} §1, then card T003D.`
  **Touches:** `ui/tasks/row.tsx`.
- [ ] T003E Deploy the fix
  added by T003 · after: T003 · S · effort medium · kind frontend
  **Do:** deploy `web` after the owner's yes.
  **Touches:** `ui/tasks/row.tsx`.

"""

    def touches(self, cid: str, files: str) -> None:
        text = open(self.tasks).read()
        line = f"then card {cid}.`\n"
        self.assertIn(line, text)
        with open(self.tasks, "w") as handle:
            handle.write(text.replace(line, f"{line}**Touches:** {files}. **Never:** guess.\n", 1))

    def test_small_cards_in_no_batch_get_a_quiet_heads_up(self):
        self.touches("T002", "`api/orders.py`")
        self.touches("T003", "`ui/login.tsx`")
        self.touches("T004", "`db/store.py`")
        self.add_to_tasks("## 6. Supervisor", self.LOOSE.replace("{tasks}", self.tasks))
        open(self.state()["resume"], "a").write("".join(f"| {c} | x | todo | - | - | - |\n"
                                                         for c in ("T003B", "T003C", "T003D", "T003E")))
        s = self.state()
        self.assertEqual(s["cards"]["T003C"]["touches"], ["ui/tasks/row.tsx", "ui/tasks/list.tsx"])
        self.assertEqual(s["cards"]["T003C"]["stage"], "Backlog")
        self.assertTrue(s["cards"]["T003E"]["asks_owner"])
        texts = [u["text"] for u in s["unbatched"]]
        self.assertIn("3 small cards are in no batch: T003B, T003C, T003D (they share `ui/tasks/…`);"
                      " batch them (§5)", texts, "T003E deploys: it runs alone")
        self.assertNotIn("T001", " ".join(texts))
        self.assertEqual(len(texts), 1, "T002, T003 and T004 share no file")
        self.assertEqual(s["cards"]["T002"]["touches"], ["api/orders.py"])
        self.assertIn("Heads-up:\n  3 small cards are in no batch: T003B, T003C, T003D", sv.render(s))
        self.assertFalse([d for d in s["drift"] if "no batch" in d], "a heads-up, not drift")
        self.assertNotIn("unbatched", sv.snapshot(s), "it never wakes the supervisor")
        # batched or finished cards are not counted
        before = open(self.tasks).read()
        self.add_to_tasks("## 6. Supervisor", "| batch | name | cards, in order | effort | Start with |\n"
                          "| --- | --- | --- | --- | --- |\n| B1 | Rows | T003C, T003D | | `Demo · B1.` |\n\n- [ ] B1 Rows\n\n")
        self.assertEqual(self.state()["unbatched"], [], "T003C and T003D are batched; T003B is left alone")
        open(self.tasks, "w").write(before)
        self.finish("T003B")
        self.assertEqual(self.state()["unbatched"], [], "T003B is done: two cards left in no batch")

    def test_cards_without_touches_are_grouped_by_stage(self):
        s = self.state()
        self.assertEqual([(u["cards"], u["share"]) for u in s["unbatched"]],
                         [(["T002", "T003", "T004"], "a stage, Stage 2 — Build; their Touches name no files")])
        self.assertTrue(s["unbatched"][0]["text"].endswith("batch them (the batch table under Stage 2 — Build)"))
        self.add_stage_batch()
        self.assertEqual(self.state()["unbatched"], [], "T003 alone is no batch")


    # --- [P] batches side by side: the Touches check, own worktrees and ports, the merge lock ---------

    PARALLEL = """## 5. Backlog

### Batches

- [ ] B1 [P] Orders polish
- [ ] B2 [P] Login polish

| batch | name | cards, in order | effort | Start with |
| --- | --- | --- | --- | --- |
| B1 | Orders polish | T002B, T002C | | `Demo · B1. Follow {tasks} §1, then the cards of batch B1 (§5, Batches) in order.` |
| B2 | Login polish | T003B, T003C | | `Demo · B2. Follow {tasks} §1, then the cards of batch B2 (§5, Batches) in order.` |

- [ ] T002B Orders empty state
  added by T002 · after: T001 · S · effort medium · kind backend
  **Start with:** `Demo · T002B. Follow {tasks} §1, then card T002B.`
  **Touches:** `api/orders/{list.py,empty.py}`.
- [ ] T002C Orders row copy
  added by T002 · after: T002B · S · effort medium · kind backend
  **Start with:** `Demo · T002C. Follow {tasks} §1, then card T002C.`
  **Touches:** `api/orders/rows.py`.
- [ ] T003B Login empty state
  added by T003 · after: T001 · S · effort medium · kind frontend
  **Start with:** `Demo · T003B. Follow {tasks} §1, then card T003B.`
  **Touches:** `ui/login/{form.tsx,copy.ts}`.
- [ ] T003C Login focus ring
  added by T003 · after: T003B · S · effort medium · kind frontend
  **Start with:** `Demo · T003C. Follow {tasks} §1, then card T003C.`
  **Touches:** `ui/login/focus.css`.

"""
    WHERE = ("2. **Where things live.** Code work happens in the integration worktree `../demo-wt`, branch"
             " `feat/demo`; a `[P]` card works in `../demo-wt-t0nn`, branch `feat/demo-t0nn-<slug>`. A `[P]`\n"
             "   batch started beside another works in `../demo-wt-b<n>`, branch `batch/b<n>`, dev server on its own\n"
             "   port.\n\n")

    def parallel_batches(self, edit=lambda text: text) -> None:
        """Two [P] batches in §5 whose cards wait only on T001 (done); T002–T004 are the owner's to run."""
        self.add_to_tasks("## 6. Supervisor", edit(self.PARALLEL.replace("{tasks}", self.tasks)))
        self.add_to_tasks("## 4. Cards", self.WHERE)
        open(self.state()["resume"], "a").write("".join(f"| {c} | x | todo | - | - | - |\n"
                                                         for c in ("T002B", "T002C", "T003B", "T003C")))
        self.finish("T001")
        reg = autopilot.registry(self.tasks)
        reg["manual"] = ["T002", "T003", "T004"]
        autopilot.save_registry(self.tasks, reg)

    def live_units(self) -> list:
        return sorted(autopilot.run_unit(r) for r in autopilot.live(autopilot.registry(self.tasks)))

    def steps(self, n: int = 4) -> None:
        for _ in range(n):
            autopilot.step(self.tasks)
            time.sleep(0.1)

    def test_touches_clash_reads_paths_folders_and_globs(self):
        clash = sv.touches_clash
        self.assertEqual(clash(["api/x.py"], ["ui/y.tsx"]), "")
        self.assertEqual(clash(["api/x.py"], ["api/x.py"]), "api/x.py")
        self.assertEqual(clash(["packages/x"], ["packages/x/a.ts"]), "packages/x/…")
        self.assertEqual(clash(["packages/x/a.ts"], ["packages/x/"]), "packages/x/…")
        self.assertEqual(clash(["api/orders"], ["api/orders2.py"]), "", "a prefix only on a folder boundary")
        self.assertEqual(clash(["src/**/*.ts"], ["src/app/a.ts"]), "src/…")
        self.assertEqual(clash(["**/*.ts"], ["ui/a.ts"]), "**/*.ts (everything)")
        self.assertIsNone(clash(None, ["ui/a.ts"]))
        self.assertIsNone(clash(["ui/a.ts"], None))

    def test_parallel_batches_with_disjoint_touches_start_together(self):
        self.parallel_batches()
        self.script_for({"B1": ["sleep"], "B2": ["sleep"]})
        s = self.state()
        self.assertEqual([b["id"] for b in s["ready_batches"]], ["B1", "B2"])
        self.assertIn("May run side by side, each in its own worktree: T002, T003, B1, B2 (plus", sv.render(s))
        autopilot.step(self.tasks)
        calls = self.wait_calls(2)
        self.assertEqual([c["card"] for c in calls], ["B1", "B2"], "both start in the same pass")
        self.assertEqual(self.live_units(), ["B1", "B2"])
        s = self.state()
        self.assertEqual([self.batch(s, b)["status"] for b in ("B1", "B2")], ["running", "running"])
        first, second = calls[0], calls[1]
        self.assertEqual((first["port_offset"], second["port_offset"]), ("1", "2"), "a dev-server port each")
        self.assertEqual([r["slot"] for r in autopilot.registry(self.tasks)["runs"]], [1, 2])
        rules = second["args"][second["args"].index("--append-system-prompt") + 1]
        lock = os.path.join(os.path.dirname(self.tasks), "state", "merge.lock")
        self.assertIn("running now: B1", rules)
        self.assertIn("`../demo-wt-b2`, branch `batch/b2`", rules, "the worktree §1 names, for this batch")
        self.assertIn(f"mkdir {lock}", rules)
        self.assertIn(f"{lock}/holder", rules)
        self.assertIn("default port plus 2", rules)
        self.assertIn("$SPEC_GRILL_PORT_OFFSET", rules)
        self.assertIn("Never `git stash`", rules)
        self.assertIn("Never touch another session's worktree", rules)
        self.assertIn("running now: none yet", first["args"][first["args"].index("--append-system-prompt") + 1])

    def test_parallel_batches_whose_touches_overlap_wait(self):
        self.parallel_batches(lambda text: text.replace("`ui/login/{form.tsx,copy.ts}`", "`api/orders`, `ui/login/form.tsx`"))
        self.script_for({"B1": ["sleep"], "B2": ["sleep"]})
        self.steps()
        self.assertEqual(self.live_units(), ["B1"])
        self.assertEqual([c["card"] for c in self.launched()], ["B1"])
        s, reg, cfg = self.state(), autopilot.registry(self.tasks), autopilot.settings(self.tasks)
        self.assertEqual(autopilot.room(s, reg, cfg, "B2"), "B2 shares api/orders/… with running B1")
        self.assertEqual(self.batch(s, "B2")["waits"],
                         [{"kind": "touches", "on": "B1", "status": "running", "shares": "api/orders/…"}])
        self.assertEqual(self.batch(s, "B2")["status"], "waiting")
        self.assertIn("  B2 [P] Login polish (batch) waits for B1 to finish (running; their Touches overlap: api/orders/…)",
                      sv.render(s))
        self.assertEqual(autopilot.act(self.tasks, "start", {"card": "B2"}),
                         "B2 queued: it starts when a session slot frees up. (B2 shares api/orders/… with running B1)")
        # B1 ends: B2 starts on the next pass
        autopilot.act(self.tasks, "stop", {"card": "B1"})
        for _ in range(30):
            autopilot.step(self.tasks)
            if "B2" in self.live_units():
                break
            time.sleep(0.1)
        self.assertEqual(self.live_units(), ["B2"])
        # a card naming no Touches overlaps everything: it never runs beside another unit
        text = open(self.tasks).read().replace("  **Touches:** `api/orders/rows.py`.\n", "")
        open(self.tasks, "w").write(text)
        s, reg = self.state(), autopilot.registry(self.tasks)
        self.assertEqual(autopilot.room(s, reg, cfg, "B1"), "B1's cards name no Touches, so it runs alone (B2 is running)")
        self.assertEqual(self.batch(s, "B1")["waits"][0]["kind"], "touches")

    def test_a_serial_unit_runs_beside_a_parallel_one_but_not_beside_another_serial_one(self):
        self.parallel_batches(lambda text: text.replace("- [ ] B2 [P] Login polish", "- [ ] B2 Login polish"))
        self.touches("T004", "`db/store.py`")
        reg = autopilot.registry(self.tasks)
        reg["manual"] = ["T002", "T003"]
        autopilot.save_registry(self.tasks, reg)
        self.script_for({"B1": ["sleep"], "B2": ["sleep"], "T004": ["sleep"]})
        self.steps()
        self.assertEqual(self.live_units(), ["B1", "T004"], "serial T004 and [P] B1 run together")
        s, reg, cfg = self.state(), autopilot.registry(self.tasks), autopilot.settings(self.tasks)
        self.assertIn("T004 holds the integration worktree", autopilot.room(s, reg, cfg, "B2"))
        self.assertEqual(self.batch(s, "B2")["waits"][0]["kind"], "worktree")
        calls = {c["card"]: c for c in self.launched()}
        self.assertEqual(calls["T004"]["port_offset"], "0", "the integration worktree keeps the default port")
        self.assertNotIn("You are a [P]", calls["T004"]["args"][calls["T004"]["args"].index("--append-system-prompt") + 1])

    def test_the_merge_lock_is_shown_and_cleared_when_its_holder_is_gone(self):
        self.parallel_batches()
        lock = os.path.join(os.path.dirname(self.tasks), "state", "merge.lock")
        os.mkdir(lock)
        open(os.path.join(lock, "holder"), "w").write("T001 2026-10-09T10:00Z\n")
        s = self.state()
        self.assertEqual(s["merge_lock"]["holder"], "T001")
        self.assertIn("Merge lock: held by T001 since 2026-10-09T10:00Z", sv.render(s))
        self.assertIn("the merge lock (state/merge.lock) is still held by T001, which is done", s["drift"])
        self.script_for({"B1": ["sleep"], "B2": ["sleep"]})
        events = autopilot.step(self.tasks)
        self.assertIn("cleared the merge lock T001 held: no live session of it is left", events)
        self.assertFalse(os.path.exists(lock))
        self.assertIsNone(self.state()["merge_lock"])
        # held by a live session: it stays, and the supervisor shows it
        self.wait_calls(2)
        os.mkdir(lock)
        open(os.path.join(lock, "holder"), "w").write("B2 2026-10-09T10:05Z\n")
        self.steps(2)
        self.assertTrue(os.path.isdir(lock))
        s = self.state()
        self.assertEqual((s["merge_lock"]["holder"], s["merge_lock"]["since"]), ("B2", "2026-10-09T10:05Z"))
        self.assertFalse([d for d in s["drift"] if "merge lock" in d])
        # just taken, its holder line not written yet: it stays
        shutil.rmtree(lock)
        os.mkdir(lock)
        self.steps(1)
        self.assertTrue(os.path.isdir(lock))

    def test_chrome_still_runs_one_parallel_batch_at_a_time(self):
        self.parallel_batches()
        self.script_for({"B1": ["sleep"], "B2": ["sleep"]})
        autopilot.act(self.tasks, "settings", {"chrome": True})
        for _ in range(40):
            autopilot.step(self.tasks)
            if autopilot.live(autopilot.registry(self.tasks)):
                break
            time.sleep(0.15)
        self.steps(5)
        self.assertEqual(len(autopilot.live(autopilot.registry(self.tasks))), 1, "one browser session at a time")


class CloseWaits(unittest.TestCase):
    def test_after_section_5_waits_for_every_backlog_card(self):
        text = (
            "# Tasks: X\n\n## 4. Cards\n\n- [x] T001 One\n- [ ] T009 Results\n\n"
            "#### T009 — Results\nafter: T001, §5 · M · effort medium · kind fullstack\n\n"
            "## 5. Backlog\n\n- [ ] T001B Fix one\n  after: T001 · S · effort low · kind frontend\n"
            "- [ ] T001C Fix two\n  after: T001 · S · effort low · kind frontend\n"
        )
        cards, _ = sv.parse_tasks(text)
        self.assertEqual(cards["T009"]["after"], ["T001", "T001B", "T001C"])
        self.assertEqual(cards["T001B"]["after"], ["T001"])


class CrossPhase(unittest.TestCase):
    """"after: phase 1's T003" names a card of another feature (a sibling folder), never this file's T003."""

    ONE = (
        "# Tasks: One\n\n## 4. Cards\n\n- [x] T001 Base\n- [ ] T002 Middle\n- [ ] T003 Top\n\n"
        "#### T001 — Base\nafter: — · M · effort high · kind backend\n\n"
        "#### T002 — Middle\nafter: T001 · S · effort low · kind backend\n\n"
        "#### T003 — Top\nafter: T002 · S · effort low · kind backend\n"
    )
    TWO = (
        "# Tasks: Two\n\n## 4. Cards\n\n- [ ] T001 Start\n- [ ] T002 Next\n- [ ] T003 Last\n\n"
        "### Stage 2 — Build\n\n| batch | name | cards, in order | effort | Start with |\n"
        "| --- | --- | --- | --- | --- |\n| B1 | Pair | T002, T003 | | `Two · B1.` |\n\n- [ ] B1 Pair\n\n"
        "#### T001 — Start\nafter: {after} · M · effort high · kind backend\n\n"
        "#### T002 — Next\nafter: T001 · S · effort low · kind backend\n\n"
        "#### T003 — Last\nafter: T002 · S · effort low · kind backend\n"
    )

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="phases-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def feature(self, name: str, text: str, rows: dict | None = None) -> str:
        folder = os.path.join(self.root, "spec", name)
        os.makedirs(os.path.join(folder, "state"), exist_ok=True)
        tasks = os.path.join(folder, "tasks.md")
        with open(tasks, "w", encoding="utf-8") as handle:
            handle.write(text)
        if rows is not None:
            with open(os.path.join(folder, "state", "RESUME.md"), "w", encoding="utf-8") as handle:
                handle.write("# resume\n\n## Status\n| card | title | status | branch | commit | date (UTC) |\n"
                             "| --- | --- | --- | --- | --- | --- |\n"
                             + "".join(f"| {c} | x | {st} | - | - | - |\n" for c, st in rows.items()))
        return tasks

    def test_parse_takes_other_phases_out_of_the_local_dependencies(self):
        cards, _ = sv.parse_tasks(self.TWO.format(after="phase 1's T003 (phase 1 on `main`)"))
        self.assertEqual(cards["T001"]["after"], [], "T003 here is another feature's")
        self.assertEqual(cards["T001"]["external"], [{"phase": "1", "card": "T003", "conditional": False}])
        cards, _ = sv.parse_tasks(self.TWO.format(
            after="T002, Phase 10’s T041A and T042, phase 11's T021–T023, CPD (the owner's yes)"))
        self.assertEqual(cards["T001"]["after"], ["T002", "CPD"])
        self.assertEqual([(d["phase"], d["card"], d.get("through")) for d in cards["T001"]["external"]],
                         [("10", "T041A", None), ("10", "T042", None), ("11", "T021", "T023")])
        cards, _ = sv.parse_tasks(self.TWO.format(after="T002 (beside phase 3's T009), (phase 4's CP2 if German)"))
        self.assertEqual(cards["T001"]["external"], [{"phase": "4", "card": "CP2", "conditional": True}],
                         "a bracket is no dependency unless it names a condition")

    def test_a_card_waits_for_another_phases_card_until_it_is_finished(self):
        one = self.feature("01-one", self.ONE, {"T001": "done", "T002": "done", "T003": "todo"})
        two = self.feature("2-two", self.TWO.format(after="phase 1's T003 (phase 1 on `main`)"),
                           {"T001": "todo", "T002": "todo", "T003": "todo"})
        s = sv.build(two, 4)
        self.assertEqual(s["waiting"]["T001"], [{"kind": "phase", "on": "phase 1's T003", "status": "todo",
                                                 "conditional": False}])
        self.assertNotIn("T001", s["ready"])
        self.assertIn("phase 1's T003 (todo)", sv.wait_text(s["waiting"]["T001"]))
        self.assertEqual(s["cards"]["T003"]["after"], ["T002"])
        self.assertEqual(s["drift"], [], "no loop through this file's T003, so no 'can never run'")
        self.assertEqual(s["unblocks"], {}, "nothing runs, so nothing unblocks")
        reg, cfg = autopilot.registry(two), autopilot.settings(two)
        self.assertEqual(autopilot.plan(two, s, reg, cfg), [], "a phase wait is a real wait, not a room wait")
        self.assertNotIn("phase", autopilot.ROOM_WAITS)
        # ticked in tasks.md but still todo in feature 1's RESUME: RESUME wins, it still waits
        open(one, "w").write(self.ONE.replace("- [ ] T003", "- [x] T003"))
        self.assertEqual(sv.build(two, 4)["waiting"]["T001"][0]["status"], "todo")
        self.feature("01-one", self.ONE.replace("- [ ] T002", "- [x] T002").replace("- [ ] T003", "- [x] T003"),
                     {"T001": "done", "T002": "done", "T003": "done"})
        s = sv.build(two, 4)
        self.assertNotIn("T001", s["waiting"])
        self.assertEqual(s["ready"], ["T001"])
        self.assertEqual(s["unblocks"]["T001"], ["T002"], "unblocks reads this file's cards only")
        self.assertEqual(s["drift"], [])

    def test_a_missing_phase_or_card_waits_and_says_not_found(self):
        self.feature("01-one", self.ONE, {"T001": "done", "T002": "done", "T003": "done"})
        two = self.feature("02-two", self.TWO.format(after="phase 7's T003, phase 1's T099"))
        s = sv.build(two, 4)
        self.assertEqual([(w["kind"], w["on"], w["status"]) for w in s["waiting"]["T001"]],
                         [("phase", "phase 7's T003", "unknown"), ("phase", "phase 1's T099", "unknown")])
        self.assertEqual(sv.wait_text(s["waiting"]["T001"]),
                         "phase 7's T003 (not found); phase 1's T099 (not found)")
        self.assertNotIn("T001", s["ready"])
        self.assertEqual(s["drift"], [])

    def test_a_range_of_another_phase_expands_with_that_phases_cards(self):
        self.feature("01-one", self.ONE, {"T001": "done", "T002": "todo", "T003": "todo"})
        two = self.feature("02-two", self.TWO.format(after="phase 1's T001–T003"))
        s = sv.build(two, 4)
        self.assertEqual([w["on"] for w in s["waiting"]["T001"]], ["phase 1's T002", "phase 1's T003"])

    def test_owner_step_heuristic_reads_whole_words(self):
        for text in ("Write the reproduction test.", "The step is not paid for.", "Unpaid trial only."):
            self.assertIsNone(sv.OWNER_STEP_RE.search(text), text)
        for text in ("Switch it on in production.", "A paid API call.", "Deploy to staging.", "Ask first."):
            self.assertIsNotNone(sv.OWNER_STEP_RE.search(text), text)


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
        self.assertTrue(all(cards[c]["kind"] for c in ("T001", "CP0", "T002", "T003", "T004", "CPA", "CPEND")))
        self.assertIn("Bash(git push:*)", sv.unattended_deny(block))
        resume = block.split("**RESUME**", 1)[1].split("```markdown", 1)[1].split("```", 1)[0]
        resume = resume.replace("| # | card | step | why | status | answer |",
                                "| # | card | step | why | status | answer |\n| --- | --- | --- | --- | --- | --- |\n"
                                "| A1 | T003 | deploy | verify | pending | |", 1)
        parsed = sv.parse_resume(resume)
        self.assertEqual(parsed["approvals"][0]["card"], "T003")

    def test_template_pins_and_checks_parse(self):
        skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        block = skill.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
        self.assertEqual(list(sv.pinned_tests(block)), ["<test file>"])
        handoff = block.split("**Hand-off note**", 1)[1].split("```markdown", 1)[1].split("```", 1)[0]
        checks = sv.handoff_checks(handoff)
        self.assertEqual(len(checks), 1)
        self.assertEqual(sv.column(checks[0], "criterion"), "<each Done when line>")
        self.assertEqual(checks[0]["verdict"], "pass")  # "pass / fail / unresolved": judged by its first word
        resume = block.split("**RESUME**", 1)[1].split("```markdown", 1)[1].split("```", 1)[0]
        self.assertIn("Pins reviewed up to <commit>", resume)
        self.assertEqual(sv.parse_resume(resume)["pins_reviewed"], "", "the placeholder is no commit")

    def test_template_rules_agree(self):
        skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        block = skill.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
        finish = re.sub(r"\s+", " ", block.split("9. **Finish.**", 1)[1].split("2. Commit", 1)[0])
        self.assertIn("a check recorded as `fail` in the Checks table with its design-review.md line (the scope item)", finish)
        self.assertIn("write a `|` inside a cell as `\\|`", finish)
        self.assertIn("(In the Checks table, write a `|` inside a cell as `\\|`.)", block)
        self.assertIn("In the Checks table too, write a `|` inside a cell as `\\|`.", autopilot.RULES)
        routine = re.sub(r"\s+", " ", block.split("**Checkpoint routine**", 1)[1].split("### Stage 1", 1)[0])
        self.assertIn("write `Pins reviewed up to <commit>`", routine)
        # the autopilot's rules name §1's items by their titles, since projects number them differently
        for title, name in (("Preconditions.", "Preconditions"), ("Stay in scope", "scope"),
                            ("Context budget.", "Context budget"), ("Owner's yes.", "Owner's yes"), ("Finish.", "Finish")):
            self.assertRegex(block, rf"\n\d+\. \*\*{re.escape(title)}")
            self.assertIn(f"§1's {name} item", autopilot.RULES)
        prompts = [autopilot.RULES, autopilot.PARALLEL_RULES, autopilot.CHROME_RULES, autopilot.CONTINUE, autopilot.BATCH_CONTINUE,
                   autopilot.UNBLOCKED, autopilot.ANSWER, autopilot.APPROVED, autopilot.REJECTED]
        self.assertFalse([p for p in prompts if re.search(r"§1 items? \d|\bitems? \d", p)])
        self.assertFalse(re.search(r"§1 items? \d", skill), "SKILL.md names §1's items, not their numbers")

    def template_feature(self, ticks=()) -> str:
        """The template written as a feature's tasks.md (no RESUME: statuses come from the ticks)."""
        skill = open(os.path.join(HERE, "..", "SKILL.md"), encoding="utf-8").read()
        block = skill.split("### tasks.md", 1)[1].split("````markdown", 1)[1].split("\n````", 1)[0]
        for cid in ticks:
            block = block.replace(f"- [ ] {cid} ", f"- [x] {cid} ")
        root = tempfile.mkdtemp(prefix="template-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "specs", "001-t"))
        tasks = os.path.join(root, "specs", "001-t", "tasks.md")
        with open(tasks, "w", encoding="utf-8") as handle:
            handle.write(block)
        return tasks

    def test_template_stage_batch_is_ready_after_its_dependencies(self):
        s = sv.build(self.template_feature(), 4)
        b1 = next(b for b in s["batches"] if b["id"] == "B1")
        self.assertEqual((b1["cards"], b1["effort"], b1["stage"]), (["T002", "T004"], "high", "Stage 2 — <user scenario>"))
        self.assertEqual(b1["status"], "waiting")
        self.assertIn("then the cards of batch B1 (Stage 2's batch table) in order.", b1["start_with"])
        self.assertEqual(s["cards"]["CPA"]["after"], ["T002", "T003", "T004"])
        self.assertEqual([b["id"] for b in s["batches"]], ["B1", "B2"], "§5's batches continue the numbering")
        self.assertFalse([d for d in s["drift"] if "B1" in d], s["drift"])
        self.assertEqual(s["unbatched"], [], "the template leaves no small card unbatched")
        s = sv.build(self.template_feature(ticks=("T001", "CP0")), 4)
        self.assertEqual([b["id"] for b in s["ready_batches"]], ["B1"])
        self.assertIn("T003", s["ready"], "T003 runs beside B1")
        self.assertFalse({"T002", "T004"} & set(s["ready"]), "B1's cards are not ready alone")


    def test_template_names_the_worktrees_and_the_merge_lock_of_parallel_units(self):
        s = sv.build(self.template_feature(), 4)
        self.assertEqual(autopilot.own_worktree(s, "B2"), "`<path>-b2`, branch `batch/b2` (§1's \"Where things live\")")
        self.assertEqual(autopilot.own_worktree(s, "T003"),
                         "`<path>-t003`, branch `<branch>-t003-<slug>` (§1's \"Where things live\")")
        where = re.sub(r"\s+", " ", open(s["tasks"]).read().split("**Where things live.**", 1)[1].split("3. **Start.**", 1)[0])
        self.assertIn("`mkdir state/merge.lock`", where)
        self.assertIn("`rm -rf state/merge.lock`", where)
        self.assertEqual(autopilot.own_worktree({**s, "tasks": os.devnull}, "B2"),
                         "a worktree and branch of your own, as §1's \"Where things live\" says")


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
