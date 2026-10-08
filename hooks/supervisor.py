#!/usr/bin/env python3
"""Spec-Grill supervisor: where a feature's tasks.md stands, read from files only.

    supervisor.py [TASKS]            report once (TASKS: a tasks.md, its feature folder,
                                     or nothing for the newest specs/*/tasks.md here)
    supervisor.py TASKS --json       the same, as JSON
    supervisor.py TASKS --watch      live view for a terminal, redrawn when the state changes
    supervisor.py TASKS --wait       block until the state differs from the last --wait
                                     snapshot, print what changed and the report, exit
    supervisor.py TASKS --serve      dashboard at http://127.0.0.1:8765 for every feature
                                     beside TASKS (local only; add --open to open a browser)
    supervisor.py TASKS --serve --autopilot
                                     the same, plus the dispatcher that starts card sessions
                                     on its own (autopilot.py next to this file)

It reads tasks.md (the checklist and each card's "after:" and "blocks:" fields),
state/RESUME.md (status table, decisions, blockers, deploy lock), state/handoff/*.md,
git (the last commit on a doing card's branch, to spot stalled cards), and, when the
autopilot runs, state/autopilot.json (its settings) and state/runs.json (the sessions it
started). Without --autopilot it writes nothing except the --wait snapshot,
state/.supervisor.json. The dashboard page is dashboard.html next to this file. Standard
library only.
"""

import argparse
import datetime as dt
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time

