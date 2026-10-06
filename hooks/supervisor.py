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

It reads tasks.md (the checklist and each card's "after:" and "blocks:" fields),
state/RESUME.md (status table, decisions, blockers, deploy lock), state/handoff/*.md,
and git (the last commit on a doing card's branch, to spot stalled cards). It never
writes anything except the --wait snapshot, state/.supervisor.json. The dashboard page is
dashboard.html next to this file. Standard library only.
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time

ID = r"(?:T\d+[A-Z]*|CP[A-Z0-9]+)"
ID_RE = re.compile(rf"\b{ID}\b")
RANGE_RE = re.compile(rf"\b({ID})\s*[–-]\s*({ID})\b")
CHECK_RE = re.compile(rf"^- \[([ xX])\] ({ID})((?: \[P\])?) (.+?)(?: — fulfills .*)?$", re.M)
SECTION_RE = re.compile(r"^#{2,3} (.+?)\s*$", re.M)
HEAD_RE = re.compile(rf"^#{{3,4}} ({ID})((?: \[P\])?) — (.+?)\s*$", re.M)
FINISHED = {"done", "waived"}
STATUSES = {"todo", "doing", "done", "blocked", "waived"}
OPEN_ANSWERS = {"", "-", "—", "?", "tbd", "todo", "open", "pending"}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


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
            }
            order.append(cid)
        return cards[cid]

    for m in CHECK_RE.finditer(text):
        c = card(m.group(2))
        c["ticked"] |= m.group(1).lower() == "x"
        c["parallel"] |= bool(m.group(3))
        c["title"] = c["title"] or m.group(4).strip()

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
        meta = body.split("**", 1)[0]  # the lines before the first bold field
        if found := re.search(r"\bafter:\s*([^·\n]*)", meta):
            c["after_text"] = found.group(1).strip()
        if found := re.search(r"\bblocks:?\s*([^·\n]*)", meta):
            c["blocks_text"] = found.group(1).strip()
        if found := re.search(r"·\s*([SML])\s*(?:·|$)", meta, re.M):
            c["size"] = found.group(1)
        if found := re.search(r"\beffort\s+(\w+)", meta):
            c["effort"] = found.group(1)
        if found := re.search(r"\*\*Start with:\*\*\s*`([^`]+)`", body):
            c["start_with"] = found.group(1)

    for c in cards.values():
        c["after"] = ids_in(c["after_text"], order)
    for c in cards.values():  # "blocks: T009" on a backlog card makes T009 wait for it
        for target in ids_in(c["blocks_text"], order):
            if target in cards and c["id"] not in cards[target]["after"]:
                cards[target]["after"].append(c["id"])
    return cards, order


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
    cells = lambda line: [cell.strip() for cell in line.strip("|").split("|")]  # noqa: E731
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
            "answer": answer,
            "open": plain in OPEN_ANSWERS or plain.startswith("deferred"),
        })
    blockers = []
    for line in section(secs, "blockers"):
        item = line.strip()
        if not item.startswith("- "):
            continue
        item = item[2:].strip()
        if item.lower().strip("().") in ("", "none") or item.startswith("<"):
            continue
        blockers.append({"text": item, "cards": ID_RE.findall(item)})
    lock = next((line.strip() for line in section(secs, "deploy lock") if line.strip()), "")
    return {"status": status, "decisions": decisions, "blockers": blockers, "lock": lock}


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

    open_decisions = [d for d in resume["decisions"] if d["open"]]
    waits: dict = {}
    for cid in order:
        if status[cid] in FINISHED:
            continue
        reasons = []
        for dep in cards[cid]["after"]:
            if status.get(dep) not in FINISHED:
                reasons.append({"kind": "card", "on": dep, "status": status.get(dep, "unknown")})
        for d in open_decisions:
            if cid in d["before"]:
                reasons.append({"kind": "owner", "on": f"decision {d['n']}: {d['question']}"})
        for b in resume["blockers"]:
            if cid in b["cards"]:
                reasons.append({"kind": "blocker", "on": b["text"]})
        if status[cid] == "blocked" and not reasons:
            reasons.append({"kind": "blocker", "on": "marked blocked in RESUME"})
        waits[cid] = reasons

    doing = [c for c in order if status[c] == "doing"]
    serial_doing = [c for c in doing if not cards[c]["parallel"]]
    ready = []
    for cid in order:
        if status[cid] != "todo" or waits[cid]:
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
        "blockers": resume["blockers"],
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

    lines.append("")
    lines.append("Running now:")
    for cid in s["doing"]:
        row = s["rows"].get(cid, {})
        extra = ", ".join(x for x in (row.get("branch"), row.get("date") and f"since {row['date']}") if x)
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
                parts.append(f"{r['on']} ({r['status']})")
            elif r["kind"] == "worktree":
                parts.append(f"the integration worktree ({r['on']} is doing)")
            elif r["kind"] == "owner":
                parts.append(f"you: {r['on']}")
            else:
                parts.append(f"blocker: {r['on']}")
        lines.append(f"  {label(s, cid)} waits for {'; '.join(parts)}")
    if not s["waiting"]:
        lines.append("  nobody")

    needs = [f"decision {d['n']}: {d['question']} (needed before {', '.join(d['before']) or '?'})"
             for d in s["open_decisions"]]
    needs += [f"blocker: {b['text']}" for b in s["blockers"]]
    needs += [f"{x['card']} is doing but quiet for {x['quiet'][:-4]}: check its session" for x in s["stalled"]]
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
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Where a Spec-Grill tasks.md stands.")
    parser.add_argument("tasks", nargs="?", help="tasks.md, its feature folder, or nothing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the picture as JSON")
    mode.add_argument("--watch", action="store_true", help="live view, redrawn on change")
    mode.add_argument("--wait", action="store_true", help="block until the state changes")
    mode.add_argument("--serve", action="store_true", help="serve the dashboard on localhost")
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
        serve(tasks, args.port, args.stale_hours, args.open)
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


def serve(tasks: str, port: int, stale_hours: float, open_browser: bool) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    specs = os.path.dirname(os.path.dirname(tasks))
    default = os.path.basename(os.path.dirname(tasks))
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

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
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return self.send(403, "local only", "text/plain")
            url = urlparse(self.path)
            query = parse_qs(url.query)
            name = query.get("f", [""])[0]
            if url.path == "/":
                self.send(200, read(page) or "dashboard.html is missing", "text/html; charset=utf-8")
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
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)


if __name__ == "__main__":
    main()
