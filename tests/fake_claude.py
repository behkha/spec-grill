#!/usr/bin/env python3
"""A stand-in for `claude -p` in the autopilot tests: it acts out one scripted card session.

FAKE_SCRIPT (a JSON file) maps a card to the behaviour of each of its sessions, in order:
"done" finishes the card the way §1 item 9 says (RESUME row done, ticked, hand-off written),
"doing" marks it doing and stops, "work" acts out a realistic session (to-dos, tools, pauses) and finishes, "decision" asks the owner a question and waits, "approval" (or "approval#A1=<step>") asks for the owner's yes and stops, "blocked"
records a blocker, "apierror" fails the way an expired login does, "budget" hits the dollar
cap, "sleep" stays alive until killed. Every call is appended to FAKE_CALLS as one JSON line.
"""

import json
import os
import re
import sys
import time

args = sys.argv[1:]
card = os.environ["SPEC_GRILL_CARD"]
tasks = os.environ["FAKE_TASKS"]
resume = os.path.join(os.path.dirname(tasks), "state", "RESUME.md")
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]

with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"card": card, "args": args, "cwd": os.getcwd()}) + "\n")
with open(os.environ["FAKE_SCRIPT"], encoding="utf-8") as handle:
    script = json.load(handle)
counter = os.environ["FAKE_SCRIPT"] + ".count"
counts = json.load(open(counter)) if os.path.exists(counter) else {}
n = counts.get(card, 0)
counts[card] = n + 1
json.dump(counts, open(counter, "w"))
steps = script.get(card, ["done"])
behaviour = steps[min(n, len(steps) - 1)]


def emit(event: dict) -> None:
    print(json.dumps({**event, "session_id": session}), flush=True)


def set_status(status: str) -> None:
    text = open(resume).read()
    text = re.sub(rf"^\| {card} \|([^|]*)\| [a-z]+ \|", rf"| {card} |\1| {status} |", text, flags=re.M)
    open(resume, "w").write(text)


emit({"type": "system", "subtype": "init"})
if behaviour == "apierror":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Failed"}]}, "is_api_error_message": True})
    emit({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
          "result": "Failed to authenticate: OAuth session expired", "total_cost_usd": 0})
    sys.exit(1)

if behaviour == "work":  # a realistic session: a to-do list, tool calls with results, usage, pauses
    pause = float(os.environ.get("FAKE_STEP", "1.5"))
    steps = [("Read the card and RESUME", "Reading the card and RESUME", "Read", {"file_path": tasks}),
             ("Write the failing test", "Writing the failing test", "Edit", {"file_path": "tests/test_login.py"}),
             ("Make the test pass", "Making the test pass", "Bash", {"command": "pytest tests/test_login.py -q", "description": "Run the login tests"}),
             ("Save screenshots", "Saving screenshots", "Bash", {"command": "pnpm shots login", "description": "Screenshot the login screen"})]
    tokens = 0
    for i, (todo, doing, tool, arg) in enumerate(steps):
        todos = [{"content": t, "activeForm": a, "status": "completed" if j < i else "in_progress" if j == i else "pending"}
                 for j, (t, a, _, _) in enumerate(steps)]
        tokens += 1800
        emit({"type": "assistant", "message": {"usage": {"input_tokens": 1200 + tokens, "cache_read_input_tokens": 18000 + 4 * tokens, "output_tokens": 420},
              "content": [{"type": "text", "text": f"Next: {doing.lower()}."},
                          {"type": "tool_use", "id": f"todo{i}", "name": "TodoWrite", "input": {"todos": todos}}]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"todo{i}", "content": "Todos updated"}]}})
        emit({"type": "assistant", "message": {"usage": {"input_tokens": 1300 + tokens, "cache_read_input_tokens": 18500 + 4 * tokens, "output_tokens": 260},
              "content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": arg}]}})
        time.sleep(pause)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"ok: {tool} finished ({i + 1}/{len(steps)})"}]}})
    behaviour = "done"

emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
if behaviour == "sleep":
    time.sleep(600)
if behaviour == "done":
    set_status("done")
    text = open(tasks).read()
    open(tasks, "w").write(re.sub(rf"^- \[ \] {card}\b", f"- [x] {card}", text, flags=re.M))
    os.makedirs(os.path.join(os.path.dirname(resume), "handoff"), exist_ok=True)
    open(os.path.join(os.path.dirname(resume), "handoff", f"{card}.md"), "w").write(f"# {card}\n")
    if os.environ.get("FAKE_COMMIT"):  # commit the way a card session does at §1 item 9
        import subprocess
        who = {"GIT_AUTHOR_NAME": "card", "GIT_AUTHOR_EMAIL": "card@example.invalid",
               "GIT_COMMITTER_NAME": "card", "GIT_COMMITTER_EMAIL": "card@example.invalid"}
        subprocess.run(["git", "commit", "--allow-empty", "-qm", f"feat: finish the card's work ({card})"],
                       env={**os.environ, **who}, capture_output=True)
    if "answered approval" in args[-1] and "approved" in args[-1]:
        text = open(resume).read()
        open(resume, "w").write(re.sub(r"\| approved \|", "| done |", text))
elif behaviour == "doing":
    set_status("doing")
elif behaviour.startswith("approval"):  # "approval", "approval=<step>", "approval#A1=<step>"
    found = re.match(r"approval(?:#(A\d+))?(?:=(.*))?$", behaviour)
    set_status("doing")
    text = open(resume).read()
    count = len(re.findall(r"^\| A\d+ \|", text, re.M))
    number = found.group(1) or f"A{count + 1}"
    step = (found.group(2) or "deploy to staging").replace("|", "\\|")
    row = f"| {number} | {card} | {step} | the card's verify step | pending | |\n"
    text = re.sub(r"(## Approvals\n(?:.*\n)*?\| --- .*\n)", lambda m: m.group(1) + row, text)
    open(resume, "w").write(text)
elif behaviour == "decision":
    set_status("doing")
    text = open(resume).read()
    text = text.replace("| 1 | Which payment provider? | Stripe | T006 | | |",
                        f"| 1 | Which payment provider? | Stripe | T006 | | |\n| 2 | Which layout? | B | {card} | | |")
    open(resume, "w").write(text)
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "AUTOPILOT: WAITING FOR DECISION 2", "total_cost_usd": 0.1})
    sys.exit(0)
elif behaviour == "blocked":
    set_status("blocked")
    text = open(resume).read()
    open(resume, "w").write(text.replace("## Blockers\n", f"## Blockers\n- {card}: needs a key\n"))
if behaviour == "budget":
    emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
          "terminal_reason": "max_budget", "result": "Reached the budget", "total_cost_usd": 20})
    sys.exit(1)
emit({"type": "result", "subtype": "success", "is_error": False, "result": f"AUTOPILOT: {behaviour.upper()}",
      "total_cost_usd": 0.25})
