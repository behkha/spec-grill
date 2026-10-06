---
name: spec-grill
description: Turns a vague feature or project idea into a rigorous constitution.md, spec.md, plan.md, and tasks.md through relentless one-at-a-time interviewing ("grilling") before any code is written. Combines Spec-Driven Development (SDD) phase structure with a decision-tree interview method where every question comes with a recommended answer and nothing is written until the user confirms. Use this skill ONLY when the user explicitly asks to "spec this out," "grill me," invokes spec-driven development, or names this skill directly. Also use it when the user asks to supervise, track or report on the progress of an existing spec-grill tasks.md ("what's done," "what's next," "who is waiting for whom," "start the supervisor," "show the dashboard") — that runs the long-lived supervisor session, not the interview. Do NOT trigger proactively on ordinary feature requests, bug fixes, or small changes — this is an opt-in, heavyweight process.
---

# Spec-Grill

Spec-Grill turns an unspecified idea into a chain of approved artifacts — `constitution.md` → `spec.md` → `plan.md` → `tasks.md` — using two different modes depending on the phase:

- **Grilled phases** (constitution, spec): Claude interviews the user relentlessly, one question at a time, walking a decision tree, always proposing a recommended answer, and never proceeding until the user confirms. These phases are subjective/high-stakes (goals, constraints, requirements), so the cost of getting them wrong via assumption is high.
- **Drafted phases** (plan, tasks): Claude autonomously drafts the full artifact from the approved spec/plan, then presents it for holistic review and approval — not question-by-question. These phases are more mechanical derivations where interviewing would be exhausting and low-value.

Every phase ends with an explicit approval gate. Nothing is written to disk as final until the user approves it.

After the artifacts are approved, the **supervisor** (see "Supervisor mode") keeps track of the implementation for the user: one long-lived session that watches the feature's state files and reports what finished, who waits for whom, and what to run next.

## Directory layout

```
specs/
├── constitution.md              # project-wide, created once, reused across features
└── 001-feature-slug/
    ├── spec.md
    ├── plan.md
    ├── tasks.md
    └── state/                   # written during implementation, not by spec-grill
        ├── RESUME.md
        ├── handoff/T0nn.md
        └── .supervisor.json     # the supervisor's last-seen snapshot
```

Feature folder names are auto-derived: an incrementing zero-padded number + kebab-case slug of the feature name (e.g. `specs/002-payment-retries/`). This is a lookup/derivation, not a decision — don't ask the user for it. Check the `specs/` directory for the next available number before creating.

## The grilling method (applies to constitution + spec phases)

Non-negotiable rules for grilled phases:

1. **One question at a time.** Never bundle multiple questions in one message.
2. **Always propose a recommended answer** with brief reasoning, then let the user pick, override, or answer freehand.
3. **Wait for the user's response before continuing.** Do not act on an assumed answer.
4. **Look up facts, don't ask for them.** If something is discoverable from the filesystem, an existing file, prior conversation, or a tool, look it up instead of interviewing about it. Only decisions genuinely belonging to the user go through the interview.
5. **"I don't know" is a valid answer.** Log it verbatim into the artifact's Open Questions section and move on — never block the interview waiting for certainty.
6. **Walk the decision tree in dependency order.** Some questions only make sense once an earlier one is answered (e.g. don't ask about API contract shape before the user has confirmed there's an API at all). Resolve prerequisites first.
7. **Nothing is final until explicitly confirmed.** At the end of the phase, show the complete drafted file and ask for explicit approval before writing it / advancing.

## Phase 0 — Constitution check

On any `spec-grill` invocation:

1. Check whether `specs/constitution.md` exists.
2. **If it doesn't exist**: run the Constitution Grill (below) before anything else — a feature spec built with no stated principles/constraints has nothing to be checked against.
3. **If it exists**: read it. As the feature spec later takes shape, watch for conflicts or gaps against the constitution's Constraints and Quality Standards. If one appears, stop, flag it explicitly to the user, and grill specifically on whether/how to amend the constitution before continuing the feature spec. Don't silently proceed past a detected conflict, and don't ask about amendment when nothing conflicts.
4. The user can also invoke constitution amendment directly (e.g. "amend the constitution," "update our project principles") without a feature in progress — same grill, just entered directly instead of via conflict detection.

