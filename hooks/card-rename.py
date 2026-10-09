#!/usr/bin/env python3
"""Spec-Grill UserPromptSubmit hook: a card's "Start with" line renames the session first.

A card session starts with a line like

    Webhook retries · T003. Follow /abs/path/specs/004-webhook-retries/tasks.md §1, then card T003.

The hook finds that tasks.md (an absolute path, or a "…/" path resolved from the
session's directory upwards), reads the name pattern from its §1 ("rename the
session to `<NNN> T0nn <card title>`") and the card's title from the checklist,
and tells Claude that renaming the session is its first action. The supervisor's
line (card "SUP", tasks.md §6) renames the session to "<NNN> Supervisor". Any other
prompt passes through untouched, and odd input never fails the prompt. Standard
library only, and Python 3.9 too (macOS's /usr/bin/python3).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# On the prompt's first non-blank line: the label (the feature's name, which may hold a "·" itself), the card
# after the "·" just before ". Follow" (T003, T012A, T042B2, T042R2A, CPA, CPEND, a batch's B2, SUP),
# and the tasks.md path: an absolute, "~/" or "…/" one may hold spaces, any other is one word, so
# "Follow up on what tasks.md says" is not a path. The card is one flat [A-Z][A-Z0-9]* run, so a long
# uppercase run after a "·" can't make the match backtrack for ever.
START = re.compile(
    r"^\s*(?P<label>[^\n]{1,200}?)[ \t]*·[ \t]*(?P<card>[A-Z][A-Z0-9]*)\.\s*Follow\s+"
    r"(?P<path>(?:[/~…]|\.\.\.)[^\n]*?tasks(?:\.draft)?\.md|\S*tasks(?:\.draft)?\.md)(?![\w/-]|\.\w)"
)


def main_checkout(cwd: str) -> str | None:
    """The main checkout of a git worktree: git-ignored spec folders live only there."""
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return None
    return os.path.dirname(common) if common else None


def find_tasks(path: str, cwd: str) -> str | None:
    """The tasks.md a start line names: its absolute path; else (a "…/" path, or an absolute path from
    another checkout cut to its specs/… part) looked for from the session's directory up to the root
    of its git repository, and from the main checkout of a git worktree."""
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        if os.path.isfile(path):
            return path
        stale = re.match(r".*/(specs/.+)$", path)
        if not stale:
            return None
        tail = stale.group(1)
    else:
        tail = re.sub(r"^(?:…|\.\.\.)/?", "", path)
    start = os.path.abspath(cwd or ".")
    for root in (start, None):
        directory = root or main_checkout(start)  # git only when the walk from cwd found nothing
        while directory:
            candidate = os.path.join(directory, tail)
            if os.path.isfile(candidate):
                return candidate
            if os.path.isdir(os.path.join(directory, ".git")):
                break  # the repository's root: above it lies another project's specs/ (a worktree's
                # or submodule's .git is a file, so the walk goes on to the repository around it)
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return None


def card_title(text: str, card: str) -> str:
    """The card's title: from its `###`/`####` heading, which beats the checklist line as in the
    supervisor (a backlog card's line may say `- [x] T010B done <date> (…)`); else from its checklist
    line, unless that is bare (`- [x] T010B`, `- [ ] B2 [P]`)."""
    found = re.search(rf"^#{{3,4}} {re.escape(card)}(?: \[P\])? [—–:-] (.+?)\s*$", text, re.M)
    if found:
        return found.group(1)
    for found in re.finditer(
        rf"^- \[[ xX]\] {re.escape(card)}(?: \[P\])?(?: (.+?))?(?: — fulfills .*)?\s*$", text, re.M
    ):
        title = re.sub(r"^\[P\]", "", found.group(1) or "").strip()
        if title and not re.match(r"done \d", title):
            return title
    return ""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if os.environ.get("SPEC_GRILL_AUTOPILOT"):
        return  # the autopilot names its sessions itself (claude -n)
    match = START.match(str(payload.get("prompt") or ""))
    if not match:
        return
    card = match.group("card")
    tasks = find_tasks(match.group("path").strip(), str(payload.get("cwd") or ""))
    text = ""
    if tasks:
        try:
            with open(tasks, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            text = ""
    title = card_title(text, card)
    flat = re.sub(r"\s+", " ", text)
    # the card pattern, as autopilot.session_name picks it (not §6's "<NNN> Supervisor")
    pattern = next((p for p in re.findall(r"rename the session to `([^`]+)`", flat) if "T0nn" in p), "")
    if pattern:
        number = re.match(r"(\d+)-", os.path.basename(os.path.dirname(tasks or "")))
        name = (
            pattern
            .replace("<NNN>", number.group(1) if number else "")
            .replace("<card title>", title)
            .replace("T0nn", card)
        )
    else:
        name = f"{match.group('label').strip()} · {card} {title}"
    if card == "SUP":  # the supervisor session (tasks.md §6), not a card
        number = re.match(r"(\d+)-", os.path.basename(os.path.dirname(tasks or "")))
        name = f"{number.group(1)} Supervisor" if number else f"{match.group('label').strip()} · Supervisor"
    name = re.sub(r"\s+", " ", name.replace("`", "")).strip()[:120]  # as autopilot.session_name does
    context = (
        f'MANDATORY FIRST ACTION, before reading any file: rename this session to "{name}". '
        'Desktop: mcp__ccd_session_mgmt__set_session_title with session_id "self" (load it with '
        'ToolSearch "select:mcp__ccd_session_mgmt__set_session_title" if it is deferred). '
        "CLI, where that tool is missing: you can't run slash commands, so begin your first reply by "
        f"asking the user to run `/rename {name}`, then go on. "
        f"This is {'§6' if card == 'SUP' else 'rule 1 of the session protocol'} "
        "of tasks.md; do not skip it and do not choose another title."
    )
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a failing hook shows the user an error on every prompt; this one passes the prompt through