ID = r"(?:T\d+(?:[A-Z]+\d*)*|CP[A-Z0-9]+)"  # T001, T012A, follow-ups like T042B2 and T042R2A; CPA, CP0, CPEND
ID_RE = re.compile(rf"\b{ID}\b")
RANGE_RE = re.compile(rf"\b({ID})\s*[–-]\s*({ID})\b")
CHECK_RE = re.compile(rf"^- \[([ xX])\] ({ID})((?: \[P\])?)(?: (.+?))?(?: — fulfills .*)?\s*$", re.M)
SECTION_RE = re.compile(r"^#{2,3} (.+?)\s*$", re.M)
COND_RE = re.compile(r"\(([^)]*\bif\b[^)]*)\)")  # "(T036 if German)": a dependency under a condition
BEFORE_RE = re.compile(rf"\bbefore:?\s+({ID})")
HEAD_RE = re.compile(rf"^#{{3,4}} ({ID})((?: \[P\])?) — (.+?)\s*$", re.M)
FINISHED = {"done", "waived"}
STATUSES = {"todo", "doing", "done", "blocked", "waived"}
OPEN_ANSWERS = {"", "-", "—", "?", "tbd", "todo", "open", "pending"}
PIPE_RE = re.compile(r"(?<!\\)\|")  # a cell separator: a pipe not escaped as \|
IMAGE_RE = re.compile(r"^[\w.-]+\.(?:png|jpe?g|webp|gif)$", re.I)
SETTINGS = {  # state/autopilot.json; the dashboard changes them, autopilot.py acts on them
    "auto": False,               # start ready cards on its own (off: only the owner's Start buttons)
    "max_parallel": 3,           # sessions at the same time, at most one of them a card without [P]
    "gate_checkpoints": True,    # after a checkpoint, hold the next stage until the owner approves it
    "approved_gates": [],
    "budget_per_card_usd": 20,   # --max-budget-usd for each session
    "budget_total_usd": 0,       # pause when the sessions together cost this much (0: no cap)
    "permission_mode": "auto",
    "max_attempts": 2,           # sessions per card before it needs the owner (approval resumes aside)
    "quiet_minutes": 45,         # stop a session whose output stays silent this long
    "max_run_hours": 6,
    "notify": True,              # desktop notification when something needs the owner
    "chrome": False,             # give sessions Claude in Chrome (the owner's real browser); one at a time
    "result_grace_s": 30,        # stop a session this long after its final result if it has not exited
    "claude": "claude",
    "paused_reason": "",
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def load_settings(state_dir: str) -> dict:
    """The autopilot's settings; "exists" says whether the feature uses the autopilot at all."""
    found = read_json(os.path.join(state_dir, "autopilot.json"), None)
    out = {**SETTINGS, **(found if isinstance(found, dict) else {})}
    out["exists"] = isinstance(found, dict)
    return out


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def run_alive(run: dict) -> bool:
    """A session the autopilot started is still running: its pid is alive and is still that session
    (a reused pid, or a finished child not yet reaped, does not carry the session id)."""
    if run.get("ended") or not pid_alive(run.get("pid")):
        return False
    try:
        args = subprocess.run(["ps", "-o", "args=", "-p", str(int(run["pid"]))],
                              capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True
    return bool(run.get("session")) and run["session"] in args


def dispatcher_alive(state_dir: str) -> bool:
    """Some process holds the feature's dispatcher lock (autopilot.acquire)."""
    path = os.path.join(state_dir, ".autopilot.lock")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def is_gate(cid: str) -> bool:
    """A build checkpoint (CPA, CPB, …): the stage after it waits for the owner's review."""
    return cid.startswith("CP") and cid not in ("CP0", "CPEND")


def find_tasks(arg: str | None) -> str:
    if arg and os.path.isdir(arg):
        arg = os.path.join(arg, "tasks.md")
    if arg:
        if not os.path.isfile(arg):
            sys.exit(f"supervisor: no such file: {arg}")
        return os.path.abspath(arg)
    found = sorted(glob.glob(os.path.join("specs", "*", "tasks.md")))
    if not found:
        sys.exit("supervisor: no specs/*/tasks.md here; pass the path of a tasks.md")
    return os.path.abspath(found[-1])


# --- tasks.md -------------------------------------------------------------------------


def parse_tasks(text: str) -> tuple[dict, list]:
    cards: dict = {}
    order: list = []

    def card(cid: str) -> dict:
        if cid not in cards:
            cards[cid] = {
                "id": cid, "title": "", "parallel": False, "ticked": False, "after": [],
                "after_text": "", "blocks_text": "", "size": "", "effort": "", "start_with": "", "stage": "",
                "kind": "", "model": "",
            }
            order.append(cid)
        return cards[cid]

    inline = {}
    for m in CHECK_RE.finditer(text):
        c = card(m.group(2))
        c["ticked"] |= m.group(1).lower() == "x"
        c["parallel"] |= bool(m.group(3))
        c["title"] = c["title"] or (m.group(4) or "").strip()  # a bare "- [x] T010B" under a card's heading
        # a §5 card written inline: its meta line and fields indented under the checklist line
        block = re.match(r"(?:\n[ \t]+\S[^\n]*)+", text[m.end():])
        if block:
            inline[m.group(2)] = block.group(0)

    heads = list(HEAD_RE.finditer(text))
    stages = [(m.start(), m.group(1)) for m in SECTION_RE.finditer(text)]
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        stop = re.search(r"^#{1,3} ", body, re.M)
        body = body[: stop.start()] if stop else body
        c = card(m.group(1))
        c["title"] = m.group(3).strip()  # the heading beats a checklist line like "T004B done …"
        c["parallel"] |= bool(m.group(2))
        stage = next((name for pos, name in reversed(stages) if pos < m.start()), "")
        c["stage"] = re.sub(r"\s*\(.*\)$", "", re.sub(r"^\d+\.\s*", "", stage))
        read_meta(c, body)
        c["headed"] = True
    for cid, body in inline.items():
        if not cards[cid].get("headed"):
            read_meta(cards[cid], body)

    for c in cards.values():
        c.pop("headed", None)
        c["after"] = ids_in(c["after_text"], order)
        c["conditional"] = [x for x in ids_in(" ".join(COND_RE.findall(c["after_text"])), order)
                            if x not in c["after"]]
        c["after"] += c["conditional"]  # the condition cannot be read here; waiting is the safe side
    # "after: CPF, §5": the card waits for every backlog card too (the close waits for everything)
    backlog_at = re.search(r"^## 5\.", text, re.M)
    if backlog_at:
        backlog = [m.group(2) for m in CHECK_RE.finditer(text) if m.start() > backlog_at.start()]
        backlog += [m.group(1) for m in HEAD_RE.finditer(text) if m.start() > backlog_at.start()]
        for c in cards.values():
            if "§5" in c["after_text"]:
                c["after"] += [x for x in dict.fromkeys(backlog) if x != c["id"] and x not in c["after"]]
    for c in cards.values():  # "blocks: T009" on a backlog card makes T009 wait for it
        for target in ids_in(c["blocks_text"], order):
            if target in cards and c["id"] not in cards[target]["after"]:
                cards[target]["after"].append(c["id"])
    return cards, order


def read_meta(c: dict, body: str) -> None:
    """A card's fields from its body: the meta line(s) before the first bold field, and Start with."""
    meta = body.split("**", 1)[0]  # the lines before the first bold field
    if found := re.search(r"\bafter:\s*([^·\n]*)", meta):
        c["after_text"] = found.group(1).strip()
    if found := re.search(r"\bblocks:?\s*([^·\n]*)", meta):
        c["blocks_text"] = found.group(1).strip()
    if found := re.search(r"·\s*([SML])\s*(?:·|$)", meta, re.M):
        c["size"] = found.group(1)
    if found := re.search(r"\beffort\s+(\w+)", meta):
        c["effort"] = found.group(1)
    # "· kind frontend ·", "kind: owner", "model `opus`": a field that starts a segment or a line
    if found := re.search(r"(?:^|·)\s*kind\s*:?\s*`?([A-Za-z]+)", meta, re.M | re.I):
        c["kind"] = found.group(1).lower()
    if found := re.search(r"(?:^|·)\s*model\s*:?\s*`?([\w.\[\]-]*\w)", meta, re.M | re.I):
        c["model"] = found.group(1)
    if found := re.search(r"\*\*Start with:\*\*\s*`([^`]+)`", body):
        c["start_with"] = found.group(1)


def runs_as(text: str) -> dict:
    """tasks.md §6: "**Runs as:** owner@example.com via `~/.local/bin/claude-work`" (the account the
    feature's sessions must use, and the CLI launcher logged in as it) and "**App URL:** http://…" (the
    page a Chrome check opens)."""
    out = {"email": "", "launcher": "", "app_url": ""}
    if found := re.search(r"\*\*Runs as\s*:?\*\*:?\s*`?([^\s`]+@[^\s`]+?)`?(?:\s+via\s+`?([^\s`]+)`?)?\s*$", text, re.M | re.I):
        out["email"], out["launcher"] = found.group(1).strip(".,;"), (found.group(2) or "").strip(".,;")
    if found := re.search(r"\*\*App URL\s*:?\*\*:?\s*`?(https?://[^\s`]+)`?", text, re.I):
        out["app_url"] = found.group(1).rstrip(".,;")
    return out


def unattended_deny(text: str) -> list:
    """Tool patterns a card session started by the autopilot may never run (tasks.md §6,
    "**Never unattended:** `Bash(git push:*)`, …"); the owner runs those steps after a yes."""
    found = re.search(r"\*\*Never unattended\s*:?\*\*:?([^\n]*(?:\n(?!\s*\n)[^\n]*)*)", text, re.I)
    return re.findall(r"`([^`]+)`", found.group(1)) if found else []


def ids_in(text: str, order: list) -> list:
    text = re.sub(r"\([^)]*\)", " ", text)  # "(beside T002)" is not a dependency
    out = []
    for m in RANGE_RE.finditer(text):
        a, b = m.group(1), m.group(2)
        if a in order and b in order:
            prefix = re.match(r"[A-Z]+", a).group(0)
            i, j = sorted((order.index(a), order.index(b)))
            out += [x for x in order[i: j + 1] if re.match(r"[A-Z]+", x).group(0) == prefix]
        else:
            out += [a, b]
    out += ID_RE.findall(RANGE_RE.sub(" ", text))
    return list(dict.fromkeys(out))


# --- RESUME.md ------------------------------------------------------------------------


def sections(text: str) -> dict:
    out: dict = {}
    current = None
    for line in text.splitlines():
        if m := re.match(r"^##\s+(.+?)\s*$", line):
            current = m.group(1).lower()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def section(secs: dict, name: str) -> list:
    return next((lines for key, lines in secs.items() if key.startswith(name)), [])


def table(lines: list) -> list:
    rows = [line.strip() for line in lines if line.strip().startswith("|")]
    if not rows:
        return []
    cells = lambda line: [cell.strip() for cell in PIPE_RE.split(line.strip().strip("|"))]  # noqa: E731
    head = [h.lower() for h in cells(rows[0])]
    out = []
    for row in rows[1:]:
        values = cells(row)
        if all(set(v) <= set("-: ") for v in values):
            continue
        out.append(dict(zip(head, values)))
    return out


def column(row: dict, name: str) -> str:
    return next((v for k, v in row.items() if k.startswith(name)), "")


def parse_resume(text: str) -> dict:
    secs = sections(text)
    status = {}
    for row in table(section(secs, "status")):
        found = ID_RE.search(column(row, "card"))
        if not found:
            continue
        words = column(row, "status").lower().split()
        status[found.group(0)] = {
            "status": words[0] if words else "",
            "branch": column(row, "branch").strip("`"),
            "commit": column(row, "commit").strip("`"),
            "date": column(row, "date"),
        }
    decisions = []
    for row in table(section(secs, "decisions")):
        answer = column(row, "answer")
        plain = answer.lower().strip(" .")
        decisions.append({
            "n": column(row, "#"),
            "question": column(row, "question"),
            "before": ID_RE.findall(column(row, "needed")),
            "recommended": column(row, "recommend"),
            "answer": answer,
            "open": plain in OPEN_ANSWERS or plain.startswith("deferred"),
        })
    approvals = []
    for row in table(section(secs, "approvals")):
        n = column(row, "#")
        if not n or n.startswith("<"):
            continue
        card = ID_RE.search(column(row, "card"))
        words = column(row, "status").lower().split()
        approvals.append({
            "n": n, "card": card.group(0) if card else "", "step": column(row, "step"),
            "why": column(row, "why"), "status": words[0].strip("`*") if words else "pending",
            "answer": column(row, "answer"),
        })
    blockers = []
    for line in section(secs, "blockers"):
        item = line.strip()
        if not item.startswith("- "):
            continue
        item = item[2:].strip()
        if item.lower().strip("().") in ("", "none") or item.startswith("<"):
            continue
        if item.lower().startswith("(resolved"):  # kept in RESUME as history, no longer blocks anyone
            continue
        if any(b["text"] == item for b in blockers):
            continue  # the same blocker written twice blocks once
        blockers.append({"text": item, "cards": blocker_subjects(item)})
    lock = next((line.strip() for line in section(secs, "deploy lock") if line.strip()), "")
    return {"status": status, "decisions": decisions, "approvals": approvals, "blockers": blockers, "lock": lock}


def blocker_subjects(item: str) -> list:
    """The cards a blocker holds up: the card it opens with ("T037 (date): …", "T016C: …"), else the
    cards after "before" ("<precondition> before T005"), else the card it opens with, else none."""
    lead = re.match(rf"\s*({ID})\b(\s*[(:])?", item)
    if lead and lead.group(2):
        return [lead.group(1)]
    before = BEFORE_RE.findall(item)
    if before:
        return list(dict.fromkeys(before))
    return [lead.group(1)] if lead else []


# --- the picture ----------------------------------------------------------------------


def last_commit(repo: str, branch: str) -> float | None:
    if not branch or branch in ("-", "—"):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%ct", branch],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def recent_commits(folder: str, cards: dict, rows: dict, limit: int = 25) -> list:
    """The newest commits on every branch of the repository holding the feature, each matched to the
    card it belongs to (a card id in its subject, else the commit recorded in RESUME's status row)."""
    try:
        root = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(["git", "-C", root or folder, "log", "--all", "--date-order", f"-n{limit}",
                              "--format=%H%x1f%ct%x1f%s%x1f%D"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    recorded = {}
    for cid, row in rows.items():
        commit = re.sub(r"[^0-9a-f]", "", (row.get("commit") or "").lower())
        if len(commit) >= 7:
            recorded[commit[:7]] = cid
    items = []
    for line in out.splitlines():
        parts = (line.split("\x1f") + ["", "", "", ""])[:4]
        sha, stamp, subject, refs = parts
        if not sha:
            continue
        card = next((x for x in ID_RE.findall(subject) if x in cards), "") or recorded.get(sha[:7], "")
        branch = next((r.strip().replace("HEAD -> ", "") for r in refs.split(",") if r.strip() and "tag:" not in r), "")
        items.append({"hash": sha[:10], "ts": int(stamp or 0), "subject": subject[:240], "branch": branch, "card": card})
    return items


def ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def build(tasks: str, stale_hours: float) -> dict:
    text = read(tasks)
    cards, order = parse_tasks(text)
    folder = os.path.dirname(tasks)
    state_dir = os.path.join(folder, "state")
    resume_path = os.path.join(state_dir, "RESUME.md")
    has_resume = os.path.isfile(resume_path)
    resume = parse_resume(read(resume_path))
    handoff_dir = os.path.join(state_dir, "handoff")
    handoffs = {}
    for path in glob.glob(os.path.join(handoff_dir, "*.md")):
        handoffs[os.path.basename(path)[:-3]] = os.path.getmtime(path)

    status = {}
    for cid in order:
        row = resume["status"].get(cid)
        if row and row["status"] in STATUSES:
            status[cid] = row["status"]
        else:
            status[cid] = "done" if cards[cid]["ticked"] else "todo"

    settings = load_settings(state_dir)
    # a finished checkpoint holds the next stage until the owner has looked at it (autopilot only)
    gates_open = [c for c in order if is_gate(c) and status[c] == "done"
                  and settings["exists"] and settings["gate_checkpoints"]
                  and c not in settings["approved_gates"]]
    gates_open = [g for g in gates_open if any(g in cards[c]["after"] and status[c] not in FINISHED for c in order)]
    open_decisions = [d for d in resume["decisions"] if d["open"]]
    # a blocker about finished cards only is history: it stays in RESUME, not in "Needs you"
    active_blockers = [b for b in resume["blockers"]
                       if not b["cards"] or any(status.get(c) not in FINISHED for c in b["cards"])]
    waits: dict = {}
    for cid in order:
        if status[cid] in FINISHED:
            continue
        reasons = []
        for dep in cards[cid]["after"]:
            if status.get(dep) not in FINISHED:
                reasons.append({"kind": "card", "on": dep, "status": status.get(dep, "unknown"),
                                "conditional": dep in cards[cid]["conditional"]})
        for d in open_decisions:
            if cid in d["before"]:
                reasons.append({"kind": "owner", "on": f"decision {d['n']}: {d['question']}"})
        for b in active_blockers:
            if cid in b["cards"]:
                reasons.append({"kind": "blocker", "on": b["text"]})
        for dep in cards[cid]["after"]:
            if dep in gates_open:
                reasons.append({"kind": "gate", "on": dep})
        if status[cid] == "blocked" and not reasons:
            reasons.append({"kind": "blocker", "on": "marked blocked in RESUME"})
        waits[cid] = reasons

    registry = read_json(os.path.join(state_dir, "runs.json"), {})
    registry = registry if isinstance(registry, dict) else {}
    alive = {id(r): run_alive(r) for r in registry.get("runs", []) if not r.get("ended")}
    live_cards = [c for c in order if status[c] not in FINISHED and any(
        r.get("card") == c and alive.get(id(r)) for r in registry.get("runs", []))]
    # a session the autopilot started is running even before it marks its row doing
    doing = [c for c in order if status[c] == "doing" or c in live_cards]
    serial_doing = [c for c in doing if not cards[c]["parallel"]]
    ready = []
    for cid in order:
        if status[cid] != "todo" or waits[cid] or cid in live_cards:
            continue
        if not cards[cid]["parallel"] and serial_doing:
            # cards without [P] share the integration worktree: one at a time
            waits[cid].append({"kind": "worktree", "on": serial_doing[0], "status": "doing"})
            continue
        ready.append(cid)

    unblocks = {}
    for cid in doing + ready:
        unblocks[cid] = [
            other for other in order
            if waits.get(other) and all(w.get("on") == cid for w in waits[other])
        ]

    stamp = now().timestamp()
    stalled = []
    for cid in doing:
        row = resume["status"].get(cid, {})
        last = last_commit(folder, row.get("branch", ""))
        if cid in handoffs:
            last = max(last or 0, handoffs[cid])
        if last and stamp - last > stale_hours * 3600:
            stalled.append({"card": cid, "quiet": ago(stamp - last)})

    drift = []
    if has_resume:
        for cid in order:
            row = resume["status"].get(cid)
            if not row:
                drift.append(f"{cid} is in tasks.md but has no row in RESUME's status table")
            elif cards[cid]["ticked"] and row["status"] not in FINISHED:
                drift.append(f"{cid} is ticked in tasks.md but RESUME says {row['status'] or 'nothing'}")
            elif row["status"] in FINISHED and not cards[cid]["ticked"]:
                drift.append(f"{cid} is {row['status']} in RESUME but not ticked in tasks.md")
        for cid in resume["status"]:
            if cid not in cards:
                drift.append(f"RESUME has a row for {cid}, which tasks.md does not define")
    for cid in order:
        if status[cid] == "done" and cid not in handoffs and has_resume:
            drift.append(f"{cid} is done but state/handoff/{cid}.md is missing")
        for dep in cards[cid]["after"]:
            if dep not in cards:
                drift.append(f"{cid} waits for {dep}, which tasks.md does not define")
    lock = resume["lock"]
    holder = ID_RE.search(lock or "")
    if holder and status.get(holder.group(0)) in FINISHED:
        drift.append(f"the deploy lock is still held by {holder.group(0)}, which is {status[holder.group(0)]}")

    runs: dict = {}
    # a resumed session reports its running total, so a session costs the most any of its runs reported
    session_cost: dict = {}
    for run in registry.get("runs", []):
        key = (run.get("card", ""), run.get("session", ""))
        session_cost[key] = max(session_cost.get(key, 0.0), float(run.get("cost") or 0))
    for run in registry.get("runs", []):
        cid = run.get("card", "")
        live = bool(alive.get(id(run)))
        entry = runs.setdefault(cid, {"sessions": 0, "cost": 0.0})
        entry["sessions"] += 1
        entry["cost"] = round(sum(v for (c, _), v in session_cost.items() if c == cid), 4)
        entry.update({
            "live": live, "session": run.get("session", ""), "reason": run.get("reason", ""),
            "started": run.get("started", ""), "ended": run.get("ended", ""),
            "result": run.get("result", ""), "error": run.get("error", ""),
        })
    attention = registry.get("attention", {})
    runs_file = os.path.join(state_dir, "runs.json")
    beat = os.path.getmtime(runs_file) if os.path.exists(runs_file) else None
    pending = [a for a in resume["approvals"] if a["status"] == "pending"]
    answered = [a for a in resume["approvals"] if a["status"] in ("approved", "rejected")]
    yours = [c for c in order if cards[c]["kind"] == "owner" and status[c] not in FINISHED and not waits[c]]

    balance: dict = {}
    for cid in order:
        kind = cards[cid]["kind"]
        if kind in ("backend", "frontend", "fullstack"):
            entry = balance.setdefault(kind, {"done": 0, "total": 0})
            entry["total"] += 1
            entry["done"] += status[cid] in FINISHED
    lopsided = ""
    back, front = balance.get("backend"), balance.get("frontend")
    if back and front and back["done"] / back["total"] - front["done"] / front["total"] >= 0.4:
        lopsided = (f"the back end is {round(100 * back['done'] / back['total'])}% done and the front end "
                    f"{round(100 * front['done'] / front['total'])}%: the interface is falling behind")

    screens = {}
    for path in sorted(glob.glob(os.path.join(state_dir, "screens", "*"))):
        name = os.path.basename(path)
        if re.fullmatch(ID, name) and os.path.isdir(path):
            files = sorted(f for f in os.listdir(path) if IMAGE_RE.match(f))
            if files:
                screens[name] = files

    done = [c for c in order if status[c] == "done"]
    waived = [c for c in order if status[c] == "waived"]
    last_handoff = max(handoffs.items(), key=lambda kv: kv[1]) if handoffs else None
    title = re.search(r"^# Tasks:\s*(.+)$", text, re.M)
    return {
        "feature": title.group(1).strip() if title else os.path.basename(folder),
        "tasks": tasks,
        "resume": resume_path if has_resume else None,
        "checked_at": now().strftime("%Y-%m-%d %H:%MZ"),
        "total": len(order),
        "done": done,
        "waived": waived,
        "doing": doing,
        "ready": ready,
        "waiting": {c: w for c, w in waits.items() if w},
        "unblocks": unblocks,
        "open_decisions": open_decisions,
        "blockers": active_blockers,
        "lock": lock,
        "stalled": stalled,
        "drift": drift,
        "handoffs": sorted(handoffs),
        "last_handoff": {"card": last_handoff[0], "ago": ago(stamp - last_handoff[1])} if last_handoff else None,
        "recent_handoffs": [
            {"card": card, "ago": ago(stamp - mtime)}
            for card, mtime in sorted(handoffs.items(), key=lambda kv: -kv[1])[:8]
        ],
        "status": status,
        "cards": {c: cards[c] for c in order},
        "rows": resume["status"],
        "commits": recent_commits(folder, cards, resume["status"]),
        "now": stamp,
        "approvals": pending,
        "approvals_answered": answered,
        "gates": gates_open,
        "yours": yours,
        "balance": balance,
        "lopsided": lopsided,
        "screens": screens,
        "autopilot": {
            "used": settings["exists"],
            "dispatcher": dispatcher_alive(state_dir),
            "settings": {k: v for k, v in settings.items() if k != "exists"},
            "runs": runs,
            "live": [c for c, r in runs.items() if r["live"]],
            "attention": attention,
            "manual": registry.get("manual", []),
            "queued": [c for c in registry.get("queued", []) if c in cards and status[c] not in FINISHED],
            "blocked_on": {c: v for c, v in registry.get("blocked_on", {}).items() if c in cards and status[c] not in FINISHED},
            "unkinded": [c for c in order if not cards[c]["kind"] and status[c] not in FINISHED],
            "beat": beat,
            "spent_usd": round(sum(r["cost"] for r in runs.values()), 2),
            "runs_as": runs_as(text),
            "account": registry.get("account"),
            "chrome_check": registry.get("chrome_check"),
            "deny": unattended_deny(text),
        },
    }


# --- output ---------------------------------------------------------------------------


def bar(done: int, total: int) -> str:
    share = done / total if total else 0
    cells = round(share * 20)
    return f"[{'█' * cells}{'░' * (20 - cells)}] {round(share * 100)}%"


def label(s: dict, cid: str) -> str:
    c = s["cards"][cid]
    return f"{cid}{' [P]' if c['parallel'] else ''} {c['title']}".strip()


def render(s: dict) -> str:
    finished = len(s["done"]) + len(s["waived"])
    lines = [
        f"{s['feature']} · {s['checked_at']}",
        f"{bar(finished, s['total'])} · {finished}/{s['total']} cards finished"
        + (f" ({len(s['waived'])} waived)" if s["waived"] else ""),
    ]
    if not s["resume"]:
        lines.append("No state/RESUME.md yet (T001 creates it); statuses come from the checklist ticks.")
    if s["last_handoff"]:
        lines.append(f"Last hand-off: {s['last_handoff']['card']} ({s['last_handoff']['ago']})")
    lines.append(f"Done: {', '.join(s['done']) or 'nothing yet'}")
    auto = s["autopilot"]
    if auto["used"]:
        state = ("on" if auto["settings"]["auto"] else "paused") if auto["dispatcher"] else "not running"
        lines.append(f"Autopilot: {state} · sessions live: {', '.join(auto['live']) or 'none'}"
                     f" · spent ${auto['spent_usd']:.2f}")
    if s["commits"]:
        c = s["commits"][0]
        lines.append(f"Latest commit: {c['hash'][:7]} {c['subject']} ({ago(s['now'] - c['ts'])}"
                     + (f", {c['card']}" if c["card"] else "") + ")")
    if s["balance"]:
        lines.append("Balance: " + ", ".join(f"{k} {v['done']}/{v['total']}" for k, v in s["balance"].items()))

    lines.append("")
    lines.append("Running now:")
    for cid in s["doing"]:
        row = s["rows"].get(cid, {})
        blank = ("", "-", "—")
        extra = ", ".join(x for x in (row.get("branch") not in blank and row.get("branch"),
                                      row.get("date") not in blank and f"since {row['date']}") if x)
        opens = s["unblocks"].get(cid)
        lines.append(f"  {label(s, cid)}" + (f" ({extra})" if extra else ""))
        if opens:
            lines.append(f"    finishing it unblocks {', '.join(opens)}")
    if not s["doing"]:
        lines.append("  nothing")

    lines.append("")
    lines.append("Ready to run next:")
    for cid in s["ready"]:
        c = s["cards"][cid]
        meta = " · ".join(x for x in (c["size"], c["effort"] and f"effort {c['effort']}") if x)
        lines.append(f"  {label(s, cid)}" + (f" · {meta}" if meta else ""))
        if c["start_with"]:
            lines.append(f"    Start with: {c['start_with']}")
        if s["unblocks"].get(cid):
            lines.append(f"    finishing it unblocks {', '.join(s['unblocks'][cid])}")
    if not s["ready"]:
        lines.append("  nothing" + (" until a running card finishes" if s["doing"] else ""))
    parallel = [c for c in s["ready"] if s["cards"][c]["parallel"]]
    if len(s["ready"]) > 1 and parallel:
        lines.append(f"  May run side by side, each in its own worktree: {', '.join(parallel)}"
                     " (plus at most one card without [P])")

    lines.append("")
    lines.append("Waiting:")
    for cid, reasons in s["waiting"].items():
        parts = []
        for r in reasons:
            if r["kind"] == "card":
                parts.append(f"{r['on']} ({r['status']}{', conditional' if r.get('conditional') else ''})")
            elif r["kind"] == "worktree":
                parts.append(f"the integration worktree ({r['on']} is doing)")
            elif r["kind"] == "owner":
                parts.append(f"you: {r['on']}")
            elif r["kind"] == "gate":
                parts.append(f"your review of stage checkpoint {r['on']}")
            else:
                parts.append(f"blocker: {r['on']}")
        lines.append(f"  {label(s, cid)} waits for {'; '.join(parts)}")
    if not s["waiting"]:
        lines.append("  nobody")

    auto = s["autopilot"]
    needs = [f"approval {a['n']} for {a['card']}: {a['step']}" for a in s["approvals"]]
    needs += [f"review stage checkpoint {g} (screens, findings), then approve it on the dashboard" for g in s["gates"]]
    needs += [f"{c} needs you: {why}" for c, why in auto["attention"].items()]
    needs += [f"{c} is your card: {s['cards'][c]['title']}" for c in s["yours"]]
    needs += [f"decision {d['n']}: {d['question']} (needed before {', '.join(d['before']) or '?'})"
              + (f"; recommended: {d['recommended']}" if d.get("recommended") else "")
              for d in s["open_decisions"]]
    needs += [f"blocker: {b['text']}" for b in s["blockers"]]
    needs += [f"{x['card']} is doing but quiet for {x['quiet'][:-4]}: check its session" for x in s["stalled"]]
    if s["lopsided"]:
        needs.append(f"balance: {s['lopsided']}")
    if auto["settings"]["paused_reason"]:
        needs.append(f"autopilot paused: {auto['settings']['paused_reason']}")
    if auto["used"] and auto["unkinded"]:
        needs.append(f"no kind on {', '.join(auto['unkinded'])}: the autopilot won't start them until their"
                     " meta line says `kind backend|frontend|fullstack|owner`")
    lines.append("")
    lines.append("Needs you:")
    lines += [f"  {n}" for n in needs] or ["  nothing"]
    if s["lock"] and s["lock"].lower() != "free":
        lines.append(f"Deploy lock: {s['lock']}")
    if s["drift"]:
        lines.append("")
        lines.append("Drift (files disagree):")
        lines += [f"  {d}" for d in s["drift"]]
    return "\n".join(lines)


def snapshot(s: dict) -> dict:
    return {
        "status": s["status"],
        "ready": s["ready"],
        "open_decisions": [f"{d['n']}: {d['question']}" for d in s["open_decisions"]],
        "blockers": [b["text"] for b in s["blockers"]],
        "lock": s["lock"],
        "handoffs": s["handoffs"],
        "stalled": [x["card"] for x in s["stalled"]],
        "drift": s["drift"],
        "approvals": [f"{a['n']} {a['card']}: {a['step']}" for a in s["approvals"]],
        "gates": s["gates"],
        "attention": sorted(s["autopilot"]["attention"]),
        "paused": s["autopilot"]["settings"]["paused_reason"],
        "commits": [f"{c['hash'][:7]} {c['subject']}" for c in s["commits"][:10]],
    }


def changes(old: dict, new: dict) -> list:
    out = []
    for cid, st in new["status"].items():
        before = old["status"].get(cid)
        if before is None:
            out.append(f"new card {cid} ({st})")
        elif before != st:
            out.append(f"{cid}: {before} → {st}")
    out += [f"{cid} was removed from tasks.md" for cid in old["status"] if cid not in new["status"]]
    out += [f"{cid} is now ready" for cid in new["ready"] if cid not in old["ready"]]
    out += [f"hand-off {cid}.md written" for cid in new["handoffs"] if cid not in old["handoffs"]]
    out += [f"decision answered: {d}" for d in old["open_decisions"] if d not in new["open_decisions"]]
    out += [f"decision needed: {d}" for d in new["open_decisions"] if d not in old["open_decisions"]]
    out += [f"blocker cleared: {b}" for b in old["blockers"] if b not in new["blockers"]]
    out += [f"new blocker: {b}" for b in new["blockers"] if b not in old["blockers"]]
    if old["lock"] != new["lock"]:
        out.append(f"deploy lock: {old['lock'] or 'unset'} → {new['lock'] or 'unset'}")
    out += [f"{cid} looks stalled" for cid in new["stalled"] if cid not in old["stalled"]]
    out += [f"drift: {d}" for d in new["drift"] if d not in old["drift"]]
    out += [f"approval needed: {a}" for a in new.get("approvals", []) if a not in old.get("approvals", [])]
    out += [f"stage {g} finished and waits for your review" for g in new.get("gates", []) if g not in old.get("gates", [])]
    out += [f"{c} needs you" for c in new.get("attention", []) if c not in old.get("attention", [])]
    out += [f"new commit {c}" for c in new.get("commits", []) if c not in old.get("commits", [])][:5]
    if new.get("paused") and new.get("paused") != old.get("paused"):
        out.append(f"autopilot paused: {new['paused']}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Where a Spec-Grill tasks.md stands.")
    parser.add_argument("tasks", nargs="?", help="tasks.md, its feature folder, or nothing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the picture as JSON")
    mode.add_argument("--watch", action="store_true", help="live view, redrawn on change")
    mode.add_argument("--wait", action="store_true", help="block until the state changes")
    mode.add_argument("--serve", action="store_true", help="serve the dashboard on localhost")
    parser.add_argument("--autopilot", action="store_true",
                        help="also run the dispatcher that starts card sessions (with --serve, or alone)")
    parser.add_argument("--port", type=int, default=8765, help="dashboard port (8765)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--interval", type=float, default=5, help="seconds between reads (5)")
    parser.add_argument("--stale-hours", type=float, default=4,
                        help="a doing card with no commit or hand-off for this long is stalled (4)")
    args = parser.parse_args()
    tasks = find_tasks(args.tasks)

    if args.json:
        print(json.dumps(build(tasks, args.stale_hours), indent=2, ensure_ascii=False))
    elif args.watch:
        shown, drawn = None, 0.0
        try:
            while True:
                body = render(build(tasks, args.stale_hours))
                key = body.split("\n", 1)[1]
                if key != shown or time.time() - drawn > 60:
                    sys.stdout.write("\033[2J\033[H" + body + f"\n\nwatching every {args.interval:g}s · Ctrl-C stops\n")
                    sys.stdout.flush()
                    shown, drawn = key, time.time()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    elif args.serve:
        serve(tasks, args.port, args.stale_hours, args.open, args.autopilot)
    elif args.autopilot:
        import threading
        import autopilot
        stop = threading.Event()
        enable(tasks)
        print(f"Autopilot: dispatching {tasks} (Ctrl-C stops; running sessions keep going)", flush=True)
        try:
            autopilot.loop(lambda: users(tasks), args.interval, stop, lambda line: print(line, flush=True))
        except KeyboardInterrupt:
            stop.set()
    elif args.wait:
        path = os.path.join(os.path.dirname(tasks), "state", ".supervisor.json")
        try:
            with open(path, encoding="utf-8") as handle:
                old = json.load(handle)
        except (OSError, ValueError):
            old = None
        while True:
            state = build(tasks, args.stale_hours)
            new = snapshot(state)
            if old is None:
                old = new  # first run: this is the baseline
                save(path, new)
            elif new != old:
                save(path, new)
                print("Changed:")
                print("\n".join(f"  {line}" for line in changes(old, new)) or "  (details only)")
                print()
                print(render(state))
                return
            time.sleep(args.interval)
    else:
        print(render(build(tasks, args.stale_hours)))


def enable(tasks: str) -> None:
    """--autopilot on a feature that has never used it: create its settings, switched on."""
    path = os.path.join(os.path.dirname(tasks), "state", "autopilot.json")
    if not os.path.exists(path):
        save(path, {**{k: v for k, v in SETTINGS.items()}, "auto": True})


def users(tasks: str) -> list:
    """Every feature beside tasks that uses the autopilot."""
    specs = os.path.dirname(os.path.dirname(tasks))
    return sorted(p for p in glob.glob(os.path.join(specs, "*", "tasks.md"))
                  if os.path.exists(os.path.join(os.path.dirname(p), "state", "autopilot.json")))


def serve(tasks: str, port: int, stale_hours: float, open_browser: bool, dispatch: bool = False) -> None:
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    specs = os.path.dirname(os.path.dirname(tasks))
    default = os.path.basename(os.path.dirname(tasks))
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    token = secrets.token_urlsafe(24)  # the page carries it; other sites can't read it, so can't post

    def features() -> list:
        return sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(specs, "*", "tasks.md")))

    def tasks_of(name: str) -> str | None:
        name = name or default
        return os.path.join(specs, name, "tasks.md") if name in features() else None  # no paths from the URL

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def send(self, code: int, body: str, kind: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.frame_guard()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def frame_guard(self) -> None:  # the page has buttons: no other site may frame it
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

        def local(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                self.send(403, "local only", "text/plain")
                return False
            return True

        def do_POST(self) -> None:
            if not self.local():
                return
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
                return self.send(403, "cross-site request refused", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-Supervisor-Token", ""), token):
                return self.send(403, "missing or wrong token; reload the page", "text/plain")
            url = urlparse(self.path)
            if url.path != "/api/act":
                return self.send(404, "not found", "text/plain")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.send(400, "bad JSON", "text/plain")
            path = tasks_of(str(data.get("f", "")))
            if not path:
                return self.send(404, "no such feature", "text/plain")
            import autopilot
            if data.get("action") in ("start", "retry") and not (dispatch and autopilot.holds(path)):
                return self.send(409, "only the dashboard running the autopilot for this feature starts sessions"
                                 " (supervisor.py … --serve --autopilot)", "text/plain")
            try:
                message = autopilot.act(path, str(data.get("action", "")), data)
            except autopilot.Refused as error:
                return self.send(409, str(error), "text/plain; charset=utf-8")
            self.send(200, message, "text/plain; charset=utf-8")

        def do_GET(self) -> None:
            if not self.local():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            name = query.get("f", [""])[0]
            if url.path == "/":
                html = read(page) or "dashboard.html is missing"
                meta = f'<meta name="supervisor-token" content="{token}"><meta name="supervisor-dispatch" content="{int(dispatch)}">'
                self.send(200, html.replace("</head>", meta + "</head>", 1), "text/html; charset=utf-8")
            elif url.path == "/api/features":
                items = []
                for feature in features():
                    state = build(os.path.join(specs, feature, "tasks.md"), stale_hours)
                    items.append({
                        "id": feature, "feature": state["feature"], "total": state["total"],
                        "finished": len(state["done"]) + len(state["waived"]),
                    })
                self.send(200, json.dumps({"default": default, "features": items}), "application/json")
            elif url.path == "/api/state" and (path := tasks_of(name)):
                self.send(200, json.dumps(build(path, stale_hours), ensure_ascii=False),
                          "application/json; charset=utf-8")
            elif url.path == "/api/handoff" and (path := tasks_of(name)):
                card = query.get("card", [""])[0]
                text = read(os.path.join(os.path.dirname(path), "state", "handoff", f"{card}.md")) \
                    if re.fullmatch(ID, card) else ""
                self.send(200 if text else 404, text or "no hand-off", "text/plain; charset=utf-8")
            elif url.path == "/api/live" and (path := tasks_of(name)):
                import autopilot
                card = query.get("card", [""])[0]
                if not re.fullmatch(ID, card):
                    return self.send(404, "no such card", "text/plain")
                try:
                    after = int(query.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                view = autopilot.live_view(path, card, after, events=query.get("events", ["1"])[0] != "0")
                self.send(200, json.dumps(view, ensure_ascii=False), "application/json; charset=utf-8")
            elif url.path == "/api/log" and (path := tasks_of(name)):
                import autopilot
                card = query.get("card", [""])[0]
                text = autopilot.log_tail(path, card) if re.fullmatch(ID, card) else ""
                self.send(200 if text else 404, text or "no log", "text/plain; charset=utf-8")
            elif url.path == "/screen" and (path := tasks_of(name)):
                card, file = query.get("card", [""])[0], query.get("file", [""])[0]
                full = os.path.join(os.path.dirname(path), "state", "screens", card, file)
                if not (re.fullmatch(ID, card) and IMAGE_RE.match(file) and os.path.isfile(full)):
                    return self.send(404, "not found", "text/plain")
                with open(full, "rb") as handle:
                    data = handle.read()
                ext = file.rsplit(".", 1)[1].lower()
                self.send_response(200)
                self.send_header("Content-Type", "image/" + {"jpg": "jpeg"}.get(ext, ext))
                self.frame_guard()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send(404, "not found", "text/plain")

    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"supervisor: ports {port}-{port + 19} are all busy")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Dashboard: {url} (Ctrl-C stops)", flush=True)
    stop = threading.Event()
    if dispatch:
        import autopilot
        enable(tasks)
        worker = threading.Thread(target=autopilot.loop, daemon=True,
                                  args=(lambda: users(tasks), 5, stop, lambda line: print(line, flush=True)))
        worker.start()
        print("Autopilot: dispatching; running sessions keep going if this stops", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if dispatch:
            worker.join(timeout=10)


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)


if __name__ == "__main__":
    main()