## Phase 1 — Constitution Grill

Grill toward these fixed sections (spec-kit standard):

- **Core Principles** — the non-negotiable values/priorities for the project (e.g. "correctness over speed," "no external dependencies without approval")
- **Constraints** — tech stack, language/framework choices, forbidden dependencies, platform targets
- **Quality Standards** — testing bar, code review requirements, performance/security baselines
- **Governance** — how the constitution itself may be amended in future (who approves, what triggers a review)

Walk these in order — Governance depends on knowing what's actually being governed, so grill it last. For each section, ask targeted one-at-a-time questions with a recommended answer (e.g. "Should the constitution require test coverage before merge? Recommendation: yes, given most projects invoking this skill care about correctness — but confirm."). Stop and present the full draft `constitution.md` for approval before writing it.

## Phase 2 — Spec Grill

Grill toward these fixed sections (spec-kit standard):

- **User Scenarios / User Stories** — who's using this and what are they trying to accomplish
- **Functional Requirements** — what the system must do, numbered and testable
- **Non-Functional Requirements** — performance, security, scalability, accessibility, etc.
- **Success Criteria** — how you'll know this is done and working
- **Out of Scope** — what this explicitly does NOT cover (prevents scope creep mid-implementation)
- **Open Questions** — anything deferred, unresolved, or answered "I don't know" during grilling

Dependency order: User Scenarios first (everything else derives from who/why), then Functional Requirements, then Non-Functional Requirements, then Success Criteria (which should map back to the requirements), then Out of Scope (informed by everything above), with Open Questions accumulated throughout rather than asked about directly.

Check the spec against `constitution.md` as sections firm up (see Phase 0 step 3). Present the full draft `spec.md` for approval before writing it.

## Phase 3 — Plan Draft (not grilled)

Once `spec.md` is approved, autonomously draft `plan.md` covering:

- **Technical Context** — language, framework, key dependencies (checked against constitution constraints)
- **Architecture / Approach** — how the system will be structured to satisfy the spec
- **Data Model** — entities, relationships, storage shape
- **Contracts / Interfaces** — API shapes, function signatures, message formats
- **Research** — notes on unknowns investigated and resolved while drafting
- **Quickstart / Testing Approach** — how someone would run this and how it'll be verified

Draft the whole thing before showing it — don't interview section by section. Present the complete draft and ask for holistic approval or revision. Revise and re-present until approved, then write `plan.md`.

## Phase 4 — Tasks Draft (not grilled)

Once `plan.md` is approved, autonomously draft `tasks.md`. It is not a to-do list: it is the **entry point of every implementation session**. Each task is a self-contained **card** that one fresh session (new chat or `/clear`) can carry out after reading only the file's shared sections (§1–§3) and its own card. Nothing passes between sessions through chat memory; state moves only through files — a `RESUME.md` state file and one hand-off note per finished card.

### Look up before drafting (don't ask)

- From `spec.md`: requirement ids (FR-n, NFR-n — use the spec's own ids), success criteria, Open Questions. From `plan.md`: the sections each card will cite. From `constitution.md`: quality standards (they become each card's Verify commands and the checkpoint review) and constraints (they become protocol rules and card **Never** lines).
- From the repo: test, lint and type-check commands; the directories code lives in (to define short path prefixes); git-ignored folders; worktree and branch conventions; deploy commands; whether a state folder already exists.
- From the environment: the reasoning-effort tiers or session commands available, so each card can name its effort; the directory this skill is installed in, so §6 can name `hooks/supervisor.py` by absolute path.

### Cutting the cards

