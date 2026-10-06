# Spec-Grill

A Claude Code skill that refuses to let you start coding until your idea survives an interrogation.

You bring a vague feature idea. Spec-Grill interviews you — one question at a time, each with a recommended answer — until it can produce a `constitution.md`, `spec.md`, `plan.md`, and `tasks.md` that actually say something. Nothing gets written to disk until you approve it.

It's Spec-Driven Development (SDD) phase structure married to a decision-tree interview method. The name is the promise: you get grilled.

---

## Why

Most "spec" workflows fail the same way. The agent asks you three vague questions, fills the rest with plausible assumptions, and hands you a document that reads well and commits to nothing. Then you implement it, discover the assumptions were wrong, and rewrite everything.

Spec-Grill attacks that with three constraints:

1. **One question at a time.** No question bundles you can only half-answer.
2. **Every question arrives with a recommendation.** You're reviewing a proposal, not staring at a blank prompt. Saying "yeah, that one" is a legitimate and fast path through the whole interview.
3. **Facts get looked up, not asked.** If it's discoverable from the filesystem, an existing file, or earlier in the conversation, the skill finds it. Your attention is spent only on decisions that are genuinely yours.

And one escape hatch that matters more than it sounds: **"I don't know" is always a valid answer.** It gets logged verbatim into Open Questions and the interview moves on. You are never stuck.

---

## Install

### With the `skills` CLI (recommended)

```bash
npx skills add behkha/spec-grill
```

That's it. The CLI detects which agents you have installed and writes to the right path for each.

Install globally, so it's available in every project:

```bash
npx skills add behkha/spec-grill -g
```

Target a specific agent and skip the prompts:

```bash
npx skills add behkha/spec-grill -a claude-code -y
```

Useful follow-ups — `npx skills list` to see what's installed, `npx skills update spec-grill` to pull the latest, `npx skills remove spec-grill` to uninstall. The CLI also accepts full URLs (`https://github.com/behkha/spec-grill`), GitLab and generic git remotes, and local paths (`./spec-grill`) if you're hacking on a fork.

### Manual

A skill is just a directory containing a `SKILL.md`. Clone it wherever Claude Code looks:

**Personal** — every project:

```bash
git clone https://github.com/behkha/spec-grill.git ~/.claude/skills/spec-grill
```

**Project** — shared with everyone in the repo, checked into version control:

```bash
git clone https://github.com/behkha/spec-grill.git .claude/skills/spec-grill
```

Copying `SKILL.md` by hand works too; copy the `hooks/` folder beside it if you want the card-rename hook and the supervisor.

### Verify

```
/skills
```

`spec-grill` should be listed. If it isn't:

- Confirm the file landed at `<skills-dir>/spec-grill/SKILL.md` and its YAML frontmatter is intact.
- Restart your session — skills are picked up at startup.
- If you installed with `npx skills` and nothing appears, check where it wrote. The CLI can install to `~/.agents/skills/`, while Claude Code reads `~/.claude/skills/` — passing `-a claude-code` pins it to the path Claude Code actually reads.

---

## Use

Spec-Grill is **opt-in and heavyweight**. It will not fire on an ordinary feature request or bug fix — that's deliberate, and it's enforced in the skill description. You have to ask for it:

```
spec this out: a webhook retry system with exponential backoff
```

```
/spec-grill
```

```
grill me on the notifications redesign
```

```
amend the constitution — we're dropping the no-dependencies rule
```

Then answer questions until you have artifacts.

Once the cards are running, the same skill tracks them for you:

```
supervise the webhook retries feature — what's done and what's next?
```

---

## How it runs

Two modes, chosen per phase by whether interviewing actually earns its cost.

**Grilled phases** — constitution and spec. Subjective, high-stakes, expensive to get wrong by assumption. These get the full one-question-at-a-time treatment.

**Drafted phases** — plan and tasks. Mechanical derivations from an already-approved artifact. Question-by-question interviewing here is exhausting and low-value, so the skill drafts the whole thing and hands it to you for holistic review.

Every phase ends at an explicit approval gate.

### Phase 0 — Constitution check

Looks for `specs/constitution.md`.

Missing? The Constitution Grill runs first. A feature spec with no stated principles has nothing to be checked against.

Present? It gets read, and the feature spec is continuously checked against its Constraints and Quality Standards. On a detected conflict the skill **stops and flags it**, then grills on whether to amend the constitution — it won't quietly proceed past a contradiction, and it won't nag about amendment when nothing conflicts.

### Phase 1 — Constitution Grill

Grilled toward four sections: **Core Principles**, **Constraints**, **Quality Standards**, **Governance**.

