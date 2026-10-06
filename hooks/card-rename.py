#!/usr/bin/env python3
"""Spec-Grill UserPromptSubmit hook: a card's "Start with" line renames the session first.

A card session starts with a line like

    Webhook retries · T003. Follow /abs/path/specs/004-webhook-retries/tasks.md §1, then card T003.

The hook finds that tasks.md (an absolute path, or a "…/" path resolved from the
session's directory upwards), reads the name pattern from its §1 ("rename the
session to `<NNN> T0nn <card title>`") and the card's title from the checklist,
and tells Claude that renaming the session is its first action. The supervisor's
line (card "SUP", tasks.md §6) renames the session to "<NNN> Supervisor". Any other
prompt passes through untouched. Standard library only.
"""

import json
import os
import re
import subprocess
import sys

START = re.compile(
    r"^\s*(?P<label>[^\n·]{1,80}?)\s*·\s*(?P<card>[A-Z]+[0-9]*[A-Z]*)\.\s*Follow\s+(?P<path>\S*tasks(?:\.draft)?\.md)"
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
    if os.path.isabs(path) and os.path.isfile(path):
        return path
    tail = re.sub(r"^(?:…|\.\.\.)/?", "", path)
    start = os.path.abspath(cwd or ".")
    for root in (start, main_checkout(start)):
        directory = root
        while directory:
            candidate = os.path.join(directory, tail)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    match = START.match(payload.get("prompt", ""))
    if not match:
        return
    card = match.group("card")
    tasks = find_tasks(match.group("path"), payload.get("cwd", ""))
    text = ""
    if tasks:
        try:
            with open(tasks, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            text = ""
    title_match = re.search(
        rf"^- \[[ xX]\] {re.escape(card)}(?: \[P\])? (.+?)(?: — fulfills .*)?$", text, re.M
    )
    title = title_match.group(1).strip() if title_match else ""
    flat = re.sub(r"\s+", " ", text)
    pattern_match = re.search(r"rename the session to `([^`]+)`", flat)
    if pattern_match:
        number = re.match(r"(\d+)-", os.path.basename(os.path.dirname(tasks or "")))
        name = (
            pattern_match.group(1)
            .replace("<NNN>", number.group(1) if number else "")
            .replace("<card title>", title)
            .replace("T0nn", card)
        )
    else:
        name = f"{match.group('label').strip()} · {card} {title}"
    if card == "SUP":  # the supervisor session (tasks.md §6), not a card
        number = re.match(r"(\d+)-", os.path.basename(os.path.dirname(tasks or "")))
        name = f"{number.group(1)} Supervisor" if number else f"{match.group('label').strip()} · Supervisor"
    name = re.sub(r"\s+", " ", name).strip()
    context = (
        f'MANDATORY FIRST ACTION, before reading any file: rename this session to "{name}". '
        'Desktop: mcp__ccd_session_mgmt__set_session_title with session_id "self" (load it with '
        'ToolSearch "select:mcp__ccd_session_mgmt__set_session_title" if it is deferred). '
        f"CLI: /rename. This is {'§6' if card == 'SUP' else 'rule 1 of the session protocol'} "
        "of tasks.md; do not skip it and do not choose another title."
    )
    print(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
        )
    )


if __name__ == "__main__":
    main()
