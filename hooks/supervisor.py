#!/usr/bin/env python3
"""Spec-Grill supervisor: where a feature's tasks.md stands, read from files only.

    supervisor.py [TASKS]            report once (TASKS: a tasks.md, its feature folder,
                                     or nothing for the newest specs/*/tasks.md here)
    supervisor.py TASKS --json       the same, as JSON
    supervisor.py TASKS --watch      live view for a terminal, redrawn when the state changes
    supervisor.py TASKS --wait       block until the state differs from the last --wait
                                     snapshot, print what changed and the report, exit
    supervisor.py TASKS --lessons    what each card took (runs, tries, cost, hours), for the
                                     close card's retro
    supervisor.py TASKS --serve      dashboard at http://127.0.0.1:8765 for every feature
                                     beside TASKS (local only; open the link it prints, which
                                     carries this launch's key, or add --open to open a browser)
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

from __future__ import annotations  # `X | None` annotations on Python 3.9

import argparse
import datetime as dt
import fcntl
import fnmatch
import glob
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

# T001, T012A, follow-ups like T042B2 and T042R2A; CPA, CP0, CPEND (no nested quantifier: no backtracking blow-up)
ID = r"(?:T\d+[A-Z0-9]*|CP[A-Z0-9]+)"
ID_RE = re.compile(rf"\b{ID}\b")
RANGE_RE = re.compile(rf"\b({ID})\s*[–-]\s*({ID})\b")
# "- [ ] T012 [P] Title", also indented, with "*", a tab, "T012: Title", or "[P]" after the title (group 5)
CHECK_RE = re.compile(rf"^[ \t]*[-*][ \t]+\[([ xX])\][ \t]+({ID}):?((?:[ \t]+\[P\])?)(?:[ \t]+(.+?))?"
                      rf"((?:[ \t]+\[P\])?)(?: — fulfills .*)?\s*$", re.M)
SECTION_RE = re.compile(r"^#{2,3} (.+?)\s*$", re.M)
# "(T036 if German)", "(T036 (de) if German)": a dependency under a condition
COND_RE = re.compile(r"\(((?:[^()]|\([^()]*\))*\bif\b(?:[^()]|\([^()]*\))*)\)")
BEFORE_RE = re.compile(rf"\bbefore:?\s+({ID})")
# "#### T012 [P] — Title"; "-", "–" or ":" also separate the title, but "### T001 – T003 Setup" is a range
HEAD_RE = re.compile(rf"^#{{3,4}}[ \t]+({ID})((?:[ \t]+\[P\])?)[ \t]*(?:—|[–:-](?![ \t]*{ID}\b))[ \t]+(.+?)\s*$",
                     re.M)
# a heading that starts with a card id (T only: "### CPU usage" is no card), parsed by HEAD_RE or not
HEAD_LIKE_RE = re.compile(rf"^#{{3,4}}[ \t]+(T\d+[A-Z0-9]*)\b(?![ \t]*[—–-][ \t]*{ID}\b).*$", re.M)
# a §5 card written inline: its meta line and fields indented under its checklist line, up to the next one
INLINE_RE = re.compile(r"(?:\n(?![ \t]*[-*][ \t]+\[[ xX]\])[ \t]+\S[^\n]*)+")
# a meta field written bold ("**after:** T001 · **S**"): read as plain text, so only other bold fields
# ("**Start with:**") end the meta lines
META_BOLD_RE = re.compile(r"\*\*((?i:after|blocks|fulfills|added by|effort|kind|model|size)\b[^*\n]*|[SML])\*\*")
# "phase 11's T034", "Phase 10's T042A and T043", "phase 11's T021–T023": another feature's cards
# "phase 11's T034, T035": a comma list too, which split_phases ends where the ids turn local
PHASE_RE = re.compile(rf"\b(?i:phase)\s+(\d+)['’]s\s+({ID}(?:\s*(?:[–,-]|\band\b|\bor\b|&)\s*{ID})*)\b")
BATCH = r"B\d+"  # batches of small cards (a stage's, §5's): B1, B2, … (not cards: never in order or progress)
# "- [ ] B3 [P] name", as leniently as CHECK_RE; a "[P]" after the name counts too (group 5)
BATCH_CHECK_RE = re.compile(rf"^[ \t]*[-*][ \t]+\[([ xX])\][ \t]+({BATCH})\b:?((?:[ \t]+\[P\])?)(?:[ \t]+(.+?))?"
                            rf"((?:[ \t]+\[P\])?)\s*$", re.M)
TOUCHES_RE = re.compile(r"\*\*Touches:?\*\*:?(.*?)(?=\s\*\*\w[^*\n]*\*\*|\n[ \t]*\n|\Z)", re.S)
DO_RE = re.compile(r"\*\*Do:?\*\*:?(.*?)(?=\n[ \t]*\*\*\w[^*\n]*:\*\*|\Z)", re.S)
# a card that always runs alone: one whose Do asks for the owner's yes, or a walk-through (title)
# (a heuristic: "reproduction" is not production, and "not paid for" / "unpaid" is not a paid step)
OWNER_STEP_RE = re.compile(r"owner['’]s yes|ask first|(?<!\bnot )\bpaid\b|\bdeploy|\bproduction\b", re.I)
WALK_RE = re.compile(r"walk[- ]?through", re.I)
SIZE_COST = {"S": 1, "M": 2}  # a batch fits one session: its cards' sizes add up to at most BATCH_BUDGET
BATCH_BUDGET = 4
EFFORT_RANK = ["low", "medium", "high", "xhigh", "max"]
FINISHED = {"done", "waived"}
STATUSES = {"todo", "doing", "done", "blocked", "waived"}
OPEN_ANSWERS = {"", "-", "—", "?", "tbd", "todo", "open", "pending"}
PIPE_RE = re.compile(r"(?<!\\)\|")  # a cell separator: a pipe not escaped as \|
IMAGE_RE = re.compile(r"^[\w.-]+\.(?:png|jpe?g|webp|gif)$", re.I)
SETTINGS = {  # state/autopilot.json; the dashboard changes them, autopilot.py acts on them
    "auto": False,               # start ready cards on its own (off: only the owner's Start buttons)
    "max_parallel": 3,           # sessions at the same time, at most one of them a card or batch without [P]
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
    "resume_after": 0,           # a usage limit's pause: switch auto back on after this UTC epoch (0: wait for the owner)
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
        # -ww: without a TTY procps cuts args= at 80 columns, before the session id
        args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(int(run["pid"]))],
                              capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True
    return bool(run.get("session")) and run["session"] in args


def dispatcher_alive(state_dir: str) -> bool:
    """Some process dispatches the feature: the pid autopilot.acquire() wrote into state/.autopilot.lock is
    alive and the lock is held. The lock is probed (shared, non-blocking, dropped at once) only when that
    pid is alive, or can't be read (acquire() is between emptying the file and writing it), so a
    dispatcher taking the lock over from one that died never meets the probe. Left: if the recorded pid
    lives on without the lock (it released it, or the pid was reused), an acquire() at the very moment of
    a probe fails, and that dispatcher skips one pass."""
    path = os.path.join(state_dir, ".autopilot.lock")
    if not os.path.exists(path):
        return False
    said = read_json(path, None)
    pid = said.get("pid") if isinstance(said, dict) else None
    if isinstance(pid, int) and not isinstance(pid, bool):  # os.kill takes a C int: a bigger one raises
        if not 0 < pid < 2 ** 31:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass  # alive, under another account: the lock decides
        except OSError:
            return False
    try:
        handle = open(path)
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


MERGE_LOCK = "merge.lock"  # state/merge.lock: a [P] session merges only while it holds it (autopilot RULES)


def merge_lock(state_dir: str) -> dict | None:
    """The merge lock, when some session holds it: the folder `state/merge.lock` (`mkdir` takes it,
    atomically) and its `holder` file, "<unit> <time>"; None when it is free."""
    path = os.path.join(state_dir, MERGE_LOCK)
    if not os.path.isdir(path):
        return None
    said = read(os.path.join(path, "holder")).strip().split(None, 1)
    try:
        ts = os.path.getmtime(path)
    except OSError:
        ts = None
    return {"holder": said[0] if said else "", "since": said[1].strip() if len(said) > 1 else "", "ts": ts}


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
    def number(path: str) -> tuple:  # "9-x" before "10-x": by the folder's number, unnumbered ones first
        n = re.match(r"\d+", os.path.basename(os.path.dirname(path)))
        return (int(n.group(0)) if n else -1, path)

    found = sorted(glob.glob(os.path.join("specs", "*", "tasks.md")), key=number)
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
                "kind": "", "model": "", "touches": [], "asks_owner": False, "external": [],
            }
            order.append(cid)
        return cards[cid]

    stages = [(m.start(), m.group(1)) for m in SECTION_RE.finditer(text)]

    def stage_at(pos: int) -> str:
        """The stage (or section) a card stands in: the nearest heading above it, "Batches" aside."""
        name = next((n for at, n in reversed(stages) if at < pos and not n.lower().startswith("batches")), "")
        return re.sub(r"\s*\(.*\)$", "", re.sub(r"^\d+\.\s*", "", name))

    inline = {}
    lines = checklist(text)
    for m in lines:
        c = card(m.group(2))
        c["ticked"] |= m.group(1).lower() == "x"
        c["parallel"] |= bool(m.group(3) or m.group(5))
        c["title"] = c["title"] or (m.group(4) or "").strip()  # a bare "- [x] T010B" under a card's heading
        # a §5 card written inline: its meta line and fields indented under the checklist line
        block = INLINE_RE.match(text, m.end())
        if block:
            inline[m.group(2)] = (block.group(0), m.start())

    heads = list(HEAD_RE.finditer(text))
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        stop = re.search(r"^#{1,3} ", body, re.M)
        body = body[: stop.start()] if stop else body
        c = card(m.group(1))
        c["title"] = m.group(3).strip()  # the heading beats a checklist line like "T004B done …"
        c["parallel"] |= bool(m.group(2))
        c["stage"] = stage_at(m.start())
        read_meta(c, body)
        c["headed"] = True
    for cid, (body, at) in inline.items():
        if not cards[cid].get("headed"):
            cards[cid]["stage"] = stage_at(at)
            read_meta(cards[cid], body)

    for c in cards.values():
        c.pop("headed", None)
        # "phase 11's T034" names a card of another feature: never one of this file's
        local = split_phases(c["after_text"], order)[0]
        c["after"] = [x for x in ids_in(local, order) if x != c["id"]]  # waiting on itself: drift, dropped
        c["conditional"] = [x for x in ids_in(" ".join(COND_RE.findall(local)), order)
                            if x not in c["after"] and x != c["id"]]
        c["after"] += c["conditional"]  # the condition cannot be read here; waiting is the safe side
        c["external"] = external_deps(c["after_text"], order)
    # "after: CPF, §5": the card waits for every backlog card too (the close waits for everything)
    backlog_at = re.search(r"^## 5\.", text, re.M)
    if backlog_at:
        backlog = [m.group(2) for m in lines if m.start() > backlog_at.start()]
        backlog += [m.group(1) for m in HEAD_RE.finditer(text) if m.start() > backlog_at.start()]
        for c in cards.values():
            if "§5" in c["after_text"]:
                c["after"] += [x for x in dict.fromkeys(backlog) if x != c["id"] and x not in c["after"]]
    for c in cards.values():  # "blocks: T009" on a backlog card makes T009 wait for it
        for target in ids_in(split_phases(c["blocks_text"], order)[0], order):
            if target in cards and target != c["id"] and c["id"] not in cards[target]["after"]:
                cards[target]["after"].append(c["id"])
    return cards, order


def checklist(text: str) -> list:
    """CHECK_RE's matches that are cards' own lines: an indented one inside a card heading's body naming
    another card ("  - [x] T001 reviewed" under CPA's heading) is a note in that card, not T001's line."""
    heads = list(HEAD_RE.finditer(text))
    bounds = sorted([m.start() for m in heads] + [m.start() for m in re.finditer(r"^#{1,3} ", text, re.M)])
    spans = []  # each card heading's body, as parse_tasks reads it: up to the next card or section heading
    for m in heads:
        spans.append((m.end(), next((b for b in bounds if b > m.start()), len(text)), m.group(1)))
    out = []
    for m in CHECK_RE.finditer(text):
        if m.group(0)[:1] in " \t" and any(a <= m.start() < b and cid != m.group(2) for a, b, cid in spans):
            continue
        out.append(m)
    return out


def tasks_drift(text: str, cards: dict, order: list, status: dict) -> list:
    """What parse_tasks read but could not use: a card heading it cannot parse (its after: and size go
    unread), a card defined twice (read as one card), a card naming itself in after: or blocks:
    (dropped), and open cards that wait on each other in a loop (none of them can start)."""
    out = []
    for m in HEAD_LIKE_RE.finditer(text):
        if not HEAD_RE.match(m.group(0)):
            out.append(f"{m.group(0).strip()!r} is not read as a card heading: write it "
                       f"`#### {m.group(1)} — <title>`, or its after:, size and fields are ignored")
    # a card has one heading and one checklist line at most (one of each is the usual pair)
    for what, ids in (("card headings", [m.group(1) for m in HEAD_RE.finditer(text)]),
                      ("checklist lines", [m.group(2) for m in checklist(text)])):
        counts: dict = {}
        for cid in ids:
            counts[cid] = counts.get(cid, 0) + 1
        out += [f"{cid} is defined {'twice' if n == 2 else f'{n} times'} ({what}); they are read as one card"
                for cid, n in counts.items() if n > 1]
    for cid in order:
        c = cards[cid]
        for field, said in (("after:", c["after_text"]), ("blocks:", c["blocks_text"])):
            local = split_phases(said, order)[0]
            if cid in ids_in(local + " " + " ".join(COND_RE.findall(local)), order):
                out.append(f"{cid} names itself in {field}; ignored")
    # a loop of after: (blocks: and §5 included) among open cards: walk their dependencies depth first.
    # A finished card waits on nothing, so a loop through one holds nobody; every loop of open cards
    # (each strongly connected group of them) has a back edge, so each deadlock is reported at least once.
    open_cards = {cid for cid in order if status.get(cid) not in FINISHED}
    seen: dict = {}  # 1: on the current path, 2: done
    loops = []
    for root in order:
        if root in seen or root not in open_cards:
            continue
        path, stack = [root], [iter(cards[root]["after"])]
        seen[root] = 1
        while stack:
            dep = next(stack[-1], None)
            if dep is None:
                seen[path.pop()] = 2
                stack.pop()
            elif dep not in open_cards:
                continue
            elif seen.get(dep) == 1:
                loop = path[path.index(dep):]
                start = min(loop, key=order.index)  # the same loop reads the same however it was reached
                loop = loop[loop.index(start):] + loop[: loop.index(start)]
                if loop not in loops:
                    loops.append(loop)
            elif dep not in seen:
                seen[dep] = 1
                path.append(dep)
                stack.append(iter(cards[dep]["after"]))
    out += [f"after: cycle {' → '.join(loop + loop[:1])} (each waits for the next): none of them can start"
            for loop in loops]
    return out


def split_phases(text: str, order: list) -> tuple[str, list]:
    """The text with its mentions of other features' cards blanked out, and those mentions as
    (phase, ids). After a comma the list stays with the phase only while its ids are not defined in this
    tasks.md (`order`) and are of the phase's first id's kind (T… or CP…): "phase 11's T034, T035" is
    two of phase 11's cards, "phase 11's T034, T001" (T001 here) and "phase 11's T021–T023, CPD" end
    at the comma, so T001 and CPD are this feature's."""
    refs, parts, last = [], [], 0
    for m in PHASE_RE.finditer(text):
        ids, end = m.group(2), m.end()
        kind = ids[:2] == "CP"
        for comma in re.finditer(r",\s*", ids):
            after = ID_RE.match(ids, comma.end())
            if after and (after.group(0) in order or (after.group(0)[:2] == "CP") != kind):
                ids, end = ids[: comma.start()], m.start(2) + comma.start()
                break
        parts += [text[last: m.start()], " "]
        last = end
        refs.append((m.group(1), ids))
    return "".join(parts + [text[last:]]), refs


def external_deps(text: str, order: list = ()) -> list:
    """The other features' cards an `after:` names: "phase 11's T034" -> {"phase": "11", "card": "T034"};
    a range "phase 11's T021–T023" keeps its end as "through" (build() expands it with that feature's
    cards). Like ids_in, a mention in brackets is no dependency unless it names a condition, "(… if …)":
    then it is one, marked conditional."""
    out: list = []

    def take(part: str, conditional: bool) -> None:
        for phase, ids in split_phases(part, order)[1]:
            for piece in re.split(r"\s*(?:\band\b|\bor\b|&|,)\s*", ids):
                ends = ID_RE.findall(piece)
                if not ends:
                    continue
                dep = {"phase": phase, "card": ends[0], "conditional": conditional}
                if len(ends) > 1:
                    dep["through"] = ends[-1]
                if not any(d["phase"] == dep["phase"] and d["card"] == dep["card"] for d in out):
                    out.append(dep)

    take(unbracket(text), False)
    take(" ".join(COND_RE.findall(text)), True)
    return out


def read_meta(c: dict, body: str) -> None:
    """A card's fields from its body: the meta line(s) before the first bold field, and Start with."""
    # the lines before the first bold field; a bold meta field ("**after:** T001 · **S**") is no such field
    meta = META_BOLD_RE.sub(r"\1", body).split("**", 1)[0]
    # "after:", also "After:"; "effort high" or "Effort: high"; "· S ·" or "Size: S"
    if found := re.search(r"(?i:\bafter):\s*([^·\n]*)", meta):
        c["after_text"] = found.group(1).strip()
    if found := re.search(r"(?i:\bblocks):?\s*([^·\n]*)", meta):
        c["blocks_text"] = found.group(1).strip()
    if found := re.search(r"(?:·|(?i:\bsize):?)\s*([SML])\s*(?:·|$)", meta, re.M):
        c["size"] = found.group(1)
    if found := re.search(r"(?i:\beffort):?\s+(\w+)", meta):
        c["effort"] = found.group(1).lower()
    # "· kind frontend ·", "kind: owner", "model `opus`": a field that starts a segment or a line
    if found := re.search(r"(?:^|·)\s*kind\s*:?\s*`?([A-Za-z]+)", meta, re.M | re.I):
        c["kind"] = found.group(1).lower()
    if found := re.search(r"(?:^|·)\s*model\s*:?\s*`?([\w.\[\]-]*\w)", meta, re.M | re.I):
        c["model"] = found.group(1)
    if found := re.search(r"\*\*Start with:\*\*\s*`([^`]+)`", body):
        c["start_with"] = found.group(1)
    if found := TOUCHES_RE.search(body):
        c["touches"] = touch_paths(found.group(1))
    if found := DO_RE.search(body):
        c["asks_owner"] = bool(OWNER_STEP_RE.search(found.group(1)))


def touch_paths(text: str) -> list:
    """The files and folders a card's Touches names: backticked paths (`a/{b,c}.ts` is two), else
    bare words with a slash. Placeholders (`<files>`) and prose name none."""
    quoted = re.findall(r"`([^`]+)`", text)
    out = []
    for token in quoted or re.split(r"[\s,;]+", text):
        token = re.sub(r"\s*\([^()]*\)", "", token).strip().strip(".,;:()").split("::", 1)[0]  # "x.ts (+ .de.ts)"
        if not token or "<" in token or " " in token:
            continue
        for path in expand_braces(token):
            path = re.sub(r"^\./", "", path).rstrip("/")
            if "/" in path or (quoted and re.search(r"\w\.[A-Za-z]\w{0,5}$", path)):
                out.append(path)
    return list(dict.fromkeys(out))


def touch_root(path: str) -> str:
    """A Touches path as a plain path prefix: a glob is cut back to the folder before its first wildcard
    (`src/**/*.ts` -> `src`; `**/*.ts` -> "", the whole repo)."""
    found = re.search(r"[*?\[]", path)
    if not found:
        return path.rstrip("/")
    return path[: found.start()].rsplit("/", 1)[0].rstrip("/") if "/" in path[: found.start()] else ""


def touches_clash(mine: list | None, theirs: list | None) -> str | None:
    """What two units' Touches share: "" when nothing, else the path (`api/x.ts`) or the folder that holds
    the other's path (`packages/x/…`). None when either unit has a card whose Touches name nothing: such
    a unit is taken to touch everything, so it never runs beside another."""
    if mine is None or theirs is None:
        return None
    for a in mine:
        for b in theirs:
            ra, rb = touch_root(a), touch_root(b)
            if not ra or not rb:
                return f"{a if not ra else b} (everything)"
            if ra == rb:
                return ra if a.rstrip("/") == b.rstrip("/") else f"{ra}/…"
            short, long = sorted((ra, rb), key=len)
            if long.startswith(short + "/"):
                return f"{short}/…"
    return ""


def expand_braces(token: str) -> list:
    """`app/{a.ts,b/c.ts}` -> app/a.ts, app/b/c.ts (one level of braces at a time)."""
    found = re.search(r"\{([^{}]*)\}", token)
    if not found:
        return [token]
    out = []
    for part in found.group(1).split(","):
        out += expand_braces(token[: found.start()] + part.strip() + token[found.end():])
    return out


def parse_batches(text: str, cards: dict, order: list) -> tuple[list, list]:
    """The batches, a stage's (under its heading in §4) and §5's: every table whose header has a
    "batch…" and a "cards…" column, one row per batch (id, name, its cards in order, effort, the
    backticked Start with), ticked by its "- [x] B1 …" line (§4's checklist or §5's), which may say
    `[P]`. Returns the batches in order and the drift they show (unknown cards, a card in two batches,
    no Start with)."""
    found: dict = {}
    for _, lines in table_blocks(text):
        rows = [{head_cell(k): v for k, v in row.items()} for row in table(lines)]
        head = [head_cell(c) for c in cells(lines[0])]
        if not (any(h.startswith("batch") for h in head) and any(h.startswith("cards") for h in head)):
            continue
        for row in rows:
            bid = re.search(rf"\b{BATCH}\b", column(row, "batch"))
            if not bid or bid.group(0) in found:
                continue
            start = re.search(r"`([^`]+)`", column(row, "start"))
            effort = re.sub(r"[`*_]", "", column(row, "effort")).strip().lower().split()
            found[bid.group(0)] = {
                "id": bid.group(0), "name": re.sub(r"[`*]", "", column(row, "name")).strip(),
                "named": ids_in(column(row, "cards"), order), "effort": effort[0] if effort else "",
                "start_with": start.group(1).strip() if start else "", "ticked": False, "row": True,
                "parallel": False,
            }
    for m in BATCH_CHECK_RE.finditer(text):
        b = found.setdefault(m.group(2), {"id": m.group(2), "name": "", "named": [], "effort": "",
                                          "start_with": "", "ticked": False, "row": False, "parallel": False})
        b["ticked"] |= m.group(1).lower() == "x"
        b["parallel"] |= bool(m.group(3) or m.group(5))  # "B3 [P] name", or "B3 name [P]"
        b["name"] = b["name"] or (m.group(4) or "").strip()
    drift, owner = [], {}
    batches = []
    for b in found.values():
        b["cards"] = []
        for c in b.pop("named"):
            if c not in cards:
                drift.append(f"batch {b['id']} names {c}, which tasks.md does not define")
            elif c in owner:
                drift.append(f"{c} is in two batches ({owner[c]}, {b['id']}); it runs with {owner[c]}")
            else:
                owner[c] = b["id"]
                b["cards"].append(c)
        if not b.pop("row"):
            drift.append(f"batch {b['id']} is in the checklist but has no row in §5's batch table")
        elif not b["cards"]:
            drift.append(f"batch {b['id']} names no card")
        elif not b["start_with"]:
            drift.append(f"batch {b['id']} has no Start with line (a backticked line in its table row)")
        batches.append(b)
    return batches, drift


def alone(cid: str, c: dict, decisions: list = ()) -> str:
    """Why a card may never be in a batch, rule 11's always-alone list; "" when it may: T001, a checkpoint,
    an owner's card, the Results card (the close: it waits for §5, rule 16, or is titled just "Results"),
    one whose Do asks for the owner's yes, a walk-through, a card waiting for one of the open decisions
    (RESUME's "needed before", or "decision 2" in its after:)."""
    if cid == "T001":
        return "the first card"
    if cid.startswith("CP"):
        return "a checkpoint"
    if c["kind"] == "owner":
        return "an owner's card"
    if "§5" in c["after_text"] or re.fullmatch(r"(?:results|close)\W*", c["title"].strip(), re.I):
        return "the Results card"
    if c["asks_owner"]:
        return "a card whose Do asks for the owner's yes"
    if WALK_RE.search(c["title"]):
        return "a walk-through"
    named = set(re.findall(r"\bdecisions?\s+(\d+)", c["after_text"], re.I))
    if any(cid in d["before"] or str(d["n"]).strip() in named for d in decisions):
        return "a card waiting for an owner's decision"
    return ""


def batch_rules(batches: list, cards: dict, status: dict, decisions: list = ()) -> list:
    """Drift for unfinished batches that break the batching rules: a card that always runs alone (alone(),
    with the open decisions), an L card or more than BATCH_BUDGET by size (S = 1, M = 2), and a card
    outside the batch that waits on one of its cards while another of its cards waits on it (the batch
    could never run)."""
    waiters: dict = {}
    for cid, c in cards.items():
        for dep in c["after"]:
            waiters.setdefault(dep, []).append(cid)
    out = []
    for b in batches:
        if b["done"] or not b["cards"]:
            continue
        mine = b["cards"]
        for cid in mine:
            if why := alone(cid, cards[cid], decisions):
                out.append(f"batch {b['id']} holds {cid}, {why}: it runs on its own, never in a batch")
        large = [c for c in mine if cards[c]["size"] == "L"]
        out += [f"batch {b['id']} holds {c}, an L card: split it before it goes in a batch" for c in large]
        sized = [c for c in mine if cards[c]["size"] in SIZE_COST]
        cost = sum(SIZE_COST[cards[c]["size"]] for c in sized)
        if not large and cost > BATCH_BUDGET:
            out.append(f"batch {b['id']} is too big for one session: "
                       + " + ".join(f"{c} {cards[c]['size']}" for c in sized)
                       + f" = {cost} (at most {BATCH_BUDGET}, counting S as 1 and M as 2)")
        # walk from the batch's open cards to the open cards outside it that wait on them
        reached: dict = {}
        queue = [(c, c, []) for c in b["open"]]
        while queue:
            at, origin, path = queue.pop(0)
            for w in waiters.get(at, []):
                if w in mine or w in reached or status.get(w) in FINISHED:
                    continue
                reached[w] = (origin, path + [w])
                queue.append((w, origin, path + [w]))
        for cid in b["open"]:
            for dep in cards[cid]["after"]:
                if dep in reached:
                    origin, path = reached[dep]
                    through = f" (through {', '.join(path[:-1])})" if len(path) > 1 else ""
                    out.append(f"batch {b['id']} can never run: {dep}, outside it, waits for {origin}{through}"
                               f" while {cid} waits for {dep}; put {dep} in the batch or take {cid} out")
    return out


def unbatched(cards: dict, order: list, status: dict, doing: list, batch_of: dict, decisions: list,
              backlog: set) -> list:
    """A quiet heads-up: open small (S) cards in no batch that could share one session, grouped by the
    files their Touches share (or a chain: one waits on another), by stage when Touches can't be read.
    Given only when such groups hold 3 cards or more. Cards that always run alone are left out: T001,
    checkpoints, owner's cards, the Results card, a card waiting for an owner's decision, one whose Do
    asks for the owner's yes, a walk-through (alone())."""

    def never(cid: str) -> bool:
        return bool(alone(cid, cards[cid], decisions))

    picked = [c for c in order if cards[c]["size"] == "S" and status[c] not in FINISHED and c not in doing
              and c not in batch_of and not never(c)]
    if len(picked) < 3:
        return []
    key = {c: (c in backlog, cards[c]["stage"]) for c in picked}  # batches never cross a stage

    def folders(c: str) -> set:
        return {p.rsplit("/", 1)[0] if re.search(r"\.\w+$", p.rsplit("/", 1)[-1]) else p
                for p in cards[c]["touches"] if "/" in p or not re.search(r"\.\w+$", p)}

    def shared(x: str, y: str) -> str:
        """What two cards share: a file, a folder (`ui/tasks/…`), a chain; "" when nothing."""
        if y in cards[x]["after"] or x in cards[y]["after"]:
            return "a chain (one waits on the other)"
        if files := sorted(set(cards[x]["touches"]) & set(cards[y]["touches"])):
            return f"`{files[0]}`"
        for a in sorted(folders(x)):
            for b in sorted(folders(y)):
                short = min(a, b, key=len)
                if a == b or ("/" in short and (a.startswith(b + "/") or b.startswith(a + "/"))):
                    return f"`{short}/…`"
        return ""

    parent = {c: c for c in picked}

    def root(c: str) -> str:
        while parent[c] != c:
            c = parent[c]
        return c

    labels: dict = {}
    for i, x in enumerate(picked):
        for y in picked[i + 1:]:
            if key[x] != key[y]:
                continue
            why = shared(x, y)
            if not why and not cards[x]["touches"] and not cards[y]["touches"]:
                why = f"a stage, {cards[x]['stage'] or '§5'}; their Touches name no files"
            if why:
                rx, ry = root(x), root(y)
                if rx != ry:
                    parent[ry] = rx
                labels.setdefault((x, y), why)
    groups: dict = {}
    for c in picked:
        groups.setdefault(root(c), []).append(c)
    found = [g for g in groups.values() if len(g) > 1]
    if sum(len(g) for g in found) < 3:
        return []
    out = []
    for g in found:
        said = [why for (x, y), why in labels.items() if x in g and y in g]
        share = max(dict.fromkeys(said), key=said.count)
        if all(cards[c]["touches"] for c in g):  # what all of them share, when something is
            files = set.intersection(*(set(cards[c]["touches"]) for c in g))
            try:
                common = os.path.commonpath(sorted(set().union(*(folders(c) for c in g))))
            except ValueError:
                common = ""
            share = f"`{sorted(files)[0]}`" if files else f"`{common}/…`" if common else share
        where = "§5" if key[g[0]][0] else f"the batch table under {cards[g[0]]['stage'] or 'their stage'}"
        out.append({"cards": g, "share": share, "where": where,
                    "text": f"{len(g)} small cards are in no batch: {', '.join(g)} (they share {share});"
                            f" batch them ({where})"})
    return out


def runs_as(text: str) -> dict:
    """tasks.md §6: "**Runs as:** owner@example.com via `~/.local/bin/claude-work`" (the account the
    feature's sessions must use, and the CLI launcher logged in as it) and "**App URL:** http://…" (the
    page a Chrome check opens). The launcher is the first backticked word after a "via" on that line, or
    the bare word right after "<email> via"; a note around it ("(work account)") is not part of it."""
    out = {"email": "", "launcher": "", "app_url": ""}
    if found := re.search(r"\*\*Runs as\s*:?\*\*:?[ \t]*`?([^\s`]+@[^\s`]+?)`?[.,;]?(?=[ \t]|$)([^\n]*)", text, re.M | re.I):
        out["email"], rest = found.group(1).strip(".,;"), found.group(2)
        launcher = re.search(r"\bvia[ \t]+`([^\s`]+)`", rest) or re.match(r"[ \t]+via[ \t]+([^\s`]+)", rest)
        out["launcher"] = launcher.group(1).strip(".,;") if launcher else ""
    if found := re.search(r"\*\*App URL\s*:?\*\*:?\s*`?(https?://[^\s`]+)`?", text, re.I):
        out["app_url"] = found.group(1).rstrip(".,;")
    return out


def unattended_deny(text: str) -> list:
    """Tool patterns a card session started by the autopilot may never run (tasks.md §6,
    "**Never unattended:** `Bash(git push:*)`, …"); the owner runs those steps after a yes."""
    found = re.search(r"\*\*Never unattended\s*:?\*\*:?([^\n]*(?:\n(?!\s*\n)[^\n]*)*)", text, re.I)
    return re.findall(r"`([^`]+)`", found.group(1)) if found else []


def unbracket(text: str) -> str:
    """The text without its brackets, nested ones too: "(beside (x) T004)" leaves no "T004)" behind."""
    while True:
        out = re.sub(r"\([^()]*\)", " ", text)
        if out == text:
            return out
        text = out


def ids_in(text: str, order: list) -> list:
    text = unbracket(text)  # "(beside T002)" is not a dependency
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
            out.setdefault(current, [])  # a heading written twice: the second adds to the first
        elif current is not None:
            out[current].append(line)
    return out


def section(secs: dict, name: str) -> list:
    return next((lines for key, lines in secs.items() if key.startswith(name)), [])


def cells(line: str) -> list:
    return [cell.strip() for cell in PIPE_RE.split(line.strip().strip("|"))]


def tables(lines: list) -> list:
    """Each Markdown table in lines, as (its lowercased header, its rows as dicts by that header). A
    table ends at the first line that is not a table line; a run of `|` lines without a separator under
    its first line continues the table before it (a blank line slipped in), one with it starts a new one."""
    runs, run = [], []
    for line in lines + [""]:
        if line.strip().startswith("|"):
            run.append(line.strip())
        elif run:
            runs.append(run)
            run = []
    out: list = []
    for run in runs:
        if not out or (len(run) > 1 and separator(run[1])):
            out.append(([h.lower() for h in cells(run[0])], []))
            run = run[1:]
        head, rows = out[-1]
        for row in run:
            values = cells(row)
            if all(set(v) <= set("-: ") for v in values):
                continue
            rows.append(dict(zip(head, values)))
    return out


def table(lines: list) -> list:
    """The rows of the first table in lines."""
    found = tables(lines)
    return found[0][1] if found else []


def rows_of(lines: list, *names: str) -> list:
    """The rows of every table in lines whose header has each of the named columns (a section written
    twice, or a second table of the same kind under it, still counts; a table of other columns not)."""
    return [row for head, rows in tables(lines) if all(any(h.startswith(n) for h in head) for n in names)
            for row in rows]


def column(row: dict, name: str) -> str:
    return next((v for k, v in row.items() if k.startswith(name)), "")


VERDICTS = {  # a Checks row's verdict, judged by its first word
    "pass": {"pass", "passed", "ok", "green", "yes"},
    "fail": {"fail", "failed", "failing", "red", "no"},
    "unresolved": {"unresolved", "open", "pending", "unknown", "untested", "blocked"},
    "waived": {"waived", "waiver"},
}
NO_EVIDENCE = OPEN_ANSWERS | {"n/a", "na", "none", "tbd", "–", "—", "-"}
# a session that stopped to wait for the owner; is_try() leaves it out, here and in autopilot.py
WAITED = re.compile(r"AUTOPILOT: (?:WAITING FOR (?:DECISION|APPROVAL)|BLOCKED)")
# a session that finished its card with a remainder card (§1's Context budget item): not a failed try
SPLIT = re.compile(r"AUTOPILOT: SPLIT")
REVIEWED_RE = re.compile(r"Pins reviewed up to[\s`*:]*([0-9a-f]{7,40})\b", re.I)
GIT: dict = {}  # cached git answers: the repo root, the pin history (per ref state), ancestry
GIT_LIMIT = 4096  # a long --serve asks about ever more commits: past this many answers, the older half of
# the ancestry answers is forgotten (the repo roots and pin histories stay)
EOO: list = []  # [True] once git is known to take --end-of-options (2.24+), [False] when it does not


def flat(value, limit: int = 80, cell: bool = True) -> str:
    """On one line and at most limit characters; as a table cell (cell), with its pipes escaped."""
    text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    text = PIPE_RE.sub(r"\\|", text) if cell else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def head_cell(cell: str) -> str:
    """A header cell as plain lowercase words: "**Criterion** (Done when)" -> "criterion (done when)"."""
    return re.sub(r"[*`_]", "", cell).strip().lower()


def head_index(head: list, name: str, default: int) -> int:
    """The first header column that starts with name, else default."""
    return next((i for i, h in enumerate(head) if h.startswith(name)), default)


def verdict(cell: str) -> str:
    """pass, fail, unresolved or waived, from a Checks row's verdict cell; "" when it is none of them."""
    if "❌" in cell:
        return "fail"
    words = [w for w in re.split(r"[\s/]+", re.sub(r"[`*_.✅]", " ", cell).lower()) if w.strip(":;,()")]
    if not words:
        return "pass" if "✅" in cell else ""
    word = words[0].strip(":;,()")
    return next((name for name, group in VERDICTS.items() if word in group), "")


def has_evidence(cell: str) -> bool:
    plain = re.sub(r"[`*_]", "", cell).strip().lower().strip(". ")
    return not (plain in NO_EVIDENCE or re.fullmatch(r"<.*>", plain, re.S))


def table_blocks(text: str) -> list:
    """Each Markdown table in text as (the heading above it, its lines), in order."""
    lines, out, heading, i = text.splitlines(), [], "", 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            out.append((heading, lines[i:j]))
            i = j
            continue
        if re.match(r"#+\s", line) or re.fullmatch(r"\*\*[^*]+\*\*:?", line):
            heading = re.sub(r"^#+\s*|\*", "", line).strip(" :").lower()
        i += 1
    return out


def separator(line: str) -> bool:
    return line.strip().startswith("|") and "-" in line and set(line.strip()) <= set("|-: ")


def pinned_tests(text: str) -> dict:
    """§3's "Rules pinned by a test" tables: each pinning test (path, glob or file::test) -> the card
    that writes it. A table counts when its header has a "pinning …" and a "card" column."""
    out = {}
    for _, lines in table_blocks(text):
        head = [head_cell(c) for c in cells(lines[0])]
        pin, card = head_index(head, "pinning", -1), head_index(head, "card", -1)
        if pin < 0 or card < 0 or len(lines) < 2 or not separator(lines[1]):
            continue
        for line in lines[2:]:
            row = cells(line)
            if separator(line) or len(row) <= max(pin, card):
                continue
            found = ID_RE.search(row[card])
            for path in re.findall(r"`([^`]+)`", row[pin]) or [p for p in row[pin].split(",") if p.strip()]:
                out[path.strip()] = found.group(0) if found else ""
    return out


def handoff_checks(text: str) -> list | None:
    """The hand-off's Checks table, one dict per row (criterion, verdict as pass/fail/unresolved/waived
    or "", raw, evidence, correction); None without one. The table under a "Checks" heading wins, else
    the first whose header has a criterion and a verdict column."""
    blocks = table_blocks(text)
    heads = [[head_cell(c) for c in cells(lines[0])] for _, lines in blocks]
    pick = next((i for i, (heading, _) in enumerate(blocks) if heading == "checks"), None)
    if pick is None:
        pick = next((i for i, h in enumerate(heads) if head_index(h, "criterion", -1) >= 0
                     and head_index(h, "verdict", -1) >= 0), None)
    if pick is None:
        return None
    head = heads[pick]
    ic = head_index(head, "criterion", 0)
    iv = head_index(head, "verdict", ic + 1)
    ie = head_index(head, "evidence", iv + 1)
    ix = head_index(head, "correction", ie + 1)
    out = []
    for line in blocks[pick][1][1:]:
        row = cells(line)
        if separator(line):
            continue
        cell = lambda i: row[i] if i < len(row) else ""  # noqa: E731
        shift, said = 0, verdict(cell(iv))
        if not said:  # a "|" left unescaped in the criterion pushes the verdict right
            for j in range(iv + 1, len(row)):
                if verdict(row[j]):
                    shift, said = j - iv, verdict(row[j])
                    break
        criterion = " | ".join(row[ic: iv + shift]) if shift and ic < iv else cell(ic)
        if criterion:
            out.append({"criterion": criterion, "verdict": said, "raw": cell(iv + shift),
                        "evidence": cell(ie + shift), "correction": cell(ix + shift)})
    return out


def git(root: str, *args: str, timeout: float = 5) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def end_of_options() -> list:
    """["--end-of-options"] when the installed git knows it (2.24+): nothing after it is read as an option."""
    if not EOO:
        out = git("/", "--version", timeout=3)
        found = re.search(r"(\d+)\.(\d+)", out.stdout) if out else None
        EOO.append(bool(found) and (int(found.group(1)), int(found.group(2))) >= (2, 24))
    return ["--end-of-options"] if EOO[0] else []


# (each cached answer is read into a local first: another thread may clear GIT at any moment)
def git_root(folder: str) -> str:
    found = GIT.get(("root", folder))
    if not found:  # a folder that is not in a repo yet is asked again
        out = git(folder, "rev-parse", "--show-toplevel", timeout=3)
        found = GIT[("root", folder)] = out.stdout.strip() if out and out.returncode == 0 else ""
    return found


def pin_history(root: str, specs: list, tasks: str) -> tuple:
    """Every non-merge commit on any branch that changes a path under specs, as (sha, time, subject,
    paths), and when tasks (relative to root) was first committed (0: never). Read again only when a
    ref moves, so the dashboard's refresh stays cheap."""
    refs = git(root, "rev-parse", "--all", "HEAD", timeout=3)
    key = ("pins", root, tuple(specs), tasks)
    stamp = refs.stdout if refs else ""
    cached = GIT.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    out = git(root, "log", "--all", "--full-history", "--no-merges", "-z",
              "--format=%x1e%H%x1f%ct%x1f%s", "--name-only", "--", *specs, timeout=10)
    commits: list = []
    for token in (out.stdout.split("\0") if out and out.returncode == 0 else []):
        if token.startswith("\x1e"):  # -z: "<marker>sha␟time␟subject", then "\nfirst path", "path", …
            sha, ct, subject = (token[1:].split("\x1f", 2) + ["", ""])[:3]
            commits.append((sha, int(ct) if ct.isdigit() else 0, subject, []))
        elif token.strip("\n") and commits:
            commits[-1][3].append(token.lstrip("\n"))
    began = git(root, "log", "--all", "--format=%ct", "--diff-filter=A", "--", tasks, timeout=5)
    began = min((int(x) for x in began.stdout.split() if x.isdigit()), default=0) if began else 0
    GIT[key] = (stamp, (commits, began))
    return commits, began


def is_ancestor(root: str, sha: str, reviewed: str) -> bool:
    key = ("ancestor", root, sha, reviewed)
    found = GIT.get(key)
    if found is None:
        if len(GIT) >= GIT_LIMIT:
            older = [k for k in list(GIT) if k[0] == "ancestor"]  # (oldest first: insertion order)
            for k in older[: len(older) // 2 + 1]:
                GIT.pop(k, None)
        out = git(root, "merge-base", "--is-ancestor", sha, reviewed, timeout=3)
        found = GIT[key] = bool(out and out.returncode == 0)
    return found


def pin_drift(tasks: str, cards: dict, rows: dict, pins: dict, reviewed: str) -> list:
    """A commit by any card but a pin's writer that changes the pin, since the feature began and not
    yet reviewed by a checkpoint (RESUME's "Pins reviewed up to <commit>")."""
    folder = os.path.realpath(os.path.dirname(tasks))  # git answers with real paths (/private/tmp)
    root = git_root(folder)
    if not root or not pins:
        return []
    project = os.path.relpath(os.path.dirname(os.path.dirname(folder)), root)  # the folder holding specs/
    wanted = {}
    for pin, writer in pins.items():
        path = re.sub(r"^\./", "", pin.split("::", 1)[0].strip())
        if not path:
            continue
        for form in dict.fromkeys([path] + ([os.path.normpath(os.path.join(project, path))] if project != "." else [])):
            wanted[form] = (pin, writer)
    recorded = []  # RESUME's commit column: (hash prefix, card)
    for cid, row in rows.items():
        recorded += [(h, cid) for h in re.findall(r"\b[0-9a-f]{7,40}\b", (row.get("commit") or "").lower())]
    commits, began = pin_history(root, sorted(wanted), os.path.relpath(os.path.join(folder, "tasks.md"), root))
    out, seen = [], set()
    for sha, ct, subject, paths in commits:
        if ct < began:
            continue  # older than this feature: another feature's cards may share these ids
        cid = next((x for x in ID_RE.findall(subject) if x in cards), "") \
            or next((c for h, c in recorded if sha.startswith(h)), "")
        if not cid:
            continue  # no card to name: don't guess
        for path in paths:
            for form, (pin, writer) in wanted.items():
                inside = fnmatch.fnmatchcase(path, form) or path.startswith(form.rstrip("/") + "/")  # a folder
                if cid == writer or not inside or (cid, sha, path) in seen:
                    continue
                seen.add((cid, sha, path))
                if reviewed and is_ancestor(root, sha, reviewed):
                    continue
                out.append(f"{cid}'s commit {sha[:7]} changes the pinning test {path}"
                           f" (written by {writer or 'another card'}): check no test was weakened, then"
                           " record `Pins reviewed up to <commit>` in RESUME")
    return out


# a status cell's words for todo/doing/done/…: what sessions write besides the bare word (an ambiguous
# one, "pending review" or "open PR", is left out: drift asks for the word, and the card's tick stands in)
STATUS_SYNONYMS = {
    "to do": "todo", "to-do": "todo", "not started": "todo",
    "in progress": "doing", "in-progress": "doing", "inprogress": "doing", "wip": "doing", "started": "doing",
    "ongoing": "doing", "in review": "doing", "review": "doing", "reviewing": "doing", "testing": "doing",
    "on hold": "blocked", "stuck": "blocked",
    "complete": "done", "completed": "done", "finished": "done", "merged": "done",
}


def status_word(cell: str) -> str:
    """A status cell as one of STATUSES when it says one ("**Doing**", "in progress", "Done (merged)"),
    else the cell as written, lowercased ("" when empty or a dash): build() reports that as drift."""
    words = re.sub(r"[`*_~]", " ", cell).lower().split()
    if not words:
        return ""
    two = " ".join(words[:2]).strip(".,;:!()")
    word = words[0].strip(".,;:!()").strip("-–—")
    said = STATUS_SYNONYMS.get(two) or STATUS_SYNONYMS.get(word, word)
    return said if said in STATUSES or not said else " ".join(words)


def resume_ids(text: str, order: "list | None") -> list:
    """The cards a RESUME cell or line names, a range ("T005–T008") as every card in it by the order;
    with the order, only the cards tasks.md defines ("CPU" or "CPP" is a word, not a card)."""
    found = list(dict.fromkeys(ids_in(text, order or []) + ID_RE.findall(text)))  # ids_in skips "(…)"
    return [x for x in found if x in order] if order else found


def lock_free(lock: str) -> bool:
    """The deploy lock is free when it is unset or its first word is "free" ("free (released by T009)")."""
    words = re.sub(r"[`*_~]", " ", lock).lower().split()
    return not words or words[0].strip(".,;:!()") == "free"


def parse_resume(text: str, order: "list | None" = None) -> dict:
    """RESUME.md's tables and lists. With tasks.md's card order, ranges expand to its cards and only
    the cards it defines count as a decision's or blocker's subject."""
    secs = sections(text)
    status = {}
    for row in rows_of(section(secs, "status"), "card", "status"):
        found = ID_RE.search(column(row, "card"))  # one tasks.md lacks ("CPU") is drift, never a card's status
        if not found or found.group(0) in status:
            continue  # a card's first row counts, the one autopilot.set_row edits
        status[found.group(0)] = {
            "status": status_word(column(row, "status")),
            "branch": column(row, "branch").strip("`"),
            "commit": column(row, "commit").strip("`"),
            "date": column(row, "date"),
        }
    decisions = []
    for row in rows_of(section(secs, "decisions"), "question"):
        n, question = column(row, "#"), column(row, "question")
        if n.startswith("<") or question.startswith("<") or not (n or question):
            continue  # the template's placeholder row, or an empty one
        answer = column(row, "answer")
        plain = answer.lower().strip(" .")
        decisions.append({
            "n": n,
            "question": question,
            "before": resume_ids(column(row, "needed"), order),
            "recommended": column(row, "recommend"),
            "answer": answer,
            "open": plain in OPEN_ANSWERS or plain.startswith("deferred"),
        })
    approvals = []
    for row in rows_of(section(secs, "approvals"), "step"):
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
        blockers.append({"text": item, "cards": blocker_subjects(item, order)})
    lock, inside = "", False
    for line in text.splitlines():  # the first line under the last "## Deploy lock" (sections() joins them)
        if heading := re.match(r"^##\s+(.+?)\s*$", line):
            inside = heading.group(1).lower().startswith("deploy lock")
            lock = "" if inside else lock
        elif inside and not lock and line.strip():
            lock = line.strip()
    reviewed = REVIEWED_RE.findall(text)  # a checkpoint's "Pins reviewed up to <commit>"; the last one counts
    return {"status": status, "decisions": decisions, "approvals": approvals, "blockers": blockers, "lock": lock,
            "lock_free": lock_free(lock), "pins_reviewed": reviewed[-1] if reviewed else ""}


def blocker_subjects(item: str, order: "list | None" = None) -> list:
    """The cards a blocker holds up: the card (or range) it opens with ("T037 (date): …", "T016C: …",
    "T005–T008: …"), else the cards after "before" ("<precondition> before T005"), else the card it
    opens with, else the one card it names outside parentheses ("waiting on the API key for T005"), else none.
    With the order, only cards tasks.md defines count ("CPU quota exceeded" names no card)."""
    lead = re.match(rf"\s*({ID}(?:\s*[–-]\s*{ID})?)\b(\s*[(:])?", item)
    if lead and lead.group(2) and (named := resume_ids(lead.group(1), order)):
        return named
    before = []
    for m in BEFORE_RE.finditer(item):
        span = RANGE_RE.match(item, m.start(1))  # "before T005–T008": all of them
        before += resume_ids(span.group(0) if span else m.group(1), order)
    if before:
        return list(dict.fromkeys(before))
    if lead and (named := resume_ids(lead.group(1), order)):
        return named
    # "(T003 shipped the stub)" is an aside, not the card held up: a blocker on a finished card is history
    anywhere = resume_ids(re.sub(r"\([^)]*\)", " ", item), order)
    return anywhere if len(anywhere) == 1 else []


# --- the picture ----------------------------------------------------------------------


def last_commit(repo: str, branch: str) -> float | None:
    """When a card's branch last moved here, from its reflog: a commit, a merge into it, or its creation (a
    branch just cut from an old tip is not "quiet since" that tip). Without a reflog, when its tip was
    committed if no other local branch holds that tip, else None (it is not this card's work). None too
    when the cell names nothing git knows. Sessions write RESUME, so a cell starting with "-" never
    reaches git: git would read it as an option (`--output=<file>` writes that file)."""
    branch = (branch or "").strip()
    if not branch or branch in ("-", "—") or branch.startswith("-"):
        return None
    rev = [*end_of_options(), branch, "--"]
    out = git(repo, "log", "-g", "-1", "--format=%gd", "--date=unix", *rev, timeout=3)
    if out and out.returncode == 0 and (moved := re.search(r"@\{(\d+)\}$", out.stdout.strip())):
        return float(moved.group(1))
    name = re.sub(r"^refs/heads/", "", branch)
    out = git(repo, "log", "-1", "--format=%ct", "--not", f"--exclude={name}", "--branches", "--not", *rev,
              timeout=3)
    text = out.stdout.strip() if out and out.returncode == 0 else ""
    return float(text) if text.isdigit() else None


def number(value) -> float:
    """A number from runs.json; 0 for anything else ("n/a", null, NaN, infinity: never in the JSON)."""
    try:
        out = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return out if math.isfinite(out) else 0.0


# runs.json: its tables and lists, and the fields of a run that are text or numbers
REGISTRY_FIELDS = (("runs", list), ("attention", dict), ("handled", list), ("manual", list), ("notified", list),
                   ("queued", list), ("granted", dict), ("blocked_on", dict))
RUN_TEXT = ("card", "session", "batch", "reason", "approval", "started", "ended", "result", "error", "log")
RUN_NUMBERS = ("cost", "started_ts", "kill_sent_ts", "killed_ts", "leftovers_ts")


def registry_of(found) -> dict:
    """runs.json as everything that reads it expects it (build(), lessons(), autopilot.registry()): each
    table and list of its type, the ids in them as text, every run an object whose text fields are text
    ("" when missing) and whose costs and times are finite numbers (0 for "n/a", null, NaN or infinity),
    a run's port slot a whole number. A hand-edited or half-broken file must take down neither the
    report, the dashboard nor the dispatcher. Changes found in place and returns it."""
    reg = found if isinstance(found, dict) else {}
    for key, kind in REGISTRY_FIELDS:
        if not isinstance(reg.get(key), kind):
            reg[key] = kind()
    for key in ("handled", "manual", "notified", "queued"):
        reg[key] = [x for x in reg[key] if isinstance(x, str)]
    reg["attention"] = {str(k): "" if v is None else str(v) for k, v in reg["attention"].items()}
    reg["granted"] = {str(k): max(0, int(number(v))) for k, v in reg["granted"].items()}
    reg["blocked_on"] = {str(k): [str(x) for x in v] if isinstance(v, list) else []
                         for k, v in reg["blocked_on"].items()}
    runs = []
    for run in reg["runs"]:
        if not isinstance(run, dict):
            continue
        for key in RUN_TEXT:
            value = run.get(key)
            run[key] = value if isinstance(value, str) else "" if value in (None, False) else str(value)
        for key in RUN_NUMBERS:
            if key in run:
                run[key] = number(run[key])
        if "slot" in run:
            run["slot"] = max(0, int(number(run["slot"])))
        runs.append(run)
    reg["runs"] = runs
    for key in ("account", "chrome_check"):  # read with .get(): an object, or nothing
        if key in reg and not isinstance(reg[key], dict):
            reg[key] = None
    return reg


def mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:  # removed since it was listed
        return None


def row_time(cell: str, day: bool = False) -> float | None:
    """The newest time in a RESUME status row's date cell, as a UTC timestamp: "2026-10-09 14:05Z" (or
    "T14:05") at that minute. With day, the newest bare day instead, at its end but never later than
    now: it says only that the row changed that day."""
    out = []
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?", cell or ""):
        if (m.group(4) is None) != day:
            continue
        try:  # (not named `day`: that is the flag every later match is checked against)
            date = dt.datetime(*(int(x) for x in m.groups()[:3]), tzinfo=dt.timezone.utc)
            at = (min(date + dt.timedelta(days=1), now()) if m.group(4) is None
                  else date.replace(hour=int(m.group(4)), minute=int(m.group(5))))
        except ValueError:
            continue
        out.append(at.timestamp())
    return max(out, default=None)


def run_times(state_dir: str, run: dict) -> list:
    """When a session the autopilot started began, and when its log last grew (its last output)."""
    began = number(run.get("started_ts")) or row_time(str(run.get("started") or ""))
    log = run.get("log")
    return [began, mtime(os.path.join(state_dir, log)) if isinstance(log, str) and log else None]


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
        if cid not in cards:
            continue  # a row for a card tasks.md does not define: drift, not a commit's owner
        # whole hashes only (SHA-1 or SHA-256): "merged a1b2c3d" is a1b2c3d, not "merged"'s hex letters too
        for commit in re.findall(r"\b[0-9a-f]{7,64}\b", (row.get("commit") or "").lower()):
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


def card_status(cards: dict, order: list, resume: dict) -> dict:
    """Each card's status: its RESUME row when that says one, else its tick in tasks.md."""
    status = {}
    for cid in order:
        row = resume["status"].get(cid)
        if row and row["status"] in STATUSES:
            status[cid] = row["status"]
        else:
            status[cid] = "done" if cards[cid]["ticked"] else "todo"
    return status


def phase_cards(folder: str, phase: str, cache: dict) -> dict:
    """Another feature's cards and their status, for "phase 11's T034": the sibling folder of this
    feature's folder whose name starts with the phase number (`11-…`, `011-…`), read like build() reads
    its own (tasks.md ticks, state/RESUME.md rows). Empty when no such feature or tasks.md is found: its
    cards are then "unknown" and wait, the safe side."""
    if phase in cache:
        return cache[phase]
    parent = os.path.dirname(os.path.abspath(folder))
    found = {"folder": "", "order": [], "status": {}}
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        names = []
    for name in names:
        number = re.match(r"0*(\d+)(?![\d])", name)
        path = os.path.join(parent, name, "tasks.md")
        if number and int(number.group(1)) == int(phase) and name[number.end():][:1] in ("-", "_", " ", ".", "") \
                and os.path.isfile(path):
            cards, order = parse_tasks(read(path))
            resume = parse_resume(read(os.path.join(parent, name, "state", "RESUME.md")), order)
            found = {"folder": os.path.join(parent, name), "order": order,
                     "status": card_status(cards, order, resume)}
            break
    cache[phase] = found
    return found


def build(tasks: str, stale_hours: float) -> dict:
    text = read(tasks)
    cards, order = parse_tasks(text)
    batches, batch_drift = parse_batches(text, cards, order)
    batch_of = {c: b["id"] for b in batches for c in b["cards"]}
    folder = os.path.dirname(tasks)
    state_dir = os.path.join(folder, "state")
    resume_path = os.path.join(state_dir, "RESUME.md")
    has_resume = os.path.isfile(resume_path)
    resume = parse_resume(read(resume_path), order)
    handoff_dir = os.path.join(state_dir, "handoff")
    handoffs = {}
    for path in glob.glob(os.path.join(handoff_dir, "*.md")):
        if (when := mtime(path)) is not None:
            handoffs[os.path.basename(path)[:-3]] = when

    status = card_status(cards, order, resume)
    phases: dict = {}  # the other features this one's cards wait on, read once per build

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
        for dep in cards[cid]["external"]:  # another feature's card: read from that feature's files
            other = phase_cards(folder, dep["phase"], phases)
            named = ids_in(f"{dep['card']}–{dep['through']}", other["order"]) if dep.get("through") else [dep["card"]]
            for x in named:
                said = other["status"].get(x, "unknown")
                if said not in FINISHED:
                    reasons.append({"kind": "phase", "on": f"phase {dep['phase']}'s {x}", "status": said,
                                    "conditional": dep["conditional"]})
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

    registry = registry_of(read_json(os.path.join(state_dir, "runs.json"), {}))  # a hand-edited one too
    alive = {id(r): run_alive(r) for r in registry.get("runs", []) if not r.get("ended")}
    live_runs = [r for r in registry.get("runs", []) if alive.get(id(r))]
    batch_runs = {r["batch"]: r for r in live_runs if r.get("batch")}
    for b in batches:  # done when every card is finished, or when its line is ticked (drift if not both)
        if b["effort"] not in EFFORT_RANK:  # none written: the highest of its cards'
            tiers = [cards[c]["effort"] for c in b["cards"] if cards[c]["effort"] in EFFORT_RANK]
            b["effort"] = max(tiers, key=EFFORT_RANK.index) if tiers else b["effort"]
        b["open"] = [c for c in b["cards"] if status[c] not in FINISHED]
        b["finished"] = len(b["cards"]) - len(b["open"])
        b["current"] = b["open"][0] if b["open"] else ""
        b["live"] = b["id"] in batch_runs
        b["running"] = False
        everything = bool(b["cards"]) and not b["open"]
        if b["ticked"] and b["cards"] and not everything:
            batch_drift.append(f"batch {b['id']} is ticked in tasks.md but "
                               + ", ".join(f"{c} is {status[c]}" for c in b["open"]))
        elif everything and not b["ticked"]:
            batch_drift.append(f"every card of batch {b['id']} is finished but its line in tasks.md is not ticked")
        b["done"] = everything or b["ticked"]
        stages = [cards[c]["stage"] for c in b["cards"] if cards[c]["stage"]]
        b["stage"] = max(dict.fromkeys(stages), key=stages.count) if stages else ""
        mine = [r for r in registry.get("runs", []) if r.get("batch") == b["id"]]
        b["run_card"] = (batch_runs.get(b["id"]) or (mine[-1] if mine else {})).get("card", "")
    open_batch = {b["id"]: b for b in batches if not b["done"]}
    batch_drift += batch_rules(batches, cards, status, open_decisions)
    live_cards = [c for c in order if status[c] not in FINISHED and any(
        r.get("card") == c for r in live_runs)]
    # a live batch session works on its batch's first open card, whichever card it was started on
    live_cards += [b["current"] for b in open_batch.values() if b["live"] and b["current"] not in live_cards]
    # a session the autopilot started is running even before it marks its row doing
    doing = [c for c in order if status[c] == "doing" or c in live_cards]

    # a unit is what one session runs: a card, or the unfinished batch the card belongs to
    def unit_for(c: str) -> str:
        return batch_of[c] if batch_of.get(c) in open_batch else c

    def unit_parallel(u: str) -> bool:  # a batch is [P] by its checklist line, whatever its cards say
        return open_batch[u]["parallel"] if u in open_batch else cards[u]["parallel"]

    def unit_touches(u: str) -> list | None:
        mine = open_batch[u]["cards"] if u in open_batch else [u]
        return [p for c in mine for p in cards[c]["touches"]] if all(cards[c]["touches"] for c in mine) else None

    serial_doing = [c for c in doing if not unit_parallel(unit_for(c))]
    for b in open_batch.values():
        b["running"] = b["live"] or any(c in doing for c in b["cards"])
        if b["running"] and not b["parallel"] and not any(c in serial_doing for c in b["cards"]):
            # a batch without [P] holds the integration worktree whatever its cards say
            serial_doing.append(next((c for c in b["cards"] if c in doing), b["current"]))
    running_units = list(dict.fromkeys(unit_for(c) for c in doing))

    def touch_wait(u: str) -> dict | None:
        """A running unit whose Touches overlap u's (or can't be read): u waits for it to finish."""
        for other in running_units:
            if other == u:
                continue
            shared = touches_clash(unit_touches(u), unit_touches(other))
            if shared != "":
                return {"kind": "touches", "on": other, "status": "running", "shares": shared or ""}
        return None

    ready = []
    for cid in order:
        if status[cid] != "todo" or waits[cid] or cid in live_cards or batch_of.get(cid) in open_batch:
            continue  # a card of an unfinished batch runs with its batch, never on its own
        if not cards[cid]["parallel"] and serial_doing:
            # cards without [P] share the integration worktree: one at a time
            waits[cid].append({"kind": "worktree", "on": serial_doing[0], "status": "doing"})
            continue
        if clash := touch_wait(cid):  # never beside a running card or batch whose Touches overlap
            waits[cid].append(clash)
            continue
        ready.append(cid)
    for b in batches:
        # what the batch waits for outside itself: its cards run in order, so waits inside it don't count
        b["waits"] = []
        for c in b.get("open", []):
            for w in waits.get(c, []):
                if not (w["kind"] == "card" and w["on"] in b["cards"]) and w not in b["waits"]:
                    b["waits"].append(w)
        if b["done"]:
            b["status"] = "done"
        elif not b["cards"]:
            b["status"] = "waiting"  # names no card: drift, nothing to run
        elif b["running"]:
            b["status"] = "running"
        else:
            held = [c for c in serial_doing if c not in b["cards"]]
            if not b["waits"] and held and not b["parallel"]:
                b["waits"].append({"kind": "worktree", "on": held[0], "status": "doing"})
            if not b["waits"] and (clash := touch_wait(b["id"])):
                b["waits"].append(clash)
            b["status"] = "waiting" if b["waits"] else "ready"
    ready_batches = [b for b in batches if b["status"] == "ready"]

    unblocks = {}
    for cid in doing + ready:
        unblocks[cid] = [
            other for other in order
            if waits.get(other) and all(w.get("on") == cid for w in waits[other])
        ]
    for b in batches:  # what finishing the whole batch unblocks: cards outside it that wait only on it
        if b["status"] in ("ready", "running"):
            unblocks[b["id"]] = [
                other for other in order if other not in b["cards"] and waits.get(other)
                and all(w.get("on") in b["cards"] for w in waits[other])
            ]

    stamp = now().timestamp()
    stalled = []
    for cid in doing:
        row = resume["status"].get(cid, {})
        # the newest sign of work: its branch moving, its hand-off, its RESUME row's time, and its latest
        # session's start and last output (a batch's session counts for the card the batch is on); a sign
        # from the future (a local time written as UTC, a skewed clock) says nothing. A bare day on its
        # row counts only when there is nothing finer
        signs = [last_commit(folder, row.get("branch", "")), handoffs.get(cid), row_time(row.get("date", ""))]
        run = next((r for r in reversed(registry["runs"]) if r.get("card") == cid
                    or (batch_of.get(cid) and r.get("batch") == batch_of[cid])), None)
        signs += run_times(state_dir, run) if run else []
        last = max((x for x in signs if x and x <= stamp + 300), default=None) \
            or row_time(row.get("date", ""), day=True)
        if last and stamp - last > stale_hours * 3600:
            stalled.append({"card": cid, "quiet": ago(stamp - last)})

    drift = []
    if has_resume:
        for cid in order:
            row = resume["status"].get(cid)
            if not row:
                drift.append(f"{cid} is in tasks.md but has no row in RESUME's status table")
            elif row["status"] and row["status"] not in STATUSES:  # its tick in tasks.md stands in for it
                drift.append(f"{cid} has unknown status '{row['status']}' in RESUME"
                             f" (one of {', '.join(sorted(STATUSES))})")
            elif cards[cid]["ticked"] and row["status"] not in FINISHED:
                drift.append(f"{cid} is ticked in tasks.md but RESUME says {row['status'] or 'nothing'}")
            elif row["status"] in FINISHED and not cards[cid]["ticked"]:
                drift.append(f"{cid} is {row['status']} in RESUME but not ticked in tasks.md")
        for cid in resume["status"]:
            if cid not in cards:
                drift.append(f"RESUME has a row for {cid}, which tasks.md does not define")
    for cid in order:
        # an owner's card is marked done on the dashboard, which writes no hand-off
        if status[cid] == "done" and cid not in handoffs and has_resume and cards[cid]["kind"] != "owner":
            drift.append(f"{cid} is done but state/handoff/{cid}.md is missing")
        for dep in cards[cid]["after"]:
            if dep not in cards:
                drift.append(f"{cid} waits for {dep}, which tasks.md does not define")
    drift += tasks_drift(text, cards, order, status)
    drift += batch_drift
    # features whose templates have a Checks table: a pinning test changed by any card but its writer is
    # drift until a checkpoint reviews it, and every done card shows its checks with their evidence
    open_checks = []
    if re.search(r"\|\s*criterion\s*\|\s*verdict\s*\|", text, re.I):
        drift += pin_drift(tasks, cards, resume["status"], pinned_tests(text), resume["pins_reviewed"])
        review = read(os.path.join(state_dir, "design-review.md")).splitlines()
        # a feature upgraded mid-way names the day Checks began: cards finished before it are exempt
        since = re.search(r"\*\*Checks from:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
        for cid in order:
            if status[cid] != "done" or cid not in handoffs:
                continue
            if since:
                day = re.search(r"\d{4}-\d{2}-\d{2}", resume["status"].get(cid, {}).get("date", ""))
                if not day or day.group(0) < since.group(1):
                    continue
            checks = handoff_checks(read(os.path.join(handoff_dir, f"{cid}.md")))
            if not checks:
                drift.append(f"{cid} is done but its hand-off has no Checks table (criterion | verdict | evidence)")
                continue
            for r in checks:
                said, note = r["verdict"], ""
                if said == "pass" and has_evidence(r["evidence"]):
                    continue
                if said == "pass":
                    note = "no evidence"
                elif said == "waived":
                    if any(cid in d["before"] and not d["open"] for d in resume["decisions"]):
                        continue  # the owner's answer in RESUME's decisions waived it
                    note = "waived without an owner's decision"
                elif said == "fail":
                    named = any(re.search(rf"\b{cid}\b", line) for line in review)
                    note = "in design-review.md" if named else "not in design-review.md"
                else:
                    said = said or "unmarked"
                    note = flat(r["correction"], 80, cell=False) if has_evidence(r["correction"]) else ""
                open_checks.append({"card": cid, "criterion": flat(r["criterion"], 120, cell=False),
                                    "verdict": said, "note": note})
    lock = resume["lock"]
    # "free (released by T009)" is free: the card it names let go of it
    holder = "" if resume["lock_free"] else next((x for x in ID_RE.findall(lock) if x in cards), "")
    if holder and status.get(holder) in FINISHED:
        drift.append(f"the deploy lock is still held by {holder}, which is {status[holder]}")
    merging = merge_lock(state_dir)
    if merging and merging["holder"]:
        who = merging["holder"]
        b = next((x for x in batches if x["id"] == who), None)
        over = "done" if b and b["done"] else status.get(who) if status.get(who) in FINISHED else ""
        if over:
            drift.append(f"the merge lock (state/{MERGE_LOCK}) is still held by {who}, which is {over}")

    runs: dict = {}
    # a resumed session reports its running total, so a session costs the most any of its runs reported
    # (a batch's session is resumed on later cards of the batch: it still counts once)
    session_cost: dict = {}
    for run in registry.get("runs", []):
        key = run.get("session") or id(run)
        session_cost[key] = max(session_cost.get(key, 0.0), number(run.get("cost")))
    for run in registry.get("runs", []):
        cid = run.get("card", "")
        live = bool(alive.get(id(run)))
        entry = runs.setdefault(cid, {"sessions": 0, "cost": 0.0, "keys": set()})
        entry["sessions"] += 1
        entry["keys"].add(run.get("session") or id(run))
        entry["cost"] = round(sum(session_cost[k] for k in entry["keys"]), 4)
        entry.update({
            "live": live, "session": run.get("session", ""), "reason": run.get("reason", ""),
            "started": run.get("started", ""), "ended": run.get("ended", ""),
            "result": run.get("result", ""), "error": run.get("error", ""), "batch": run.get("batch", ""),
        })
    for entry in runs.values():
        entry.pop("keys")
    attention = registry.get("attention", {})
    runs_file = os.path.join(state_dir, "runs.json")
    beat = mtime(runs_file)
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
            try:
                files = sorted(f for f in os.listdir(path) if IMAGE_RE.match(f))
            except OSError:  # removed since the glob
                files = []
            if files:
                screens[name] = files

    at = re.search(r"^## 5\.", text, re.M)
    planned = {m.group(2) for m in CHECK_RE.finditer(text) if not at or m.start() < at.start()}
    backlog = {c for c in order if c not in planned}
    loose = unbatched(cards, order, status, doing, batch_of, open_decisions, backlog)

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
        "batches": batches,
        "ready_batches": ready_batches,
        "batch_of": batch_of,
        "waiting": {c: w for c, w in waits.items() if w},
        "unblocks": unblocks,
        "open_decisions": open_decisions,
        "blockers": active_blockers,
        "lock": lock,
        "lock_free": resume["lock_free"],
        "merge_lock": merging,
        "stalled": stalled,
        "drift": drift,
        "open_checks": open_checks,
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
        "unbatched": loose,
        "screens": screens,
        "autopilot": {
            "used": settings["exists"],
            "dispatcher": dispatcher_alive(state_dir),
            "settings": {k: v for k, v in settings.items() if k != "exists"},
            "runs": runs,
            "live": [c for c, r in runs.items() if r["live"]],
            "attention": attention,
            "manual": registry.get("manual", []),
            "queued": [c for c in registry.get("queued", []) if (c in cards and status[c] not in FINISHED) or c in open_batch],
            "blocked_on": {c: v for c, v in registry.get("blocked_on", {}).items()
                           if (c in cards and status[c] not in FINISHED) or c in open_batch},
            "unkinded": [c for c in order if not cards[c]["kind"] and status[c] not in FINISHED],
            "beat": beat,
            "spent_usd": round(sum(session_cost.values()), 2),
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


def wait_text(reasons: list) -> str:
    """What a card or batch waits for, in words."""
    parts = []
    for r in reasons:
        if r["kind"] == "card":
            parts.append(f"{r['on']} ({r['status']}{', conditional' if r.get('conditional') else ''})")
        elif r["kind"] == "phase":  # another feature's card
            said = "not found" if r["status"] == "unknown" else r["status"]
            parts.append(f"{r['on']} ({said}{', conditional' if r.get('conditional') else ''})")
        elif r["kind"] == "worktree":
            parts.append(f"the integration worktree ({r['on']} is doing)")
        elif r["kind"] == "touches":
            parts.append(f"{r['on']} to finish (running; "
                         + (f"their Touches overlap: {r['shares']})" if r["shares"] else "Touches not readable on both)"))
        elif r["kind"] == "owner":
            parts.append(f"you: {r['on']}")
        elif r["kind"] == "gate":
            parts.append(f"your review of stage checkpoint {r['on']}")
        else:
            parts.append(f"blocker: {r['on']}")
    return "; ".join(parts)


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
        b = next((b for b in s["batches"] if b["id"] == s["batch_of"].get(cid) and b["status"] == "running"), None)
        if b:
            extra = ", ".join(x for x in (f"batch {b['id']}, {b['finished']}/{len(b['cards'])} finished", extra) if x)
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
    for b in s["ready_batches"]:
        lines.append(f"  {b['id']}{' [P]' if b.get('parallel') else ''} {b['name']} · batch of {len(b['cards'])} ({', '.join(b['cards'])})"
                     + (f" · effort {b['effort']}" if b["effort"] else ""))
        if b["start_with"]:
            lines.append(f"    Start with: {b['start_with']}")
        if s["unblocks"].get(b["id"]):
            lines.append(f"    finishing it unblocks {', '.join(s['unblocks'][b['id']])}")
    if not s["ready"] and not s["ready_batches"]:
        lines.append("  nothing" + (" until a running card finishes" if s["doing"] else ""))
    parallel = [c for c in s["ready"] if s["cards"][c]["parallel"]]
    parallel += [b["id"] for b in s["ready_batches"] if b.get("parallel")]
    if len(s["ready"]) + len(s["ready_batches"]) > 1 and parallel:
        lines.append(f"  May run side by side, each in its own worktree: {', '.join(parallel)}"
                     " (plus at most one card or batch without [P]; never two whose Touches overlap)")

    lines.append("")
    lines.append("Waiting:")
    for cid, reasons in s["waiting"].items():
        lines.append(f"  {label(s, cid)} waits for {wait_text(reasons)}")
    # a batch's waits outside its cards' own: the integration worktree, a running unit's Touches
    held = [b for b in s["batches"] if b["status"] == "waiting" and b["cards"]
            and any(w["kind"] in ("worktree", "touches") for w in b["waits"])]
    for b in held:
        lines.append(f"  {b['id']}{' [P]' if b.get('parallel') else ''} {b['name']} (batch) waits for {wait_text(b['waits'])}")
    if not s["waiting"] and not held:
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
    if not s["lock_free"]:
        lines.append(f"Deploy lock: {s['lock']}")
    if s.get("merge_lock"):
        m = s["merge_lock"]
        lines.append(f"Merge lock: held by {m['holder'] or 'a session that has not said who it is'}"
                     + (f" since {m['since']}" if m["since"] else ""))
    if s.get("unbatched"):
        lines.append("")
        lines.append("Heads-up:")
        lines += [f"  {u['text']}" for u in s["unbatched"]]
    if s["drift"]:
        lines.append("")
        lines.append("Drift (files disagree):")
        lines += [f"  {d}" for d in s["drift"]]
    if s["open_checks"]:
        lines.append("")
        lines.append("Open checks (the owner decides):")
        lines += [f"  {check_line(c)}" for c in s["open_checks"]]
    return "\n".join(lines)


def check_line(c: dict) -> str:
    return f"{c['card']} \"{c['criterion']}\": {c['verdict']}" + (f" ({c['note']})" if c["note"] else "")


def is_try(run: dict) -> bool:
    """A session that counts against a card's attempts: not one that never reached the API, one resumed
    with the owner's answer that did work, one that split its card, or one that stopped to wait for the
    owner, unless RESUME had no pending approval or open decision for it when it ended ("unbacked",
    autopilot.py judges that): a session that only says it waits would otherwise be resumed for ever."""
    result = run.get("result") or ""
    if run.get("api_error"):
        return False
    if str(run.get("reason") or "").startswith("answer"):
        return bool(run.get("ended")) and not run.get("worked")
    return not SPLIT.search(result) and (not WAITED.search(result) or bool(run.get("unbacked")))


def lessons(s: dict) -> str:
    """What each card really took (runs, tries, cost, hours) beside what the plan guessed (size, effort),
    as Markdown for the close card's retro and specs/lessons.md. Only measured numbers; a card the
    autopilot never ran shows "-"."""
    registry = read_json(os.path.join(os.path.dirname(s["tasks"]), "state", "runs.json"), {})
    registry = registry if isinstance(registry, dict) else {}
    runs, attention = registry.get("runs", []), registry.get("attention", {})
    resume = parse_resume(read(s["resume"]), list(s["cards"])) if s["resume"] else {"approvals": [], "decisions": []}
    utc = lambda v: dt.datetime.strptime(v, "%Y-%m-%d %H:%MZ").replace(tzinfo=dt.timezone.utc).timestamp()  # noqa: E731
    def measure(mine: list) -> tuple:
        """runs, tries, cost and hours of some runs (each session's cost once, at its highest report)."""
        cost: dict = {}
        hours = 0.0
        for r in mine:
            key = r.get("session") or id(r)
            cost[key] = max(cost.get(key, 0.0), float(r.get("cost") or 0))
            try:
                if r.get("ended"):
                    began = float(r["started_ts"]) if r.get("started_ts") else utc(r["started"])
                    hours += max(utc(r["ended"]) - began, 0) / 3600  # "ended" keeps minutes only
            except (KeyError, ValueError, TypeError):
                pass
        return len(mine), sum(1 for r in mine if is_try(r)), sum(cost.values()), hours

    lines = ["| card | kind | size | effort | model | status | runs | tries | cost $ | hours | needed the owner |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    tiers: dict = {}
    batch_of = s.get("batch_of", {})
    for cid, c in s["cards"].items():
        mine = [r for r in runs if r.get("card") == cid and not r.get("batch")]  # a batch is measured whole
        n, tries, cost, hours = measure(mine)
        blank = f"in {batch_of[cid]}" if cid in batch_of and not mine else "-"
        lines.append("| " + " | ".join(flat(v) for v in (
            cid, c["kind"] or "-", c["size"] or "-", c["effort"] or "-", c["model"] or "-", s["status"][cid],
            n or blank, tries if mine else blank, round(cost, 2) if mine else blank,
            round(hours, 1) if mine else blank, owner_part(cid, mine, resume, attention) or "-")) + " |")
        if mine:
            tier = tiers.setdefault(c["effort"] or "-", {"cards": 0, "runs": 0, "tries": 0, "cost": 0.0, "retried": 0})
            tier["cards"] += 1
            tier["runs"] += n
            tier["tries"] += tries
            tier["cost"] += cost
            tier["retried"] += tries > 1
    out = [f"# What the cards took: {s['feature']}", "", *lines, "",
           "Runs: every session the autopilot started or resumed for the card. Tries: the runs that count"
           " against its attempts (not waiting for the owner, not resumed with an answer, not an API error)."
           " Cards run by hand have no measurements (\"-\")"
           + ("; a batch's cards say `in B<n>`, and the batch is measured as a whole in the next table."
              if s.get("batches") else ".")]
    if s.get("batches"):
        out += ["", "| batch | cards | effort | runs | tries | cost $ | hours |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for b in s["batches"]:
            n, tries, cost, hours = measure([r for r in runs if r.get("batch") == b["id"]])
            out.append("| " + " | ".join(flat(v) for v in (
                f"{b['id']} {b['name']}".strip(), ", ".join(b["cards"]) or "-", b["effort"] or "-", n or "-",
                tries if n else "-", round(cost, 2) if n else "-", round(hours, 1) if n else "-")) + " |")
    if tiers:
        out += ["", "| effort | cards run | tries per card | runs per card | cost per card $ | cards needing more than one try |",
                "| --- | --- | --- | --- | --- | --- |"]
        out += [f"| {flat(t)} | {v['cards']} | {v['tries'] / v['cards']:.1f} | {v['runs'] / v['cards']:.1f}"
                f" | {v['cost'] / v['cards']:.2f} | {v['retried']} |" for t, v in sorted(tiers.items())]
    if s["drift"]:
        out += ["", "Drift at the close:", *[f"- {d}" for d in s["drift"]]]
    if s["open_checks"]:
        out += ["", "Open checks at the close (the owner decides):", *[f"- {check_line(c)}" for c in s["open_checks"]]]
    return "\n".join(out)


def owner_part(cid: str, mine: list, resume: dict, attention: dict) -> str:
    """What a card needed from the owner, read from its runs and RESUME: "2 approvals, 1 decision, owner retry"."""
    approvals = {a["n"] for a in resume["approvals"] if a["card"] == cid}
    approvals |= {r["reason"].split(" ", 1)[1] for r in mine if str(r.get("reason") or "").startswith("answer ")}
    decisions = sum(1 for d in resume["decisions"] if cid in d["before"])
    by_owner = [r for r in mine if str(r.get("reason") or "").endswith("(owner)")]
    starts = sum(1 for r in by_owner if r is mine[0] and r["reason"].startswith("start"))
    retries = len(by_owner) - starts
    parts = [f"{len(approvals)} approval{'s' * (len(approvals) != 1)}" if approvals else "",
             f"{decisions} decision{'s' * (decisions != 1)}" if decisions else "",
             "owner start" if starts else "",
             (f"{retries} owner retries" if retries > 1 else "owner retry") if retries else ""]
    said = ", ".join(p for p in parts if p)
    return said or flat(attention.get(cid, ""), 60)


def snapshot(s: dict) -> dict:
    """What --wait compares (each field a str, list or dict, which like() checks a saved one against)."""
    merging = s.get("merge_lock")
    return {
        "status": s["status"],
        "ready": s["ready"],
        "batches": {b["id"]: b["status"] for b in s.get("batches", [])},
        "open_decisions": [f"{d['n']}: {d['question']}" for d in s["open_decisions"]],
        "blockers": [b["text"] for b in s["blockers"]],
        "lock": s["lock"],
        "handoffs": s["handoffs"],
        "stalled": [x["card"] for x in s["stalled"]],
        "drift": s["drift"],
        "open_checks": [check_line(c) for c in s["open_checks"]],
        "approvals": [f"{a['n']} {a['card']}: {a['step']}" for a in s["approvals"]],
        "gates": s["gates"],
        "attention": {c: flat(why, 200, cell=False) for c, why in s["autopilot"]["attention"].items()},
        "live": sorted(str(c) for c in s["autopilot"]["live"]),
        "merge_lock": f"held by {merging['holder'] or 'a session that has not said who it is'}"
                      + (f" since {merging['since']}" if merging["since"] else "") if merging else "",
        "paused": str(s["autopilot"]["settings"]["paused_reason"] or ""),
        "commits": [f"{c['hash'][:7]} {c['subject']}" for c in s["commits"][:10]],
    }


def like(saved, new: dict) -> dict | None:
    """The last --wait's snapshot with new's fields in new's types; a field it lacks (say, from an older
    supervisor) or holds in another type takes new's value, since it can't say what changed. None when
    it is no snapshot at all."""
    if not isinstance(saved, dict) or not isinstance(saved.get("status"), dict):
        return None
    return {k: saved[k] if isinstance(saved.get(k), type(v)) else v for k, v in new.items()}


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
    for bid, st in new.get("batches", {}).items():
        before = old.get("batches", {}).get(bid)
        if st == before:
            continue
        out.append(f"batch {bid} is now ready" if st == "ready" else f"batch {bid} is done" if st == "done"
                   else f"new batch {bid} ({st})" if before is None else f"batch {bid}: {before} → {st}")
    out += [f"hand-off {cid}.md written" for cid in new["handoffs"] if cid not in old["handoffs"]]
    out += [f"decision answered: {d}" for d in old["open_decisions"] if d not in new["open_decisions"]]
    out += [f"decision needed: {d}" for d in new["open_decisions"] if d not in old["open_decisions"]]
    out += [f"blocker cleared: {b}" for b in old["blockers"] if b not in new["blockers"]]
    out += [f"new blocker: {b}" for b in new["blockers"] if b not in old["blockers"]]
    if old["lock"] != new["lock"]:
        out.append(f"deploy lock: {old['lock'] or 'unset'} → {new['lock'] or 'unset'}")
    out += [f"{cid} looks stalled" for cid in new["stalled"] if cid not in old["stalled"]]
    out += [f"drift: {d}" for d in new["drift"] if d not in old["drift"]]
    out += [f"open check: {c}" for c in new.get("open_checks", []) if c not in old.get("open_checks", [])]
    out += [f"approval needed: {a}" for a in new.get("approvals", []) if a not in old.get("approvals", [])]
    out += [f"stage {g} finished and waits for your review" for g in new.get("gates", []) if g not in old.get("gates", [])]
    said = old.get("attention", {})
    out += [f"{c} needs you: {why}" for c, why in new.get("attention", {}).items() if said.get(c) != why]
    out += [f"{c} no longer needs you" for c in said if c not in new.get("attention", {})]
    out += [f"{c}: a session started" for c in new.get("live", []) if c not in old.get("live", [])]
    out += [f"{c}: its session ended" for c in old.get("live", []) if c not in new.get("live", [])]
    if old.get("merge_lock", "") != new.get("merge_lock", ""):
        out.append(f"merge lock: {old.get('merge_lock') or 'free'} → {new.get('merge_lock') or 'free'}")
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
    mode.add_argument("--lessons", action="store_true",
                      help="print what each card took (runs, tries, cost, hours) for the close card's retro")
    parser.add_argument("--autopilot", action="store_true",
                        help="also run the dispatcher that starts card sessions (with --serve, or alone)")
    parser.add_argument("--port", type=int, default=8765, help="dashboard port (8765)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--interval", type=float, default=5, help="seconds between reads (5)")
    parser.add_argument("--stale-hours", type=float, default=4,
                        help="a doing card with no sign of work for this long (a commit only its branch holds,"
                             " its hand-off, its RESUME date, its session's output) is stalled (4)")
    args = parser.parse_args()
    tasks = find_tasks(args.tasks)

    if args.json:
        print(json.dumps(build(tasks, args.stale_hours), indent=2, ensure_ascii=False))
    elif args.lessons:
        print(lessons(build(tasks, args.stale_hours)))
    elif args.watch:
        shown, drawn = None, 0.0
        try:
            while True:
                try:
                    body = render(build(tasks, args.stale_hours))
                except Exception as error:  # shown in place of the view; the next read may succeed
                    body = f"{tasks}\nsupervisor: could not read the state: {type(error).__name__}: {error}"
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
        import autopilot
        stop = threading.Event()
        if enable(tasks):
            print("Autopilot: new on this feature, so it starts paused; press Resume on the dashboard"
                  " (supervisor.py … --serve) to let it run", flush=True)
        print(f"Autopilot: dispatching {tasks} (Ctrl-C stops; running sessions keep going)", flush=True)
        try:
            autopilot.loop(lambda: users(tasks), args.interval, stop, lambda line: print(line, flush=True))
        except KeyboardInterrupt:
            stop.set()
    elif args.wait:
        path = os.path.join(os.path.dirname(tasks), "state", ".supervisor.json")
        saved, old = read_json(path, None), None
        while True:
            state = build(tasks, args.stale_hours)
            new = snapshot(state)
            if old is None:
                old = like(saved, new)
                if old is None:  # first run, or a snapshot that is missing or broken: this is the baseline
                    old = new
                    save(path, new)
            if new != old:
                save(path, new)
                print("Changed:")
                print("\n".join(f"  {line}" for line in changes(old, new)) or "  (details only)")
                print()
                print(render(state))
                return
            time.sleep(args.interval)
    else:
        print(render(build(tasks, args.stale_hours)))


NEW_PAUSED = "new: press Resume to start"


def enable(tasks: str) -> bool:
    """--autopilot on a feature that has never used it: create its settings, paused, so the owner looks at
    the dashboard first (or tries one card with its Start button) and presses Resume to let it run.
    True when it created them."""
    path = os.path.join(os.path.dirname(tasks), "state", "autopilot.json")
    if os.path.exists(path):
        return False
    save(path, {**SETTINGS, "auto": False, "paused_reason": NEW_PAUSED})
    return True


def users(tasks: str) -> list:
    """Every feature beside tasks that uses the autopilot."""
    specs = os.path.dirname(os.path.dirname(tasks))
    return sorted(p for p in glob.glob(os.path.join(specs, "*", "tasks.md"))
                  if os.path.exists(os.path.join(os.path.dirname(p), "state", "autopilot.json")))


MAX_BODY = 1 << 20  # the largest action the dashboard may post (its answers are a few hundred bytes)


def finite(text: str) -> float:
    """A JSON number the dashboard could have sent; ValueError for one that is not finite."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


LOCKED = """<!doctype html><meta charset="utf-8"><title>Spec-Grill dashboard</title>
<p style="font: 16px system-ui; margin: 2em">This dashboard opens only from the link printed in the terminal that
started it (<code>supervisor.py … --serve</code>): the link carries this launch's key. Open that link, or start
the supervisor with <code>--open</code>.</p>"""


def serve(tasks: str, port: int, stale_hours: float, open_browser: bool, dispatch: bool = False) -> None:
    import secrets
    import shutil
    import tempfile
    import traceback
    from http.cookies import CookieError, SimpleCookie
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, quote, urlparse

    specs = os.path.dirname(os.path.dirname(tasks))
    default = os.path.basename(os.path.dirname(tasks))
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    token = secrets.token_urlsafe(24)  # the page carries it; other sites can't read it, so can't post
    # This launch's key. The page (and so the token) goes only to a request that carries it: in the link
    # printed to this terminal (?k=), or in the cookie that link sets. It never enters argv or the
    # environment, so a card session can't fetch the page and post actions as the owner. Sessions run as
    # the same OS user, though: this raises the bar, it is no sandbox.
    key = secrets.token_urlsafe(24)
    handed: list = []  # the private folder --open may forward the browser through; removed once used

    def forget() -> None:  # (a request thread and the timer may both call it)
        while True:
            try:
                folder = handed.pop()
            except IndexError:
                return
            shutil.rmtree(folder, ignore_errors=True)

    def forwarder(link: str) -> str:
        """A private page that sends the browser on to link, for a browser webbrowser starts with the link
        on its command line (Linux, or $BROWSER), where `ps` would show the key for the browser's whole
        life. Removed once the dashboard has loaded through it, or after a minute."""
        folder = tempfile.mkdtemp(prefix="spec-grill-")  # only this user may enter it
        path = os.path.join(folder, "dashboard.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={link}">'
                         f'<a href="{link}">Open the dashboard</a>')
        handed.append(folder)
        timer = threading.Timer(60, forget)
        timer.daemon = True
        timer.start()
        return "file://" + path

    def features() -> list:
        return sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(specs, "*", "tasks.md")))

    def tasks_of(name: str) -> str | None:
        name = name or default
        return os.path.join(specs, name, "tasks.md") if name in features() else None  # no paths from the URL

    failed = [""]  # the last error a request met, printed once

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # a request that sends less than its Content-Length gives its thread back after this

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.guarded(self.post)

        def do_GET(self) -> None:
            self.guarded(self.get)

        def guarded(self, answer) -> None:
            """Answer a request; an error answers 500 with its text, instead of dropping the connection."""
            try:
                answer()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page went away
            except Exception as error:
                said = f"{type(error).__name__}: {error}"
                if said != failed[0]:
                    failed[0] = said
                    print(f"supervisor: {self.command} {self.path.split('?', 1)[0]} failed\n{traceback.format_exc()}",
                          file=sys.stderr, flush=True)
                try:  # the dashboard shows an action's answer as text; the GET API answers JSON
                    if self.command == "POST":
                        self.send(500, f"the supervisor failed: {said}", "text/plain; charset=utf-8")
                    else:
                        self.send(500, json.dumps({"error": said}), "application/json; charset=utf-8")
                except OSError:
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

        def cookie(self) -> str:  # one per port: a browser sends 127.0.0.1's cookies to every port
            return f"spec_grill_{self.server.server_address[1]}"

        def owner(self, query: dict) -> bool:
            """The request carries this launch's key: in the link printed to the terminal, or its cookie."""
            given = query.get("k", [""])[0]
            if not given:
                jar = SimpleCookie()
                try:
                    jar.load(self.headers.get("Cookie") or "")
                except CookieError:
                    pass
                given = jar[self.cookie()].value if self.cookie() in jar else ""
            return secrets.compare_digest(given.encode(), key.encode())

        def post(self) -> None:
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
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.send(400, "bad Content-Length", "text/plain")
            if length < 0:  # rfile.read(-1) would wait for the client to hang up
                return self.send(400, "bad Content-Length", "text/plain")
            if length > MAX_BODY:
                return self.send(413, f"request too large (at most {MAX_BODY} bytes)", "text/plain")
            try:  # Infinity, NaN and 1e999 are no JSON numbers: they would only reach act() to fail there
                data = json.loads(self.rfile.read(length) or b"{}", parse_float=finite, parse_constant=finite)
            except ValueError:
                return self.send(400, "bad JSON", "text/plain")
            if not isinstance(data, dict):
                return self.send(400, "bad JSON: the body must be an object", "text/plain")
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

        def get(self) -> None:
            if not self.local():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            name = query.get("f", [""])[0]
            if url.path == "/":
                if not self.owner(query):
                    return self.send(403, LOCKED, "text/html; charset=utf-8")
                if "k" in query:  # the key goes into a cookie, and out of the address bar
                    forget()
                    # Lax, not Strict: Chrome drops a Strict cookie on a redirect that began on another site
                    # (the forwarder's file://), and the cookie only unlocks a page no other site can read
                    self.send_response(303)
                    self.send_header("Location", "/" + (f"?f={quote(name)}" if name else ""))
                    self.send_header("Set-Cookie", f"{self.cookie()}={key}; HttpOnly; SameSite=Lax; Path=/")
                    self.send_header("Cache-Control", "no-store")
                    self.frame_guard()
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
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
    # only this terminal sees the key (--open hands the link to the browser; on macOS via osascript's stdin)
    url = f"http://127.0.0.1:{server.server_address[1]}/?k={key}"
    print(f"Dashboard: {url} (Ctrl-C stops)", flush=True)
    stop = threading.Event()
    if dispatch:
        import autopilot
        if enable(tasks):
            print("Autopilot: new on this feature, so it starts paused; press Resume on the dashboard to let it run",
                  flush=True)
        worker = threading.Thread(target=autopilot.loop, daemon=True,
                                  args=(lambda: users(tasks), 5, stop, lambda line: print(line, flush=True)))
        worker.start()
        print("Autopilot: dispatching; running sessions keep going if this stops", flush=True)
    if open_browser:
        import webbrowser
        # macOS: webbrowser hands the link to osascript on its stdin; elsewhere it goes on a command line
        direct = sys.platform == "darwin" and not os.environ.get("BROWSER")
        webbrowser.open(url if direct else forwarder(url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        forget()
        stop.set()
        if dispatch:
            worker.join(timeout=10)


def save(path: str, data: dict) -> None:
    """Write JSON whole: into a file beside path, then renamed over it, so no reader sees half of it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # the write failed
            os.remove(tmp)


if __name__ == "__main__":
    main()