1. **One card = one session.** Size each card S, M or L against one session's context budget; split every L. Also split where the work waits on someone else — an owner's decision, a paid step, an external party, a deploy — as a suffixed card (`T012A`), so no session idles while it waits.
2. **Stages.** Group cards into numbered stages in dependency order (typical: re-verify and baseline; pure contracts; storage; services; API; UI; docs; rollout; close). End every build stage with a **checkpoint card** (`CPA`, `CPB`, …) that merges the stage, runs the full checks and pinning tests, reviews the stage's diff at high effort, and turns each confirmed finding into a §5 backlog card.
3. **First card re-verifies.** The plan was written against older code: card T001 locates every fact the plan relies on *by symbol* on today's code, writes a code map, measures baselines (every number with the command that produced it), creates the state file and any worktree. When the spec has Open Questions that block cards, add `CP0` — one plain-words page for the owner, each question with a recommended default and the card that waits for it.
4. **Last cards close.** A close card records the results against the success criteria (a report, if the project keeps one), then `CPEND` checks every card is ticked or waived.
5. **`[P]` only when the cards' Touches lists don't overlap.** Each `[P]` card names whom it may run beside; parallel cards work in their own worktree and branch and merge back at the end of the card.
6. **Code by symbol, never by line number.** Lines move between the plan and the session.
7. **Every card names its effort tier** (and, when the environment has one, the command that sets it).
8. **Owner actions.** Any step the constitution or spec reserves for the owner (deploys, production writes, paid runs, new dependencies, outward messages) is a protocol rule: the session asks in that session, then runs it after a yes — or hands over the exact commands when it can't.
9. **Diverging from the plan.** When a card splits, merges or reorders plan items, list each change and its reason in the header's "Where the cards differ from plan.md".

### Checks before presenting

- Every FR/NFR maps to at least one card (§3 table); every success criterion names the card that measures it; every Open Question is a decisions row with the card it must be answered before; every rule that needs a pinning test names the test file and the card that writes it. Flag any gap to the user explicitly.
- Every card has: Start with, Read, Do, Done when, Verify, Touches. No card says "see above" — a card must stand alone.
- Every card's meta line has an `after:` field naming the cards it waits for by id (ranges like `T002–T005` are fine; notes go in parentheses, such as `(beside T002)`), because the supervisor builds the waiting graph from it. Run `hooks/supervisor.py` on the draft and check its "Waiting" and "Ready" lists match the stage outline.

### Presenting

Present the complete draft for holistic approval, revise as needed, then write `tasks.md`. When the file is too long to read in chat, write it as `tasks.draft.md` and present §1–§3, the checklist, the stage outline and three full cards (T001, one checkpoint, the hardest card); rename it to `tasks.md` on approval.

## Phase 5 — Handoff

Once `tasks.md` is approved, make sure the project registers the **card-rename hook**, then stop.