Governance goes last, because you can't sensibly design an amendment process before you know what's being governed.

Written once per project and reused across every feature.

### Phase 2 — Spec Grill

Grilled toward: **User Scenarios**, **Functional Requirements**, **Non-Functional Requirements**, **Success Criteria**, **Out of Scope**, **Open Questions**.

Walked in dependency order — User Scenarios first because everything downstream derives from who and why; Success Criteria after Requirements so they can map back to them; Out of Scope last, once there's enough context to know what you're excluding. Open Questions accumulate as you go rather than being asked about.

`Out of Scope` is doing real work here. It's the section that stops scope creep three weeks into implementation.

### Phase 3 — Plan Draft

Not grilled. Drafted whole from the approved spec: **Technical Context**, **Architecture / Approach**, **Data Model**, **Contracts / Interfaces**, **Research**, **Quickstart / Testing Approach**.

Presented complete. Revised and re-presented until you approve.

### Phase 4 — Tasks Draft

Not grilled. `tasks.md` is written as the **entry point of every implementation session**: a list of self-contained cards, each sized for one fresh session that reads only the file's shared sections and its own card.

The file has six fixed sections:

1. **Session protocol** — one card per session, renamed to the card it completes; what to read and in what order; finding code by symbol, not line number; preconditions; staying in scope; the context budget; which steps need the owner's yes; how to finish (verify, commit, hand-off note, tick, stop).
2. **Templates** — the hand-off note each card leaves behind and the `RESUME.md` state file (status table, decisions waiting on the owner, locks, blockers).
3. **Traceability** — every requirement to its cards, every success criterion to the card that measures it, every invariant to the test that pins it. Gaps get flagged.
4. **Cards** — grouped into stages, each stage closed by a checkpoint card that merges, runs the checks and reviews the stage's diff. Every card carries *fulfills · after · size · effort*, a paste-ready **Start with** line, **Read**, **Do**, **Done when**, **Verify**, **Hand-off extras**, **Touches** and **Never**. Cards marked `[P]` may run side by side in their own worktree.
5. **Backlog** — cards added during execution (review findings, work split off when a session ran out of room).
6. **Supervisor** — the brief for the one long-lived session that tracks the feature for you (see below).

### Phase 5 — Handoff

Spec-Grill checks that the project registers its **card-rename hook** (`hooks/card-rename.py`) and, with your yes, adds it to `.claude/settings.local.json`. The hook turns rule 1 of the session protocol into something Claude can't forget: when you paste a card's **Start with** line, it reads the name pattern and the card's title from `tasks.md` and makes renaming the session the first action (for example `004 T003 Backoff schedule`). Other prompts pass through untouched.

Then it stops. Spec-Grill's job ends at approved artifacts; implementation is a separate concern. It tells you how to run the cards (one per fresh session, starting with T001's **Start with** line) and how to start the supervisor, and offers to do either, but won't start unprompted.

---

## The supervisor

Twenty cards across five sessions is a lot to hold in your head. The supervisor holds it for you: one extra session, started once per feature, that tells you what finished, who is waiting for whom, and what to paste next.

Start it by pasting §6's line into a new session:

```
Webhook retries · SUP. Follow /abs/path/specs/004-webhook-retries/tasks.md §6.
```

The session renames itself `004 Supervisor`, reports, and arms a watcher. From then on every change in the feature's files (a card ticked, a hand-off written, a decision answered, a new blocker) wakes it, and it tells you what changed and what is now ready, with the **Start with** lines to paste. Ask it anything in between: *what's next?*, *what is T007 waiting for?*, *what did T004 decide?*

It never edits code or the state files and never starts a card unless you ask. It answers from the files every time, not from memory, so if you close it, a new one picks up exactly where the old one was.

The bookkeeping is a plain script, `hooks/supervisor.py` (standard library only), which you can also run yourself:

```bash
python3 ~/.claude/skills/spec-grill/hooks/supervisor.py specs/004-webhook-retries
```

```
Webhook retries · 2026-10-06 13:18Z
[█████░░░░░░░░░░░░░░░] 25% · 2/8 cards finished
Done: T001, CP0

Running now:
  T002 `webhook_deliveries` migration (feat/x, since 2026-10-06)

Ready to run next:
  T003 [P] Backoff schedule · S · effort medium
    Start with: Webhook retries · T003. Follow /abs/path/…/tasks.md §1, then card T003.

Waiting:
  T004 Worker wiring waits for T002 (doing); T003 (todo)
  T005 Rollout waits for CPA (todo); you: decision 2: Alert channel?

Needs you:
  decision 2: Alert channel? (needed before T005)
```

