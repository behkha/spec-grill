"""Tests for the supervisor at run time: what reaches git, stalled cards, broken state files, the dashboard
server's answers to bad requests, the --wait snapshot, the dispatcher probe, the git cache, the drift
list's owner cards and always-alone cards, and the autopilot's paused first run.

    python3 -m pytest -q tests/test_supervisor_runtime.py
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "..", "hooks")
sys.path.insert(0, HERE)
sys.path.insert(0, HOOKS)
import autopilot  # noqa: E402
import supervisor as sv  # noqa: E402
import test_autopilot as base  # noqa: E402  (a module, so its Feature tests are not collected here again)
from test_autopilot import RESUME, TASKS  # noqa: E402,F401

SUPERVISOR = os.path.join(HOOKS, "supervisor.py")


class Scratch(unittest.TestCase):
    """A feature in a temporary git repository, built and torn down as test_autopilot.Feature does: its
    setUp, tearDown and helpers, without its tests."""


for _name, _value in vars(base.Feature).items():
    if not _name.startswith(("test", "__")):
        setattr(Scratch, _name, _value)


def ago_stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(time.time() - seconds))


class Runtime(Scratch):
    @property
    def folder(self) -> str:
        return os.path.dirname(self.tasks)

    @property
    def state_dir(self) -> str:
        return os.path.join(self.folder, "state")

    def git(self, *args: str, when: float | None = None) -> str:
        env = dict(os.environ)
        if when is not None:
            env.update(GIT_AUTHOR_DATE=f"@{int(when)} +0000", GIT_COMMITTER_DATE=f"@{int(when)} +0000")
        return subprocess.run(["git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              check=True, capture_output=True, text=True, env=env).stdout

    def row(self, cid: str, status: str, branch: str = "-", date: str = "-") -> None:
        text = re.sub(rf"^\| {cid} \|.*$", f"| {cid} | x | {status} | {branch} | - | {date} |", self.resume_text(),
                      count=1, flags=re.M)
        open(os.path.join(self.state_dir, "RESUME.md"), "w").write(text)

    def registry(self, reg) -> None:
        with open(os.path.join(self.state_dir, "runs.json"), "w") as handle:
            json.dump(reg, handle)

    def stalled(self) -> list:
        return [x["card"] for x in self.state()["stalled"]]

    # --- SV1: a RESUME branch cell never reaches git as an option -----------------------------

    def test_a_branch_cell_never_reaches_git_as_an_option(self):
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        pwned = os.path.join(self.root, "pwned")
        for cell in (f"--output={pwned}", f" --output={pwned}", f"`--output={pwned}`", f"-o{pwned}"):
            self.row("T001", "doing", cell, ago_stamp(60))
            s = self.state()
            self.assertIn("T001", s["doing"])
            self.assertFalse(os.path.exists(pwned), cell)
        self.assertIsNone(sv.last_commit(self.root, f"--output={pwned}"))
        self.assertFalse(os.path.exists(pwned))
        if sv.end_of_options():  # git 2.24+: the revision is fenced off as well
            self.assertEqual(sv.end_of_options(), ["--end-of-options"])

    # --- SV13: stalled reads every sign of work -----------------------------------------------

    def test_a_new_branch_is_not_quiet_since_the_tip_it_was_cut_from(self):
        old = time.time() - 3 * 86400
        self.git("commit", "-q", "--allow-empty", "-m", "old main", when=old)
        self.git("branch", "feat-t001")  # cut now from a three-day-old tip
        self.row("T001", "doing", "feat-t001", ago_stamp(3 * 86400))
        self.assertAlmostEqual(sv.last_commit(self.folder, "feat-t001"), time.time(), delta=120)
        self.assertEqual(self.stalled(), [], "its branch was created a moment ago")
        self.git("branch", "-D", "feat-t001")
        self.git("branch", "feat-t001", when=old)  # cut three days ago
        self.assertEqual(self.stalled(), ["T001"], "started three days ago, nothing since")
        self.row("T001", "doing", "feat-t001", ago_stamp(600))
        self.assertEqual(self.stalled(), [], "its RESUME row says it started ten minutes ago")
        self.row("T001", "doing", "feat-t001", ago_stamp(3 * 86400))
        self.git("checkout", "-q", "feat-t001")
        self.git("commit", "-q", "--allow-empty", "-m", "T001 work")
        self.assertEqual(self.stalled(), [], "a fresh commit on its branch")
        self.assertAlmostEqual(sv.last_commit(self.folder, "refs/heads/feat-t001"), time.time(), delta=120)

    def test_a_card_on_the_shared_branch_counts_its_commits_after_another_branch_is_cut(self):
        old = time.time() - 3 * 86400
        self.git("commit", "-q", "--allow-empty", "-m", "old main", when=old)
        branch = self.git("branch", "--show-current").strip()
        self.row("T001", "doing", branch, ago_stamp(3 * 86400))
        self.assertEqual(self.stalled(), ["T001"])
        self.git("commit", "-q", "--allow-empty", "-m", "T001 work")
        self.git("branch", "feat-t003")  # a [P] card's branch, cut from the shared branch's new tip
        self.assertEqual(self.stalled(), [], "its commit counts though another branch holds it too")

    def test_without_a_reflog_only_a_tip_no_other_branch_holds_counts(self):
        old = time.time() - 3 * 86400
        self.git("commit", "-q", "--allow-empty", "-m", "old main", when=old)
        self.git("-c", "core.logAllRefUpdates=false", "branch", "quiet")
        self.assertEqual(self.git("log", "-g", "--format=%gd", "quiet", "--").strip(), "", "no reflog")
        self.assertIsNone(sv.last_commit(self.folder, "quiet"), "the old tip is the other branch's")
        self.git("-c", "core.logAllRefUpdates=false", "checkout", "-q", "quiet")
        self.git("-c", "core.logAllRefUpdates=false", "commit", "-q", "--allow-empty", "-m", "work")
        self.assertAlmostEqual(sv.last_commit(self.folder, "quiet"), time.time(), delta=120)

    def test_a_card_with_no_branch_and_no_hand_off_is_stalled_by_its_resume_date(self):
        self.row("T002", "doing", "-", ago_stamp(3 * 86400))
        self.assertEqual(self.stalled(), ["T002"])
        self.assertEqual(self.state()["stalled"][0]["quiet"], "3d ago")
        self.row("T002", "doing", "-", time.strftime("%Y-%m-%d", time.gmtime()))
        self.assertEqual(self.stalled(), [], "a bare day counts until its end")
        self.row("T002", "doing", "-", "-")
        self.assertEqual(self.stalled(), [], "no sign at all: nothing to measure")
        self.row("T002", "doing", "-", ago_stamp(3 * 86400))
        handoff = os.path.join(self.state_dir, "handoff", "T002.md")
        os.makedirs(os.path.dirname(handoff), exist_ok=True)
        open(handoff, "w").write("# T002\n")
        self.assertEqual(self.stalled(), [], "a fresh hand-off")

    def test_a_running_sessions_output_keeps_its_card_from_looking_stalled(self):
        self.row("T002", "doing", "-", ago_stamp(3 * 86400))
        log = os.path.join(self.state_dir, "runs", "T002-1.jsonl")
        os.makedirs(os.path.dirname(log))
        open(log, "w").write("{}\n")
        run = {"card": "T002", "session": "s1", "started": ago_stamp(3 * 86400), "ended": "", "log": "runs/T002-1.jsonl"}
        self.registry({"runs": [run]})
        self.assertEqual(self.stalled(), [], "its log was written a moment ago")
        os.utime(log, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        self.assertEqual(self.stalled(), ["T002"], "silent for three days")
        self.registry({"runs": [{**run, "started_ts": time.time() - 60}]})
        self.assertEqual(self.stalled(), [], "a session started a minute ago")
        self.registry({"runs": [{**run, "log": ""}]})
        self.assertEqual(self.stalled(), ["T002"], "a run without a log is no sign (not the state folder's time)")
        self.row("T002", "doing", "-", time.strftime("%Y-%m-%d", time.gmtime()))
        self.registry({"runs": [run]})
        self.assertEqual(self.stalled(), ["T002"], "a bare day today does not hide three silent days of its session")
        self.row("T002", "doing", "-", ago_stamp(-6 * 3600))
        self.assertEqual(self.stalled(), ["T002"], "a time six hours ahead (local time written as UTC) says nothing")

    # --- SV15: broken state files never take a view down ---------------------------------------

    def test_broken_runs_json_never_takes_the_report_down(self):
        good = {"card": "T001", "session": "a", "cost": 1.5, "ended": "2026-10-01 10:00Z"}
        for reg in ({"runs": [good, {"card": "T001", "session": "b", "cost": "n/a", "ended": "x"}]},
                    {"runs": [good, {"card": "T001", "session": "c", "cost": float("inf"), "ended": "x"}]},
                    {"runs": [good, {"card": "T001", "session": "d", "cost": "1e999", "ended": "x"}]},
                    {"runs": [good, {"card": "T001", "session": "e", "cost": 10 ** 400, "ended": "x"}]},
                    {"runs": [good, "x", 3, None], "attention": ["T001"], "blocked_on": ["T001"]},
                    {"runs": {"a": 1}, "attention": "T001", "manual": 3, "queued": {"T001": 1}},
                    ["not", "a", "dict"]):
            with self.subTest(reg=reg):
                self.registry(reg)
                s = self.state()
                self.assertIn("Needs you:", sv.render(s))
                json.dumps(s, allow_nan=False)  # the dashboard's JSON.parse takes no NaN or Infinity
                sv.changes(sv.snapshot(s), sv.snapshot(s))
                if isinstance(reg, dict) and isinstance(reg["runs"], list):
                    self.assertEqual(s["autopilot"]["spent_usd"], 1.5)
        for args in ([], ["--json"]):
            out = subprocess.run([sys.executable, SUPERVISOR, self.tasks, *args], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_hand_off_or_screens_folder_removed_while_read_is_skipped(self):
        real = glob.glob

        def racing(pattern, *args, **kwargs):
            found = real(pattern, *args, **kwargs)
            if pattern.endswith(os.path.join("handoff", "*.md")):
                found.append(os.path.join(os.path.dirname(pattern), "T002.md"))  # listed, then removed
            return found

        with mock.patch.object(sv.glob, "glob", racing):
            self.assertNotIn("T002", self.state()["handoffs"])
        os.makedirs(os.path.join(self.state_dir, "screens", "T003"))

        def gone(path):
            raise FileNotFoundError(path)

        with mock.patch.object(sv.os, "listdir", gone):
            self.assertEqual(self.state()["screens"], {})

    # --- SV17: the --wait snapshot ------------------------------------------------------------

    def test_a_broken_snapshot_becomes_the_baseline(self):
        new = sv.snapshot(self.state())
        for saved in (None, [], "x", 3, {}, {"status": 1}, {"ready": []}):
            self.assertIsNone(sv.like(saved, new), saved)
        new = {**new, "ready": ["T001"], "attention": {"T004": "stopped"}, "live": ["T002"], "merge_lock": "held by B2"}
        old = sv.like({"status": new["status"], "ready": "T001", "attention": ["T004"], "extra": 1}, new)
        self.assertEqual(old, new, "what an older or broken snapshot can't say is taken as unchanged")
        old = sv.like({"status": {**new["status"], "T001": "doing"}}, new)
        self.assertEqual(sv.changes(old, new), ["T001: doing → todo"])

    def test_wait_wakes_on_the_merge_lock_live_sessions_and_attention_text(self):
        s = self.state()
        old = sv.snapshot(s)
        self.assertEqual((old["merge_lock"], old["live"], old["attention"]), ("", [], {}))
        lock = os.path.join(self.state_dir, "merge.lock")
        os.makedirs(lock)
        open(os.path.join(lock, "holder"), "w").write("T002 2026-10-09T10:00Z")
        new = sv.snapshot(self.state())
        self.assertNotEqual(old, new)
        self.assertIn("merge lock: free → held by T002 since 2026-10-09T10:00Z", sv.changes(old, new))
        self.assertIn("merge lock: held by T002 since 2026-10-09T10:00Z → free", sv.changes(new, old))
        a, b = {**old, "attention": {"T001": "first"}}, {**old, "attention": {"T001": "second"}}
        self.assertEqual(sv.changes(a, b), ["T001 needs you: second"])
        self.assertEqual(sv.changes(b, old), ["T001 no longer needs you"])
        self.assertEqual(sv.changes(old, {**old, "live": ["B1"]}), ["B1: a session started"])
        self.assertEqual(sv.changes({**old, "live": ["B1"]}, old), ["B1: its session ended"])

    def test_wait_survives_a_broken_snapshot_and_reports_the_next_change(self):
        snap = os.path.join(self.state_dir, ".supervisor.json")
        command = [sys.executable, SUPERVISOR, self.tasks, "--wait", "--interval", "0.2"]
        for broken in ("[]", '{"status": 1}'):
            open(snap, "w").write(broken)
            with self.assertRaises(subprocess.TimeoutExpired, msg=broken):
                subprocess.run(command, capture_output=True, text=True, timeout=3)
            self.assertIsInstance(json.load(open(snap)), dict, "the broken snapshot became the baseline")
        self.assertEqual([p for p in os.listdir(self.state_dir) if p.endswith(".tmp")], [])
        lock = os.path.join(self.state_dir, "merge.lock")
        os.makedirs(lock)
        out = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("merge lock: free → held by a session that has not said who it is", out.stdout)

    def test_save_writes_whole_or_not_at_all(self):
        path = os.path.join(self.state_dir, "x.json")
        sv.save(path, {"a": 1})
        with mock.patch.object(sv.json, "dump", side_effect=TypeError("boom")):
            with self.assertRaises(TypeError):
                sv.save(path, {"a": 2})
        self.assertEqual(json.load(open(path)), {"a": 1}, "the old file stays whole")
        self.assertEqual([p for p in os.listdir(self.state_dir) if p.endswith(".tmp")], [])

    # --- SV18: the dispatcher probe -----------------------------------------------------------

    def test_the_dispatcher_probe_reads_the_pid_before_it_touches_the_lock(self):
        path = os.path.join(self.state_dir, ".autopilot.lock")
        self.assertFalse(sv.dispatcher_alive(self.state_dir), "no lock file")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        for said in ({"pid": dead.pid}, {"pid": 0}, {"pid": -1}, {"pid": 10 ** 30}):
            with open(path, "w") as handle:
                json.dump(said, handle)
            with mock.patch.object(sv.fcntl, "flock", side_effect=AssertionError("probed")):
                self.assertFalse(sv.dispatcher_alive(self.state_dir), said)
        for said in ({"pid": True}, {"pid": "1"}, [], "x"):  # no pid to read: the lock alone says
            with open(path, "w") as handle:
                json.dump(said, handle)
            self.assertFalse(sv.dispatcher_alive(self.state_dir), said)
        open(path, "w").close()
        with open(path) as held:  # acquire() empties the file after it takes the lock, then writes its pid
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertTrue(sv.dispatcher_alive(self.state_dir), "held, pid not written yet")
            with open(path, "w") as handle:
                json.dump({"pid": 1}, handle)  # pid 1 is root's: os.kill says EPERM, not "no such process"
            self.assertTrue(sv.dispatcher_alive(self.state_dir), "a dispatcher under another account")
        self.assertTrue(autopilot.acquire(self.tasks))
        self.assertTrue(sv.dispatcher_alive(self.state_dir), "this process holds it")
        autopilot.release(self.tasks)
        self.assertFalse(sv.dispatcher_alive(self.state_dir), "released, though its pid lives on")
        code = ("import sys, time; sys.path.insert(0, %r); import autopilot; "
                "print(autopilot.acquire(%r), flush=True); time.sleep(60)") % (HOOKS, self.tasks)
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "True")
            self.assertTrue(sv.dispatcher_alive(self.state_dir), "another process dispatches")
            with open(path) as handle:  # the probe left the lock as it was: still not ours to take
                with self.assertRaises(OSError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            child.kill()
            child.wait()
        self.assertFalse(sv.dispatcher_alive(self.state_dir), "its dispatcher died")
        self.assertTrue(autopilot.acquire(self.tasks), "and the next one takes over")

    # --- the git cache ------------------------------------------------------------------------

    def test_the_git_cache_forgets_its_older_ancestry_answers_when_it_grows_too_big(self):
        sv.GIT.clear()
        try:
            sv.GIT[("root", "/x")] = "/x"
            for i in range(sv.GIT_LIMIT):
                sv.GIT[("ancestor", "r", str(i), "x")] = False
            self.assertFalse(sv.is_ancestor(self.root, "0" * 40, "1" * 40))
            self.assertLessEqual(len(sv.GIT), sv.GIT_LIMIT // 2 + 2)
            self.assertIn(("root", "/x"), sv.GIT, "the repo roots and pin histories stay")
            self.assertNotIn(("ancestor", "r", "0", "x"), sv.GIT, "the oldest answers go")
            self.assertIn(("ancestor", "r", str(sv.GIT_LIMIT - 1), "x"), sv.GIT, "the newest stay")
            with mock.patch.object(sv, "git", side_effect=AssertionError("asked git again")):
                self.assertFalse(sv.is_ancestor(self.root, "0" * 40, "1" * 40), "answered from the cache")
        finally:
            sv.GIT.clear()

    # --- SK12, SK20: the drift list -----------------------------------------------------------

    def test_an_owner_card_marked_done_needs_no_hand_off(self):
        autopilot.act(self.tasks, "owner-done", {"card": "T005"})
        s = self.state()
        self.assertEqual(s["status"]["T005"], "done")
        self.assertFalse([d for d in s["drift"] if d.startswith("T005 ")], s["drift"])
        self.finish("T001")
        os.remove(os.path.join(self.state_dir, "handoff", "T001.md"))
        self.assertIn("T001 is done but state/handoff/T001.md is missing", self.state()["drift"])

    def test_every_always_alone_card_in_a_batch_is_drift(self):
        text = open(self.tasks).read()
        text = text.replace("#### T002 [P] — Orders API", "#### T002 [P] — Walk-through of orders")
        text = text.replace("then card T003.`\n", "then card T003.`\n\n**Do:** deploy the login screen.\n", 1)
        text = text.replace("#### T006 — Settings screen", "#### T006 — Results")
        open(self.tasks, "w").write(text)
        resume = self.resume_text().replace("| Stripe | T006 |", "| Stripe | T004 |")
        open(os.path.join(self.state_dir, "RESUME.md"), "w").write(resume)
        self.add_stage_batch(cards="T002, T003, T004, T006")
        drift = self.state()["drift"]
        tail = ": it runs on its own, never in a batch"
        self.assertIn(f"batch B1 holds T002, a walk-through{tail}", drift)
        self.assertIn(f"batch B1 holds T003, a card whose Do asks for the owner's yes{tail}", drift)
        self.assertIn(f"batch B1 holds T004, a card waiting for an owner's decision{tail}", drift)
        self.assertIn(f"batch B1 holds T006, the Results card{tail}", drift)
        for title in ("Close the modal on Escape", "Results list paginates"):  # ordinary cards
            self.assertEqual(sv.alone("T004", {**self.state()["cards"]["T004"], "title": title}), "", title)
        self.assertEqual(sv.alone("T004", {**self.state()["cards"]["T004"], "after_text": "CPA, §5"}),
                         "the Results card", "the close waits for every backlog card (rule 16)")
        autopilot.act(self.tasks, "decision", {"n": "1", "answer": "Stripe"})
        self.assertNotIn(f"batch B1 holds T004, a card waiting for an owner's decision{tail}", self.state()["drift"],
                         "answered: it may share a session now")
        self.set_meta("T004", "after: T001", "after: T001, decision 2")
        resume = self.resume_text().replace("| 1 | Which payment provider?",
                                            "| 2 | Which font? | Inter | | | |\n| 1 | Which payment provider?")
        open(os.path.join(self.state_dir, "RESUME.md"), "w").write(resume)
        self.assertIn(f"batch B1 holds T004, a card waiting for an owner's decision{tail}", self.state()["drift"],
                      "its after: names an open decision")

    # --- the autopilot's first run starts paused ----------------------------------------------

    def test_the_first_autopilot_run_starts_paused(self):
        settings = os.path.join(self.state_dir, "autopilot.json")
        os.remove(settings)
        self.assertTrue(sv.enable(self.tasks))
        s = self.state()
        self.assertFalse(s["autopilot"]["settings"]["auto"])
        self.assertEqual(s["autopilot"]["settings"]["paused_reason"], "new: press Resume to start")
        self.assertIn("autopilot paused: new: press Resume to start", sv.render(s))
        autopilot.change_settings(self.tasks, claude=os.path.join(self.root, "claude"), notify=False)
        autopilot.step(self.tasks)
        time.sleep(0.5)
        self.assertEqual(self.launched(), [], "nothing starts before Resume")
        self.assertFalse(sv.enable(self.tasks), "settings that exist are kept")
        autopilot.act(self.tasks, "settings", {"auto": True})
        cfg = self.state()["autopilot"]["settings"]
        self.assertEqual((cfg["auto"], cfg["paused_reason"]), (True, ""))
        self.assertFalse(sv.enable(self.tasks))
        self.assertTrue(self.state()["autopilot"]["settings"]["auto"], "a later run does not pause it again")
        autopilot.step(self.tasks)
        self.assertEqual([c["card"] for c in self.wait_calls(1)], ["T001"])
        self.settle()

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "no system python3")
    def test_the_supervisor_imports_on_python_3_9(self):
        old = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info < (3, 10))"],
                             capture_output=True, text=True).stdout.strip()
        if old != "True":
            self.skipTest("the system python3 is 3.10 or newer")
        out = subprocess.run(["/usr/bin/python3", SUPERVISOR, "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


SERVE = """import os, sys, webbrowser
sys.path.insert(0, {hooks!r})
import supervisor as sv
real = sv.build
def build(tasks, hours):
    if os.path.exists({mark!r}):
        raise RuntimeError("broken on purpose")
    return real(tasks, hours)