**The card-rename hook.** Rule 1 of the session protocol (rename the session to its card) is easy to forget, so a hook enforces it. `hooks/card-rename.py` (next to this file; standard library only) runs on every prompt; when the prompt is a card's **Start with** line, it finds that `tasks.md` (absolute path, or a `…/` path resolved from the session's directory and from the main checkout of a git worktree), reads the name pattern from §1 and the card's title from the checklist, and tells Claude to rename the session before anything else. Every other prompt passes through. Check the project's `.claude/settings.local.json` (git-ignored; `.claude/settings.json` if the team shares the hook) for a `UserPromptSubmit` entry whose command runs `card-rename.py`. If none is there, ask the user, and after a yes merge this entry into the existing hooks (never replace the file):

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/spec-grill/hooks/card-rename.py", "timeout": 10}]}]}}
```

Test it before saying it works: pipe `{"cwd": "<project>", "prompt": "<T001's Start with line>"}` into the command and check the name it prints.

Then Spec-grill's job is done — implementation is a separate concern. Tell the user how to run it: one card per fresh session, starting by pasting T001's **Start with** line. Tell them about the supervisor too: one extra session, started once by pasting §6's **Start with** line, that stays with them for the whole feature. Offer to run T001 or start the supervisor if the user asks, but don't start either unprompted.

## Supervisor mode

The user loses track of a long card list: which cards finished, which wait on what, what to paste next. The supervisor is their assistant for one feature: a single long-lived session that never does card work and never loses the thread, because it rebuilds its picture from the files every time instead of remembering it.

It has two halves:

- **`hooks/supervisor.py`** (standard library only) — the deterministic half. It reads `tasks.md` (checklist, each card's `after:` and `blocks:` fields), `state/RESUME.md` (status, decisions, blockers, deploy lock), `state/handoff/*.md` and git, and reports: progress bar, done, running now, ready to run next (with each card's **Start with** line and what finishing it unblocks), who waits for whom (a card, the integration worktree, an owner's decision, a blocker), what needs the owner, stalled cards (doing with no commit or hand-off for 4 h), and drift (files that disagree: ticked but not done, done without a hand-off, a backlog card missing from RESUME). `--serve` runs the **dashboard** (`hooks/dashboard.html`) on `http://127.0.0.1:8765`, local only: progress bar, a "Run next" panel with copy buttons for each ready card's **Start with** line, a "Needs you" panel, a board (running, ready, waiting, blocked, done), a dependency map where clicking a card traces what it waits for and what waits for it, a card drawer with its RESUME row and hand-off note, an activity feed and the drift list. It refreshes every 3 s and switches between every feature in `specs/`. `--watch` redraws a live view in a terminal; `--wait` blocks until the state changes, prints what changed and the report, and exits; `--json` is for scripts. It writes only `state/.supervisor.json`.
- **The supervisor session** — the conversational half. It follows tasks.md §6: it stays armed with `--wait` as a background command, so every change (a card finished, a decision answered, a new blocker) wakes it to report, and it answers the user's questions at any time from fresh output and the hand-off notes.

When the user asks only to see the dashboard, start `--serve` in the background, open its URL and stop there; the full supervisor session is not needed for that.

**Entering supervisor mode** (the user asks to supervise, track or report on a feature, or pastes §6's **Start with** line): find the feature's `tasks.md` (the one the user names; otherwise the newest `specs/NNN-*/tasks.md`, asking only when several are in progress). If it has a §6, follow it. If it predates §6, follow the §6 template below with its paths filled in, and offer to add §6 to the file. Supervisor mode never interviews and never edits the artifacts.

"Never sleeps" has two layers and the user should know which is which: `supervisor.py --watch` in a terminal tab costs nothing and runs until the tab closes; the supervisor session sleeps between events and is woken by each `--wait` exit, so it spends tokens only when something changed. It lasts as long as the session stays open (on desktop, the app's keep-awake setting stops the machine sleeping under it). If it is closed, a new session started from §6's line picks up from the files with nothing lost.

## Templates

### constitution.md
```markdown
# Constitution

## Core Principles
- ...

## Constraints
- ...

## Quality Standards
- ...

## Governance
- ...
```

### spec.md
```markdown
# Spec: <feature name>

## User Scenarios
- ...

## Functional Requirements
1. ...

## Non-Functional Requirements
- ...

## Success Criteria
- ...

## Out of Scope
- ...

## Open Questions
- ...
```

### plan.md
```markdown
# Plan: <feature name>

## Technical Context
...

## Architecture / Approach
...

## Data Model
...

## Contracts / Interfaces
...

## Research
...