### The dashboard

To see it all at a glance, serve the dashboard:

```bash
python3 ~/.claude/skills/spec-grill/hooks/supervisor.py specs/004-webhook-retries --serve --open
```

It opens `http://127.0.0.1:8765` (local only, no dependencies) and refreshes every 3 seconds:

- **Progress** — one bar split by status, with counts.
- **Run next** — every ready card with its effort tier and a copy button for its **Start with** line, plus which ones may run side by side.
- **Needs you** — open decisions, blockers, stalled cards, a held deploy lock.
- **Board** — running, ready, waiting, blocked and done columns.
- **Who waits for whom** — the dependency map; click a card to trace what it waits for and what waits for it, and open its details (RESUME row, hand-off note).
- **Activity** and **Drift** — what changed since you opened the page, and where the files disagree.

A picker switches between every feature in `specs/`. The supervisor session offers to start it for you; just say "show the dashboard."

Add `--watch` instead for a live view in a terminal that redraws whenever the state changes; leave it open in a terminal tab and it costs nothing. `--wait` blocks until something changes and prints what did (that's what wakes the supervisor session); `--json` is for your own scripts. It also flags stalled cards (doing, but no commit or hand-off for 4 hours) and drift (a card ticked in `tasks.md` but not done in `RESUME.md`, done with no hand-off, and so on).

---

## What you get

```
specs/
├── constitution.md              # project-wide, created once, reused across features
└── 001-feature-slug/
    ├── spec.md
    ├── plan.md
    ├── tasks.md
    └── state/                   # RESUME.md + handoff/ notes, written during implementation;
                                 # .supervisor.json, the supervisor's last-seen snapshot
```

Feature folders are numbered and slugged automatically (`002-payment-retries`, `003-sso-login`). The skill checks `specs/` for the next available number. That's a derivation, not a decision — you won't be asked about it.

### tasks.md, in practice

The checklist in §4:

```markdown
- [x] T001 Re-verify, measure; worktree and RESUME
- [x] CP0 The owner's open questions
- [ ] T002 `webhook_deliveries` migration — fulfills FR-1
- [ ] T003 [P] Backoff schedule — fulfills FR-2, FR-3
- [ ] CPA Storage and schedule merged and pinned
```

And one card:

```markdown
#### T003 [P] — Backoff schedule
fulfills FR-2, FR-3 · after: CP0 (beside T002) · S · effort medium

**Start with:** `Webhook retries · T003. Follow /abs/path/specs/004-webhook-retries/tasks.md §1, then card T003.`
**Read:** spec FR-2, FR-3; plan "Architecture" (the scheduler paragraph). Code by symbol: `DeliveryWorker.run` (`worker/delivery.py`).
**Do:** `next_attempt_at(attempt)` with exponential backoff and jitter, capped at the spec's limit.
**Done when:** the cap and the jitter bounds have tests that fail before and pass after.
**Verify:** `pytest tests/test_backoff.py`.
**Touches:** `worker/backoff.py`, `tests/test_backoff.py`. **Never:** change `DeliveryWorker`.
```

Full templates for all four artifacts live at the bottom of `SKILL.md`.

---

## The rules it won't break

Lifted from the skill, because they're the actual product:

1. One question at a time. Never bundled.
2. Always propose a recommended answer with brief reasoning.
3. Wait for a real response. Never act on an assumed answer.
4. Look up facts, don't ask for them.
5. "I don't know" is valid — logged to Open Questions, interview continues.
6. Walk the decision tree in dependency order. Don't ask about API contract shape before confirming there's an API.
7. Nothing is final until explicitly confirmed.

---

## When not to use this

Bug fixes. One-line changes. Anything where the spec would be longer than the diff. This is a heavyweight process and it's honest about that — reach for it when the cost of building the wrong thing exceeds the cost of an interview.

---

## Customizing

`SKILL.md` is a single readable Markdown file with YAML frontmatter. Everything is editable prose:

- Change which sections each phase grills toward.
- Move a phase between grilled and drafted.
- Adjust the `description` frontmatter to change when Claude reaches for the skill.
- Swap the templates for your team's house format.

Keep the `name` and `description` fields — that's what registers the skill and drives invocation, and the `skills` CLI requires both to be present in the frontmatter.

If you fork this, `SKILL.md` stays at the repo root. That's one of the locations the `skills` CLI scans, which is what makes `npx skills add behkha/spec-grill` work with no extra config.

---

## Credits

Artifact structure follows the [spec-kit](https://github.com/github/spec-kit) SDD convention. The grilling method — one question, one recommendation, no assumptions — is what this skill adds on top.