sv.build = build
def opened(url, *args, **kwargs):  # what --open hands the browser
    with open({opened!r}, "w") as handle:
        handle.write(url)
webbrowser.open = opened
sys.argv = ["supervisor.py", {tasks!r}, "--serve", "--open", "--port", "18801"]
sv.main()
"""


class Server(unittest.TestCase):
    """The dashboard server answers every bad request, and a failing read, instead of hanging up."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="supervisor-serve-")
        folder = os.path.join(self.root, "specs", "001-demo")
        os.makedirs(os.path.join(folder, "state"))
        self.tasks = os.path.join(folder, "tasks.md")
        open(self.tasks, "w").write(TASKS.replace("{tasks}", self.tasks))
        open(os.path.join(folder, "state", "RESUME.md"), "w").write(RESUME)
        self.mark = os.path.join(self.root, "broken")
        opened = os.path.join(self.root, "opened")
        code = SERVE.format(hooks=HOOKS, mark=self.mark, tasks=self.tasks, opened=opened)
        env = {k: v for k, v in os.environ.items() if k != "BROWSER"}
        self.proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env={**env, **self.env})
        self.link = re.search(r"http://\S+", self.proc.stdout.readline()).group(0)
        self.port = int(re.search(r":(\d+)/", self.link).group(1))
        self.key = re.search(r"\?k=(\S+)$", self.link).group(1)
        for _ in range(50):
            if os.path.exists(opened) and open(opened).read():
                break
            time.sleep(0.1)
        handed = open(opened).read()
        forwarder = handed[len("file://"):] if handed.startswith("file://") else ""
        if forwarder:  # the browser gets a private page that forwards it to the link, not the link itself
            self.assertIn(f'content="0;url={self.link}"', open(forwarder).read())
            self.assertEqual(os.stat(os.path.dirname(forwarder)).st_mode & 0o077, 0, "only this user may enter")
        else:
            self.assertEqual(handed, self.link, "--open opens the printed link")
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        page = browser.open(self.link).read().decode()
        self.token = re.search(r'name="supervisor-token" content="([^"]+)"', page).group(1)
        if forwarder:
            self.assertFalse(os.path.exists(forwarder), "removed once the dashboard has loaded through it")

    env: dict = {}  # the server's extra environment

    def tearDown(self):
        self.proc.terminate()
        self.proc.communicate(timeout=10)
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, head: str, body: bytes = b"") -> tuple:
        """(status, content type, body) of one raw request; fails on a hang or a dropped connection."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(head.encode() + body)
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
        self.assertTrue(data, "the server hung up without an answer")
        top, _, rest = data.decode().partition("\r\n\r\n")
        kind = re.search(r"^Content-Type: (.*)$", top, re.M | re.I)
        return int(top.split()[1]), kind.group(1).strip() if kind else "", rest

    def post(self, body: bytes, length=None) -> tuple:
        return self.request(f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                            f"X-Supervisor-Token: {self.token}\r\n"
                            f"Content-Length: {len(body) if length is None else length}\r\n\r\n", body)

    def test_bad_posts_get_an_answer(self):
        self.assertEqual(self.post(b"[]")[:1], (400,))
        self.assertIn("must be an object", self.post(b'"x"')[2])
        started = time.time()
        self.assertEqual(self.post(b"{}", -1)[:1], (400,), "a negative length is refused, not read to the end")
        self.assertLess(time.time() - started, 3)
        self.assertEqual(self.post(b"{}", "abc")[0], 400)
        self.assertEqual(self.post(b"{}", sv.MAX_BODY + 1)[0], 413)
        for number in (b"Infinity", b"NaN", b"1e999"):
            self.assertEqual(self.post(b'{"f": "001-demo", "action": "settings", "max_parallel": ' + number + b"}")[:3:2],
                             (400, "bad JSON"), number)
        answer = json.dumps({"f": "001-demo", "action": "decision", "n": "1", "answer": "Stripe"}).encode()
        open(self.mark, "w").close()  # act() reads the state with build(), which now fails
        status, kind, text = self.post(answer)
        self.assertEqual((status, kind, text), (500, "text/plain; charset=utf-8",
                                                "the supervisor failed: RuntimeError: broken on purpose"))
        os.remove(self.mark)
        self.assertEqual(self.post(answer)[0], 200, "the server lives on")
        no_token = self.request(f"POST /api/act HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Length: 2\r\n\r\n", b"[]")
        self.assertEqual(no_token[0], 403, "the token is checked before the body is read")

    def get(self, target: str, cookie: str = "") -> tuple:
        return self.request(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                            + (f"Cookie: {cookie}\r\n" if cookie else "") + "\r\n")

    def test_the_page_and_its_token_go_only_to_the_owners_link(self):
        name = f"spec_grill_{self.port}"
        for target, cookie in (("/", ""), ("/?k=wrong", ""), ("/?f=001-demo", ""), ("/", f"{name}=wrong"),
                               ("/", f"spec_grill_1={self.key}"), ("/", "\x7f=;;="), ("/?k=", f"{name}=")):
            status, kind, text = self.get(target, cookie)
            self.assertEqual((status, kind), (403, "text/html; charset=utf-8"), (target, cookie))
            self.assertNotIn(self.token, text)
            self.assertIn("link printed in the terminal", text)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as conn:
            conn.sendall(f"GET /?k={self.key}&f=001-demo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
            top = conn.recv(65536).decode().split("\r\n\r\n")[0]
        self.assertTrue(top.startswith("HTTP/1.0 303"), top)
        self.assertIn("\r\nLocation: /?f=001-demo\r\n", top, "the key leaves the address bar")
        self.assertIn(f"\r\nSet-Cookie: {name}={self.key}; HttpOnly; SameSite=Lax; Path=/\r\n", top)
        status, _, text = self.get("/?f=001-demo", f"other=1; {name}={self.key}")
        self.assertEqual(status, 200)
        self.assertIn(f'name="supervisor-token" content="{self.token}"', text)
        # the key is in no process's argv or environment, where a card session could read it
        pid = str(self.proc.pid)
        if os.path.exists(f"/proc/{pid}/environ"):
            seen = open(f"/proc/{pid}/cmdline", "rb").read() + open(f"/proc/{pid}/environ", "rb").read()
        else:
            seen = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", pid], capture_output=True).stdout
        self.assertTrue(seen)
        self.assertNotIn(self.key.encode(), seen)

    def test_a_failing_read_answers_500_with_its_error(self):
        get = f"GET /api/state?f=001-demo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
        self.assertEqual(self.request(get)[0], 200)
        open(self.mark, "w").close()
        status, kind, text = self.request(get)
        self.assertEqual((status, kind), (500, "application/json; charset=utf-8"))
        self.assertEqual(json.loads(text), {"error": "RuntimeError: broken on purpose"})
        features = f"GET /api/features HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
        self.assertEqual(self.request(features)[0], 500)
        os.remove(self.mark)
        self.assertEqual(self.request(get)[0], 200, "and recovers")


class ServerOpenedThroughAForwarder(Server):
    """The same, where webbrowser would put the link on a command line ($BROWSER set; Linux)."""

    env = {"BROWSER": "true"}

    def test_the_forwarder_is_used(self):
        self.assertTrue(open(os.path.join(self.root, "opened")).read().startswith("file://"))


if __name__ == "__main__":
    unittest.main()