## Quickstart / Testing Approach
...
```

### tasks.md

The section numbers (§1–§6) and card fields are fixed; fill the `<…>` parts from the repo and the approved artifacts. `[P]` = may run at the same time as the cards named in its card, in another session and another worktree. Sizes: S, M, L (split every L). Ids: `T0nn` planned cards, `T0nnA` planned splits, `T0nnB`… backlog cards, `CP0`/`CPA`…/`CPEND` checkpoints.

````markdown
# Tasks: <feature name>

Design: [spec.md](spec.md) (requirements FR-1–FR-n, NFR-1–NFR-n, success criteria SC-1–SC-n).
Rationale and acceptance detail: [plan.md](plan.md). This file is the **entry point of every working
session**: one card, one fresh session. A session reads §1–§3 and its own card, nothing else unless the
card lists it. To see where the feature stands, ask the supervisor (§6) instead of reading this file.

Format: `- [ ] T0nn [P] title — fulfills FR-…`. Every requirement maps to at least one card (§3).

**Where the cards differ from plan.md:**
1. <split / merge / reorder, and why>

---

## 1. Session protocol (every card follows it)

1. **One card per session.** Open a fresh session for each card. Never continue into the next card in
   the same session, even if there is room left. First action: rename the session to `<NNN> T0nn <card title>` (desktop: `set_session_title`; CLI: `/rename`).
2. **Where things live.** State: `specs/NNN-slug/state/RESUME.md` and `state/handoff/T0nn.md`
   <git-ignored folders are read and written by absolute path from worktrees>. Code work happens in
   <the integration worktree `<path>`, branch `<branch>`, created by T001>; a `[P]` card works in
   `<path>-t0nn`, branch `<branch>-t0nn-<slug>`, and merges back at the end of the card — only while no
   card without `[P]` is `doing` in RESUME; otherwise the branch waits for the next checkpoint.
3. **Start.** Read, in this order and only these:
   1. this file's §1–§3 and your card;
   2. RESUME (status, decisions, locks, blockers);
   3. the hand-off notes of the cards your card depends on;
   4. the spec and plan sections your card lists — sections, not whole files;
   5. the rows of the code map (written by T001) for the code your card names;
   6. code: find what the card names **by symbol** (`grep -n "def name"`), never by line number. Read
      the function, not the module.

   Path prefixes: <`core/` = `…`, `api/` = `…`>. In **Verify**, <`pytest` means `…`; test files live
   under `…`>.
4. **Preconditions.** Every dependency is `done` in RESUME and every precondition the card names holds
   (an owner's answer recorded in RESUME's decisions). If not, write it in RESUME's blockers (naming
   your card) and stop. If they hold, set your row in RESUME to `doing` with the date (UTC) and the
   branch before any other work, so the supervisor sees the card running.
5. **Stay in scope.** Do only your card. Work that belongs elsewhere becomes a new card in §5 with a
   one-line reason and a note in RESUME; do not do it.
6. **Context budget.** Keep raw logs, query results and big files out of the conversation (scratch
   files; read summaries). If the session gets heavy before the card is done, write the hand-off with
   what is finished and what is left, add the remainder as a new §5 card, and stop.
7. **Owner's yes.** <deploys, production writes, paid steps (check the remaining budget first), new
   dependencies, outward messages> need the owner's yes **in that session**. After the yes the session
   runs the step, or hands the exact commands to the owner when it can't. <Only the session holding
   RESUME's deploy lock deploys; take it before, release it after.>
8. **<Project rules from the constitution>** (data handling, secrets, words that must never appear in
   code, …).
9. **Finish.**
   1. Run the card's Verify and <the full check command>; everything green (or a waiver recorded).
   2. Commit on the card's branch (conventional message, nothing else in the commit); merge back if the
      card is `[P]` (item 2's rule).
   3. Write the hand-off note from §2's template.
   4. Tick the card here (`[x]`) and update its row in RESUME (status, branch, commit, date UTC).
   5. Stop. Tell the owner which cards are now unblocked.

## 2. Templates

**Hand-off note** — `state/handoff/T0nn.md` (≤ 60 lines):

```markdown
# T0nn — <title> (hand-off)
- When (UTC) / branch / commits:
- Built: files and symbols added or changed; switches added (default):
- Measured: numbers, the command that produced them, report paths:
- Decided: what, by whom (owner / this session):
- Deviations from the card or spec, and why:
- Left open / cards added to §5:
- The next card must know (≤ 10 lines):
```

**RESUME** — `state/RESUME.md`, created by T001 with this content and kept current by every card:

```markdown
# <feature> - resume

State file for the one-card-per-session protocol in <absolute path of tasks.md> (§1). Every session
reads this first and updates its own row at the end.

## Layout
(worktrees, branches; code map; baseline)

## Deploy lock
free

## Switch states
(feature flags and their values per environment, as last read)

## Decisions (spec Open Questions)
| # | question | needed before | answer | by, when |
| --- | --- | --- | --- | --- |

## Blockers
- <precondition> before <card>.

## Status
| card | title | status | branch | commit | date (UTC) |
| --- | --- | --- | --- | --- | --- |
(one row per card of §4: status todo | doing | done | blocked | waived)
```

## 3. Traceability (spec requirement → cards)

| id | requirement | cards |
| --- | --- | --- |
| FR-1 | <short restatement> | T002, T007 |
| NFR-1 | <short restatement> | T004, CPB |

Success criteria and the cards that measure them: <SC-1 by T0nn, …>. No requirement is without a card.

Rules pinned by a test (invariants from the spec or constitution that must never regress):

| rule | pinning test | card |
| --- | --- | --- |
| <rule> | `<test file>` | T0nn |

---

## 4. Cards

Checklist (ticked by the session that finishes the card):

- [ ] T001 Re-verify, measure; worktree and RESUME
- [ ] CP0 The owner's open questions
- [ ] T002 <title> — fulfills FR-1
- [ ] T003 [P] <title> — fulfills FR-2, NFR-1
- [ ] CPA <stage> merged and pinned
- [ ] …
- [ ] T0nn Close: results against the success criteria
- [ ] CPEND Feature done

**Who may run beside whom:** Stage 2: T002 ∥ T003; …

Each card: **fulfills · after · size · effort**, **Start with** (paste as the session's first message),
**Read**, **Do**, **Done when**, **Verify**, **Hand-off extras**, **Touches** (files, to see which cards
may run side by side), and **Never** where a card has its own.

**Checkpoint routine** (CPA, CPB, …; each checkpoint card adds its own items):
1. Merge each `[P]` branch still open into the integration branch.
2. Merge the main branch into the integration branch.
3. <the full check command>, plus what the card adds (integration tests, migrations up and down, …).
4. The stage's pinning tests pass; list them in RESUME.
5. Review the stage's diff since the previous checkpoint at high effort. Each confirmed finding becomes
   a §5 card; a finding against a pinned rule blocks the next stage until it is fixed.
6. RESUME: the checkpoint's commit, the pins, the findings.

### Stage 1 — Re-verify and baseline

#### T001 — Re-verify, measure; worktree and RESUME
fulfills <baselines> · after: <precondition> · M · effort high

**Start with:** `<Feature> · T001. Follow <absolute path>/tasks.md §1, then card T001.`
**Read:** <spec and plan sections; every code reference in the plan, as a list to locate>.
**Do:**
1. Create the integration worktree and branch; create RESUME from §2's template with a status row per
   card of §4.
2. Locate every code fact the plan relies on by symbol on today's code; write the code map (fact → file
   and symbol now; moved, renamed or gone). A missing contract blocks the feature: write it in RESUME and
   tell the owner.
3. Measure the baselines the success criteria compare against, read-only; every number with its
   command.
**Done when:** code map, baseline and RESUME exist.
**Verify:** nothing written to <production> or to git; every number has its command next to it.
**Hand-off extras:** facts that change a later card's reading list.
**Touches:** state files, the new worktree. **Never:** commit; print secrets.

#### CP0 — The owner's open questions
after: T001 · S · effort low

**Start with:** `<Feature> · CP0. Follow <absolute path>/tasks.md §1, then card CP0.`
**Read:** spec Open Questions; hand-off T001.
**Do:** One page for the owner in plain words: each open question with its recommended default and the
card that waits for it. Record each answer in RESUME's decisions.
**Done when:** every question is answered, or deferred with its waiting card named.

### Stage 2 — <name>

#### T002 — <title>
fulfills FR-1 · after: CP0 · M · effort high

**Start with:** `<Feature> · T002. Follow <absolute path>/tasks.md §1, then card T002.`
**Read:** spec §<n> (<which part>); plan <section>. Code by symbol: `<symbol>` (`<file>`), ….
**Do:** <numbered steps when there is more than one>
**Done when:** <observable criteria: tests that fail before and pass after, outputs that exist>.
**Verify:** `<exact commands>`.
**Hand-off extras:** <what later cards need from this one>.
**Touches:** `<files>`. **Never:** <card-specific prohibitions>.

#### T003 [P] — <title>
fulfills FR-2, NFR-1 · after: CP0 (beside T002) · S · effort medium

…

#### CPA — <stage> merged and pinned
after: T002–T00n · S · effort medium

**Start with:** `<Feature> · CPA. Follow <absolute path>/tasks.md §1, then card CPA.`
**Read:** RESUME; the hand-offs' "next card must know" lines; plan <checkpoint>.
**Do:** The checkpoint routine. Pins: <rules>. Review against spec §<n>.
**Done when:** green; the pins listed; the findings recorded as §5 cards.

### Stage N — Rollout

<One paragraph every rollout card follows: take the deploy lock, check nothing else is running, how to
verify on the deployed system, the rollback path.>

### Close

#### T0nn — Results
after: <last rollout card> · M · effort medium

**Do:** every success criterion with its measured number and source<; the project's phase report>.
**Done when:** every SC has its number.

#### CPEND — Feature done
after: T0nn · S · effort low

**Do:** every card ticked or waived in RESUME; switches in their final state; locks released. Tell the
owner what comes next.

---

## 5. Backlog (cards added during execution)

Add cards here in the same format, numbered `T0nnB`, `T0nnC` after the card they split from (`A` is
taken by planned splits), with a one-line reason and a meta line `added by T0nn · after: T0nn ·
blocks: T0nn · S · effort <tier>` (the supervisor reads `after:` and `blocks:`). Add the card's row to
RESUME's status table in the same session. When done, add under the heading:
`- [x] T0nnB done <date> (<commit>; hand-off T0nnB.md)`.

---

## 6. Supervisor

One long-lived session that is the owner's assistant for this feature. It is not a card: it does no card
work and follows none of §1 except reading files by absolute path. Start it once, beside the card sessions:

**Start with:** `<Feature> · SUP. Follow <absolute path>/tasks.md §6.`

1. **Name.** First action: rename the session to `<NNN> Supervisor`.
2. **Read-only.** Never edit code, this file, RESUME or hand-off notes; never commit, deploy or do a
   card's work. The only file it writes is `state/.supervisor.json`, through the script. When files
   disagree, say which session or file should change; edit one only after the owner's yes for that edit.
3. **Files, not memory.** Before every answer run
   `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md` and answer from its output, the
   RESUME rows and the hand-off notes (cite them: "hand-off T004 says …"). Never report status remembered
   from earlier in the conversation.
4. **Stay armed.** After the first report, run
   `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --wait` as a background command.
   When it exits (the state changed), report, then start it again at once. Never end a turn without it
   running. If background commands are unavailable, say so and fall back to a self-paced loop that runs
   the report about every 20 minutes.
5. **Report on every change**, short and in this order: what changed; what is ready now, with each
   card's **Start with** line, which ones may run side by side (`[P]`, own worktree), and its effort
   tier; who waits for whom; what needs the owner (open decisions, blockers, owner's-yes steps a card
   asked for, stalled cards); drift. Lead with a one-line progress bar. When the owner must act and a
   push-notification tool is available, send one line there too.
6. **Live view.** Offer once to start the dashboard,
   `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --serve`, as a background command
   and open the URL it prints (in a browser pane when the environment has one), so the owner can see the
   state at a glance. Without a browser, offer `--watch` in a terminal tab instead.
7. **Card sessions.** When the environment can list sessions, match their titles (`<NNN> T0nn …`) to
   cards: a `doing` card with no live session may be abandoned; a finished session whose card is not
   `done` left work unfinished. Say so; don't fix it.
8. **Starting cards.** Recommend the next card; start a card session only when the owner asks, one card
   per request, by handing the **Start with** line to a new session.
9. **Answers.** "What's next?", "what is T007 waiting for?", "what did T004 decide?", "how far are we?":
   answer in a few lines from the files. "Done" means `done` in RESUME and ticked here, with a hand-off.
10. **Replaceable.** Keep raw output out of the conversation beyond the report. If this session ends, a
    new one started from the line above picks up from the files with nothing lost.
````
