---
name: spec-grill
description: Turns a vague feature or project idea into a rigorous constitution.md, spec.md, plan.md, and tasks.md through relentless one-at-a-time interviewing ("grilling") before any code is written. Combines Spec-Driven Development (SDD) phase structure with a decision-tree interview method where every question comes with a recommended answer and nothing is written until the user confirms. Use this skill ONLY when the user explicitly asks to "spec this out," "grill me," invokes spec-driven development, or names this skill directly. Also use it when the user asks to supervise, track, report on or automate the progress of an existing spec-grill tasks.md ("what's done," "what's next," "who is waiting for whom," "start the supervisor," "show the dashboard," "run the cards on autopilot") — that runs the long-lived supervisor session or the autopilot, not the interview. Do NOT trigger proactively on ordinary feature requests, bug fixes, or small changes — this is an opt-in, heavyweight process.
---

# Spec-Grill

Spec-Grill turns an unspecified idea into a chain of approved artifacts — `constitution.md` → `spec.md` → `plan.md` → `tasks.md` — using two different modes depending on the phase:

- **Grilled phases** (constitution, spec): Claude interviews the user relentlessly, one question at a time, walking a decision tree, always proposing a recommended answer, and never proceeding until the user confirms. These phases are subjective/high-stakes (goals, constraints, requirements), so the cost of getting them wrong via assumption is high.
- **Drafted phases** (plan, tasks): Claude autonomously drafts the full artifact from the approved spec/plan, then presents it for holistic review and approval — not question-by-question. These phases are more mechanical derivations where interviewing would be exhausting and low-value.

Every phase ends with an explicit approval gate. Nothing is written to disk as final until the user approves it.

After the artifacts are approved, the **supervisor** (see "Supervisor mode") keeps track of the implementation for the user: one long-lived session that watches the feature's state files and reports what finished, who waits for whom, and what to run next. The **autopilot** goes one step further: it starts each card's session itself, at the card's effort, as soon as the card is ready, and turns everything the owner must do into buttons on the dashboard.

A feature with a user interface is designed, prototyped and looked at, not only coded: the spec grills its screens, the plan phase drafts `ux.md` and a clickable prototype the user approves, the cards build each user scenario end to end (storage, API and screens in the same stage), and every interface card proves itself with screenshots the user can see on the dashboard.

## Directory layout

```
specs/
├── constitution.md              # project-wide, created once, reused across features
├── lessons.md                   # project-wide, what past features' cards really took (written by each close)
└── 001-feature-slug/
    ├── spec.md
    ├── plan.md
    ├── ux.md                    # features with a user interface: screens, states, tokens
    ├── prototype/index.html     # the clickable prototype approved with ux.md
    ├── tasks.md
    └── state/                   # written during implementation, not by spec-grill
        ├── RESUME.md
        ├── handoff/T0nn.md
        ├── design-review.md     # leftovers and guesses for the owner to review in one go
        ├── screens/T0nn/*.png   # what each interface card's screens look like
        ├── autopilot.json       # the autopilot's settings (the dashboard changes them)
        ├── runs.json            # the sessions the autopilot started
        ├── runs/T0nn-1.jsonl    # each session's output
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
- **Quality Standards** — testing bar, code review requirements, performance/security baselines. When the project has a user interface, also the **UX & design standard**: the design system or visual style, the accessibility level (recommend WCAG 2.2 AA), the screen sizes supported, and that every interface card is verified by looking at it (screenshots at phone and desktop width), not by tests alone
- **Governance** — how the constitution itself may be amended in future (who approves, what triggers a review)

Walk these in order — Governance depends on knowing what's actually being governed, so grill it last. For each section, ask targeted one-at-a-time questions with a recommended answer (e.g. "Should the constitution require test coverage before merge? Recommendation: yes, given most projects invoking this skill care about correctness — but confirm."). Stop and present the full draft `constitution.md` for approval before writing it.

## Phase 2 — Spec Grill

Grill toward these fixed sections (spec-kit standard):

- **User Scenarios / User Stories** — who's using this and what are they trying to accomplish
- **Functional Requirements** — what the system must do, numbered and testable
- **Experience** — only when the feature has a user interface (skip it, without asking, when it has none): every screen or view, the flow through them for each user scenario, each screen's states (empty, loading, error, success, and permission-denied where it applies), and what it should feel like, with references (a product, a screenshot, an existing screen in this app). Ids `UX-1`, `UX-2`, … so cards can cite them. Grill it as hard as the requirements: the interface is what users meet, and a back end that works behind screens nobody designed is a failed feature
- **Non-Functional Requirements** — performance, security, scalability, accessibility, etc.
- **Success Criteria** — how you'll know this is done and working
- **Out of Scope** — what this explicitly does NOT cover (prevents scope creep mid-implementation)
- **Open Questions** — anything deferred, unresolved, or answered "I don't know" during grilling

Dependency order: User Scenarios first (everything else derives from who/why), then Functional Requirements, then Experience (screens follow from scenarios and requirements), then Non-Functional Requirements, then Success Criteria (which should map back to the requirements), then Out of Scope (informed by everything above), with Open Questions accumulated throughout rather than asked about directly.

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

### The interface (features with an Experience section)

Beside `plan.md`, draft `ux.md` (template below): the screen map, each screen's layout, components and states, the copy that matters (empty states, errors), the design tokens or the design-system parts used, the responsive behaviour and the accessibility notes, each tied to the spec's `UX-n` ids. Read the existing app's screens, styles and components first and reuse them; look up which design and frontend skills are installed (for example `impeccable`, `ui-ux-pro-max`, `frontend-ui-engineering`) and use them for the drafting. Don't ask about any of this.

Then build a **clickable prototype**: one self-contained `prototype/index.html` in the feature folder with every screen from `ux.md`, its states reachable by clicks, and realistic sample data, in the project's look. Show it to the user at the approval gate (open it in the browser pane, or publish it as an artifact) together with `ux.md`. This is the moment the user sees the interface, before any code exists: revise until they approve both. Cards cite `ux.md` and the prototype as the reference their screens must match.

## Phase 4 — Tasks Draft (not grilled)

Once `plan.md` is approved, autonomously draft `tasks.md`. It is not a to-do list: it is the **entry point of every implementation session**. Each task is a self-contained **card** that one fresh session (new chat or `/clear`) can carry out after reading only the file's shared sections (§1–§3) and its own card. Nothing passes between sessions through chat memory; state moves only through files — a `RESUME.md` state file and one hand-off note per finished card.

### Look up before drafting (don't ask)

- From `specs/lessons.md`, when it exists: what earlier features' cards really took. Size and tier the new cards by it (a kind of card that needed two tries at medium starts at high; a card shape that kept overrunning is split), and say in the header which lesson changed which card.
- From `spec.md`: requirement ids (FR-n, NFR-n — use the spec's own ids), success criteria, Open Questions. From `plan.md`: the sections each card will cite. From `constitution.md`: quality standards (they become each card's Verify commands and the checkpoint review) and constraints (they become protocol rules and card **Never** lines).
- From the repo: test, lint and type-check commands; the directories code lives in (to define short path prefixes); git-ignored folders; worktree and branch conventions; deploy commands; whether a state folder already exists.
- From the environment: the reasoning-effort tiers or session commands available, so each card can name its effort; the directory this skill is installed in, so §6 can name `hooks/supervisor.py` by absolute path.

### Cutting the cards

1. **One card = one session.** Size each card S, M or L against one session's context budget; split every L. Also split where the work waits on someone else — an owner's decision, a paid step, an external party, a deploy — as a suffixed card (`T012A`), so no session idles while it waits.
2. **Stages.** Group cards into numbered stages in dependency order (typical: re-verify and baseline; pure contracts; storage; services; API; UI; docs; rollout; close). **When the feature has an interface, cut stages by user scenario instead of by layer:** each build stage delivers one scenario (or a few small ones) end to end — its storage, its API and its screens — so the interface is built and seen from the first build stage, not squeezed in at the end. Never let a stage close with a scenario's back end done and its screens unbuilt. End every build stage with a **checkpoint card** (`CPA`, `CPB`, …) that merges the stage, runs the full checks and pinning tests, reviews the stage's diff at high effort, turns each confirmed finding into a §5 backlog card, and batches §5's open small cards (rule 11).
3. **First card re-verifies.** The plan was written against older code: card T001 locates every fact the plan relies on *by symbol* on today's code, writes a code map, measures baselines (every number with the command that produced it), creates the state file and any worktree. When the spec has Open Questions that block cards, add `CP0` — one plain-words page for the owner, each question with a recommended default and the card that waits for it.
4. **Last cards close.** A close card records the results against the success criteria in a Checks table (a report, if the project keeps one), then `CPEND` checks every card is ticked or waived and writes the **retro**: it runs `supervisor.py <tasks.md> --lessons` (each card's kind, size, effort and model beside the runs, tries, cost and hours it really took, and a summary per effort tier) and appends to `specs/lessons.md` a dated section for the feature: the measured table, then a few lines on what to do differently (tiers that were too low or too high, card shapes that overran, failures that came back, checks that were hard to evidence). Measured numbers only; a card run by hand says so, and a batch's cards are measured together, as their batch. The next feature's Phase 4 reads it.
5. **`[P]` only when the cards' Touches lists don't overlap.** Each `[P]` card names whom it may run beside; parallel cards work in their own worktree and branch and merge back at the end of the card.
6. **Code by symbol, never by line number.** Lines move between the plan and the session.
7. **Every card names its effort tier** (and, when the environment has one, the command that sets it), **its kind** — `kind backend`, `kind frontend`, `kind fullstack`, or `kind owner` for a card only the owner can do (sign, pay, call someone; the autopilot never starts it) — and, when a cheaper or stronger model fits, `model <name>`. The autopilot reads all three from the meta line.
8. **Interface cards prove themselves visually.** A `kind frontend` or `kind fullstack` card's **Verify** runs the app, drives its screens in a browser through every state the card builds, saves screenshots at phone and desktop width to `state/screens/T0nn/` (names like `login-error-phone.png`), checks the browser console is clean and an accessibility check passes, and compares the result with `ux.md` and the prototype. Its **Done when** includes the screenshots; its **Read** includes the `ux.md` sections and the prototype screens it builds. Give interface cards effort high: they are judged by how they look and behave, which tests do not catch.
9. **Owner actions.** Any step the constitution or spec reserves for the owner (deploys, production writes, paid runs, new dependencies, outward messages) is a protocol rule: the session asks in that session, then runs it after a yes — or hands over the exact commands when it can't. A session the autopilot started asks through RESUME's Approvals table and stops until the answer comes. List such steps' commands on §6's **Never unattended** line so the autopilot's sessions cannot run them on their own.
10. **Diverging from the plan.** When a card splits, merges or reorders plan items, list each change and its reason in the header's "Where the cards differ from plan.md".
11. **Batch the cards of each stage.** One session per card pays the start-up cost (reading §1–§3, RESUME and the code) every time. So once a stage's cards are cut and sized, group them into **batches**, one session per batch: it reads once, commits each card on its own, and runs the full check and the browser walk-through once at the batch's end.
    - *What goes together:* cards of the same stage that share Touches or a screen, or that form a chain (B waits only on A), in dependency order inside the batch.
    - *Size:* a batch fits one session's context. Count S = 1 and M = 2, keep the total at 4 or less, and never include an L (split it first). Efforts differ by one tier at most; the batch runs at the highest.
    - *Always alone, never batched:* T001, checkpoints (CP0, CPA…, CPEND), the Results card, `kind owner` cards, a card that waits for an owner's decision, a card whose Do has an owner's-yes step (deploy, production write, paid step), and walk-throughs.
    - *No deadlock:* no card outside a batch may sit between two of its cards in the dependency graph (an outside card that waits on one batch card while another batch card waits on it).
    - *`[P]`:* a batch may run beside another batch or card only when none of its cards' Touches overlaps theirs; write `[P]` on the batch's checklist line and name whom it may run beside in "Who may run beside whom".

    Each stage lists its batches right under its heading, in a small table with §5's columns (batch | name | cards, in order | effort | Start with), and each batch gets a checklist line `- [ ] B<n> <name>` in §4's checklist, after its last card. Batch ids run across the whole file: B1, B2, … in the stages, then §5's batches continue the numbering. A batched card keeps its full card (Start with, Read, Do, Done when, Verify, Touches) and can still be run alone by hand, but while its batch is unfinished the supervisor and the autopilot offer only the batch. During execution the checkpoint routine and the owner's design-review triage batch the new §5 cards by the same rules (rule 12, template below).
12. **No follow-up cards from a card.** A card gets one fix. Whatever still fails afterwards, is a guess, or belongs to other work goes as a dated line in `state/design-review.md` (date, card, screen or file, finding, screenshot), and the card closes. The owner reviews that list in one go and decides which lines become cards; the new small cards are batched in the same step (rule 11), in §5's batch table. Only checkpoint reviews and the owner add §5 cards; this keeps the backlog from growing faster than it shrinks.
13. **Parallel batches.** Batches whose cards' Touches don't overlap may run side by side: each beside the first works in its own worktree on a short-lived branch (`batch/b<n>`), with its own dev-server port so the app's data stays apart, and merges into the main branch at its end after the full check. `git stash` is shared by every worktree of a repo, so sessions never use it while another batch runs; they save a patch with `git diff` instead. When the constitution says one branch, this needs the owner's amendment first (Phase 0).
14. **Checks are fixed, and every pass has evidence.** Each card's **Done when** is a list of observable criteria (a command and the output that counts as a pass, a file that exists, a screenshot), never "works" or "looks right". The session copies them into its hand-off's **Checks** table (criterion | verdict | evidence | correction), with `pass`, `fail` or `unresolved` and the evidence for each: the command and its decisive output line, a file path, a screenshot path. Missing evidence stays visible as `unresolved`. A session never edits a card's Verify or Done when, a pinning test, or any other check to obtain a pass, and never waives a check itself: only the owner waives (a RESUME decision). The supervisor shows as drift a done card without a Checks table, and a commit that changes a pinning test outside the card that writes it until a checkpoint has reviewed it (`Pins reviewed up to <commit>` in RESUME); it lists a row that is `fail` or `unresolved`, a pass without evidence, and a waiver the owner has not granted as **open checks** for the owner to decide.
15. **Failed attempts are recorded.** A session that stops before its card is done (context, blocker, interruption) writes what it tried and why it failed on the hand-off's **Tried, did not work** line; the session that continues the card reads it first and does not repeat an approach listed there without a new reason. Before repeating a deploy, migration, paid call or message, it checks whether the earlier attempt already did it.
16. **The close waits for everything.** The results card's `after:` names the last checkpoint and `§5` (every backlog card and batch), so the success criteria are measured once, on the finished feature, not while fixes are still open.

### Checks before presenting

- Every FR/NFR maps to at least one card (§3 table); every `UX-n` screen maps to the frontend or fullstack card that builds it, and every user scenario has at least one; no build stage ends with only back-end cards when its scenario has screens; every success criterion names the card that measures it; every Open Question is a decisions row with the card it must be answered before; every rule that needs a pinning test names the test file and the card that writes it. Flag any gap to the user explicitly.
- Every card has: Start with, Read, Do, Done when, Verify, Touches, and a `kind` on its meta line. No card says "see above" — a card must stand alone. Every **Done when** criterion is observable (rule 14): rewrite any that only a judgment could pass.
- Every card's meta line has an `after:` field naming the cards it waits for by id (ranges like `T002–T005` are fine; notes go in parentheses, such as `(beside T002)`), because the supervisor builds the waiting graph from it. Run `hooks/supervisor.py` on the draft and check its "Waiting" and "Ready" lists match the stage outline.
- Batches (rule 11): every S card that is not on the always-alone list is in a batch, unless the header says why not. Run `hooks/supervisor.py` on the draft and confirm the ready and waiting batches match the stage outline (a stage's batch is ready when the cards before it are done, and its cards never show as ready alone), the drift list says nothing about batches (an always-alone card, an L, more than 4 by size, a card outside a batch sitting between two of its cards), and the report has no "small cards are in no batch" heads-up.

### Presenting

Present the complete draft for holistic approval, revise as needed, then write `tasks.md`. When the file is too long to read in chat, write it as `tasks.draft.md` and present §1–§3, the checklist, the stage outline with each stage's batches and three full cards (T001, one checkpoint, the hardest card); rename it to `tasks.md` on approval.

## Phase 5 — Handoff

Once `tasks.md` is approved, make sure the project registers the **card-rename hook**, then stop.

**The card-rename hook.** Rule 1 of the session protocol (rename the session to its card) is easy to forget, so a hook enforces it. `hooks/card-rename.py` (next to this file; standard library only) runs on every prompt; when the prompt is a card's **Start with** line, it finds that `tasks.md` (absolute path, or a `…/` path resolved from the session's directory and from the main checkout of a git worktree), reads the name pattern from §1 and the card's title from the checklist, and tells Claude to rename the session before anything else. Every other prompt passes through. Check the project's `.claude/settings.local.json` (git-ignored; `.claude/settings.json` if the team shares the hook) for a `UserPromptSubmit` entry whose command runs `card-rename.py`. If none is there, ask the user, and after a yes merge this entry into the existing hooks (never replace the file):

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/spec-grill/hooks/card-rename.py", "timeout": 10}]}]}}
```

Test it before saying it works: pipe `{"cwd": "<project>", "prompt": "<T001's Start with line>"}` into the command and check the name it prints.

Then Spec-grill's job is done — implementation is a separate concern. Tell the user the two ways to run it:

- **Autopilot (recommended for long card lists).** One command in their own terminal starts the dashboard and the dispatcher, which runs every card in its own session as soon as it is ready (see "Autopilot" below): `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --serve --autopilot --open`. Before the first time, two checks in that terminal: `claude` is logged in, and the project folder is trusted (run `claude` there once and accept). Suggest a dry card first: start it with the autopilot paused, press Start on T001 and watch its log in the card drawer.
- **By hand.** One card per fresh session, starting by pasting T001's **Start with** line, with the dashboard (`--serve`) showing what to paste next.

Either way, the supervisor session (§6's **Start with** line) is their assistant for questions. Offer to start the supervisor or show the dashboard, but don't start anything unprompted, and never start the autopilot from inside a Claude session: it launches sessions that work unattended, so the owner starts it in their own terminal.

## Supervisor mode

The user loses track of a long card list: which cards finished, which wait on what, what to paste next. The supervisor is their assistant for one feature: a single long-lived session that never does card work and never loses the thread, because it rebuilds its picture from the files every time instead of remembering it.

It has two halves:

- **`hooks/supervisor.py`** (standard library only) — the deterministic half. It reads `tasks.md` (checklist, each card's `after:`, `blocks:`, `kind` and `model`), `state/RESUME.md` (status, decisions, approvals, blockers, deploy lock), `state/handoff/*.md`, `state/screens/`, the autopilot's `state/runs.json` and git, and reports: progress bar, done, running now, ready to run next (with each card's **Start with** line and what finishing it unblocks; a batch, a stage's or §5's, is one entry with its cards, its effort and its own **Start with** line, and its cards never show as ready on their own), who waits for whom (a card, the integration worktree, an owner's decision, a stage review, a blocker), what needs the owner, the back-end/front-end balance (it warns when the interface falls behind), a quiet heads-up when 3 or more open small cards that could share a session (shared Touches, a chain, or the same stage when Touches can't be read) are in no batch, stalled cards, drift (including a batch that breaks rule 11's rules: an always-alone card in it, an L or more than 4 by size, an outside card between two of its cards; a done card without a Checks table, and a commit that changes a pinning test outside the card that writes it, until a checkpoint records it reviewed) and open checks for the owner (a check a done card recorded as `fail` or `unresolved`, a pass without evidence, a waiver the owner has not granted). `--lessons` prints what each card (and each batch) really took, for the close card's retro. `--serve` runs the **dashboard** (`hooks/dashboard.html`) on `http://127.0.0.1:8765`, local only: progress, a "Run next" panel (a ready batch is one row: its id, name, cards and Copy line or Start), a "Needs you" panel (with quiet, collapsed lines for the heads-ups, such as small cards in no batch, and the open checks) where the owner answers approvals and decisions, approves stages and marks their own cards done, a board (each batch card tagged with its batch, and a line of the batches with each one's stage and progress), a dependency map, a screens gallery, the latest commit and a status line saying what the autopilot is doing (working, between cards, waiting for the owner, paused, or a dispatcher that stopped checking in), a commit list matched to cards, a live card per running session (elapsed time, last output, to-do progress, the step it is on) and a card drawer with its RESUME row, hand-off note and a transcript that follows the session as it runs, an activity feed, the drift list and the open checks. `--watch` redraws a live view in a terminal; `--wait` blocks until the state changes, prints what changed and the report, and exits; `--json` is for scripts.
- **The supervisor session** — the conversational half. It follows tasks.md §6: it stays armed with `--wait` as a background command, so every change (a card finished, an approval requested, a stage waiting for review) wakes it to report, and it answers the user's questions at any time from fresh output and the hand-off notes.

When the user asks only to see the dashboard, start `--serve` in the background, open its URL and stop there; the full supervisor session is not needed for that.

**Entering supervisor mode** (the user asks to supervise, track or report on a feature, or pastes §6's **Start with** line): find the feature's `tasks.md` (the one the user names; otherwise the newest `specs/NNN-*/tasks.md`, asking only when several are in progress). If it has a §6, follow it. If it predates §6, or its §6 predates the autopilot (no "Never unattended" line), follow the §6 template below with its paths filled in, and offer to update §6 in the file. Supervisor mode never interviews and never edits the artifacts.

"Never sleeps" has two layers and the user should know which is which: `supervisor.py --watch` or `--serve` in a terminal tab costs nothing and runs until the tab closes; the supervisor session sleeps between events and is woken by each `--wait` exit, so it spends tokens only when something changed. It lasts as long as the session stays open (on desktop, the app's keep-awake setting stops the machine sleeping under it). If it is closed, a new session started from §6's line picks up from the files with nothing lost.

### Upgrading a tasks.md drafted with an older template

When the user asks to bring an existing `tasks.md` up to the current template (after this skill was updated), merge the template's rules into the file; never replace it. The file's own content stays: its paths, project rules, item wording, cards and §3. Steps:

1. Make sure no card session works on the feature: the autopilot is paused and `runs.json` has no live run, or the owner confirms no session is open. Copy the file (and RESUME) to a backup first.
2. §1: add what the template's §1 has and the file lacks, inside the matching items (start, scope, context budget, owner's yes, finish), keeping the file's numbering. The autopilot's unattended rules name §1's items by their titles (the Preconditions, scope, Context budget, Owner's yes and Finish items), not by number: when one of the file's items has a different title, say which and offer to rename it.
3. §2: add the hand-off template's missing lines and its Checks table; add RESUME's missing sections to the RESUME template.
4. The checkpoint routine, the Results and CPEND cards, §5's Batches heading, and a batch table for each stage whose cards are not started yet (rule 11): as in the template, cards not started only. Never edit a done card, a ticked line, or a card that is `doing`.
5. When cards are already done, add `**Checks from:** <today>` under §2's hand-off template: cards finished before that day are exempt from the evidence checks (their hand-offs predate the Checks table). Write `Pins reviewed up to <HEAD's commit>` in RESUME, so the pin check starts from today instead of reporting the feature's whole history.
6. Run `supervisor.py` on the file before and after; the ready and waiting lists must not change, and new drift must be only what the upgrade intends. Show the owner a short summary of what changed.

### Autopilot

`supervisor.py <tasks.md> --serve --autopilot` adds the **dispatcher** (`hooks/autopilot.py`, standard library only) to the dashboard. The owner starts it in their own terminal; it needs the `claude` CLI logged in and the project folder trusted. Every few seconds, while the feature's autopilot is on, it:

- **starts each ready card, or ready batch,** in a headless session (`claude -p`) from the repository root. A batch (a stage's or §5's) runs as one session for all its cards in order (its **Start with** line, its effort or else the highest of its cards', the session named `<NNN> B1 <batch name>`); the batch's cards never start on their own, and attempts, approvals, blockers and resumes count per batch. A card: the card's **Start with** line as the prompt, `--effort` from the card's tier, `--model` when the card names one, the session named `<NNN> T0nn <title>` (`-n`), a dollar cap per session, `--permission-mode auto`, §6's **Never unattended** tool patterns denied, and rules appended to the system prompt for running unattended (below). At most "Parallel" sessions at once (3 by default), and at most one card without `[P]` or batch, since those share the integration worktree. An owner's card (`kind owner`) is never started;
- **turns the owner's yes into a button**: a session that reaches a step needing the owner (§1's Owner's yes item) adds a row to RESUME's **Approvals** table and ends its turn; the dashboard shows Approve / Reject with an optional note; on an answer the dispatcher resumes that same session (`--resume`) with it, and lifts the deny pattern the approved step needs for that resume only;
- **turns the owner's choices into answers**: a session that needs a pick only the owner can make (a design option, an open question) adds a row to RESUME's **Decisions** table naming its card, keeps the card `doing` and stops; once the owner answers on the dashboard, the session is resumed. Waiting this way never counts as a failed try;
- **holds each stage for review**: when a checkpoint card (CPA, CPB, …) is done, the cards after it wait until the owner presses "Approve stage" (after looking at its findings and the screens). The owner can switch this off ("Hold each stage for my review");
- **runs as the right account**: when §6 says `**Runs as:** <email> via <launcher>`, sessions start with that launcher (unless the settings name another), and before dispatching and before each session the dispatcher asks `<launcher> auth status`; on a different account it pauses ("sessions would run as X, but the feature expects Y"), so nothing bills the wrong account or pairs with the wrong Chrome profile. With Chrome on, one cheap check session first opens §6's **App URL** and pauses the autopilot if it lands on a sign-in page instead of the app. The dashboard shows "runs as <email>" and the Chrome check's result;
- **stops sessions that hang after their result**: a session whose final result (or an error such as a session limit) is written but whose process has not exited after `result_grace_s` (30 s) is stopped, so it never holds a slot or hides a pause;
- **waits out blockers instead of counting them**: a session that records a blocker and ends with `AUTOPILOT: BLOCKED` does not use up an attempt. The card waits while a RESUME blocker names it; once those blockers are gone (cleared by a session, edited out, or marked resolved with the dashboard's Resolved button), the dispatcher resumes the same session by itself, through the usual slot and Chrome limits. A blocker that names no card waits for the owner's "Unblock and continue";
- **picks up after interruptions**: a session that ended before its card was done is resumed once more (`max_attempts`, 2 by default); after that the card shows under "Needs you" as stuck, with Retry and "I'll take it" (then the dashboard gives the `claude --resume <id>` command to continue the session by hand);
- **stops runaway sessions**: silent for 45 minutes or running over 6 hours (both settings), or over the dollar cap — each becomes a stuck card for the owner;
- **pauses itself** on an API error (an expired login, a usage limit) without counting it against the card, and when the sessions together reach the total budget, if one is set; the reason shows on the dashboard, and Resume continues;
- **notifies the owner** (a desktop notification) once for each new thing that needs them;
- **gives sessions a browser when the owner allows it**: "Give sessions Chrome" in the dashboard's settings (off by default, confirmed once) starts every session with `--chrome`, so cards whose Verify walks the app or takes screenshots can do it unattended. It is the owner's real, signed-in Chrome, so sessions then run one at a time and are told to work in their own tab, use only the app under test and the pages their card names, and never sign in, change settings or submit forms elsewhere.

The unattended rules the dispatcher appends tell each session: follow §1 and the card exactly; never run a step that needs the owner's yes, but ask through the Approvals table and stop; record blockers and splits as §1 says; end with `AUTOPILOT: DONE`, `WAITING FOR APPROVAL A<n>`, `BLOCKED` or `SPLIT`; never start a card or batch beyond the one it was started for. The files stay the only channel: the dispatcher decides from RESUME, not from what a session says.

Settings live in `state/autopilot.json` and change from the dashboard's autopilot bar (on/paused, parallel sessions, stage review); the rest (`budget_per_card_usd` 20, `budget_total_usd` 0 = none, `max_attempts`, `quiet_minutes`, `max_run_hours`, `permission_mode`, `notify`) are edited in the file. Running sessions keep going if the dispatcher stops; a new dispatcher picks them up from `state/runs.json`. Only one dispatcher works on a feature at a time (`state/.autopilot.lock`).

With the autopilot on, the supervisor session still reports and answers; it never starts card sessions itself, because the dispatcher does.

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
- UX & design (projects with an interface): <design system or style>, <accessibility level>, <screen sizes>; interface cards are verified with screenshots.

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

## Experience
(features with a user interface)
- UX-1 <screen>: purpose; reached from …; states: empty, loading, error, success
- Flow for <scenario>: <screen> → <screen> → …
- Look and feel: <references>

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

### ux.md
```markdown
# UX: <feature name>

Spec: [spec.md](spec.md) (UX-1–UX-n). Prototype: [prototype/index.html](prototype/index.html).

## Screen map
<screens and how the user moves between them, per user scenario>

## Screens
### UX-1 <screen>
- Purpose and entry points:
- Layout (phone / desktop):
- Components (existing ones reused, new ones):
- States: empty · loading · error · success (· no permission), with the copy for each:
- Interactions and feedback:
- Accessibility notes (focus order, labels, contrast):

## Design tokens / design system
<colours, type scale, spacing, radius, motion; or the design-system parts used>

## Responsive behaviour
<breakpoints and what changes at each>
```

### tasks.md

The section numbers (§1–§6) and card fields are fixed; fill the `<…>` parts from the repo and the approved artifacts. `[P]` = may run at the same time as the cards named in its card, in another session and another worktree. Sizes: S, M, L (split every L). Ids: `T0nn` planned cards, `T0nnA` planned splits, `T0nnB`… backlog cards, `CP0`/`CPA`…/`CPEND` checkpoints, `B1`, `B2`… batches (numbered across the file: the stages' first, then §5's).

````markdown
# Tasks: <feature name>

Design: [spec.md](spec.md) (requirements FR-1–FR-n, NFR-1–NFR-n, screens UX-1–UX-n, success criteria
SC-1–SC-n). Rationale and acceptance detail: [plan.md](plan.md). Interface: [ux.md](ux.md) and the
approved prototype, [prototype/index.html](prototype/index.html). This file is the **entry point of every working
session**: one card, one fresh session. A session reads §1–§3 and its own card, nothing else unless the
card lists it. To see where the feature stands, ask the supervisor (§6) instead of reading this file.

Format: `- [ ] T0nn [P] title — fulfills FR-…`. Every requirement maps to at least one card (§3).

**Where the cards differ from plan.md:**
1. <split / merge / reorder, and why>

---

## 1. Session protocol (every card follows it)

1. **One card or one batch per session.** Open a fresh session for each card, or for each batch (a
   stage's batch table, or §5's; its cards in the listed order, each committed on its own). While a
   card's batch is unfinished, the card runs with its batch, not alone. Never continue into another
   card or batch in the same session, even if there is room left. First action: rename the session to `<NNN> T0nn <card title>` (for a batch, T0nn is the batch's id and the title its name, e.g. `004 B2 Search and export`) (desktop: `set_session_title`; CLI: `/rename`).
2. **Where things live.** State: `specs/NNN-slug/state/RESUME.md` and `state/handoff/T0nn.md`
   <git-ignored folders are read and written by absolute path from worktrees>. Code work happens in
   <the integration worktree `<path>`, branch `<branch>`, created by T001>; a `[P]` card works in
   `<path>-t0nn`, branch `<branch>-t0nn-<slug>`, and merges back at the end of the card — only while no
   card without `[P]` is `doing` in RESUME; otherwise the branch waits for the next checkpoint. A `[P]`
   batch started beside another works in `<path>-b<n>`, branch `batch/b<n>`, dev server on its own
   port, and merges back at the batch's end (main merged in first, then the full check). Never `git
   stash` while another session works in the repo (the stash is shared by every worktree); save a patch
   with `git diff` instead.
3. **Start.** Read, in this order and only these:
   1. this file's §1–§3 and your card;
   2. RESUME (status, decisions, locks, blockers);
   3. the hand-off notes of the cards your card depends on, and your own card's hand-off if an earlier
      session left one (its "Tried, did not work" line: don't repeat those approaches without a new
      reason);
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
5. **Stay in scope; one fix, no follow-up cards; checks are fixed.** Do only your card (or your
   batch's cards). Each gets one fix; whatever still fails afterwards, is a guess, or belongs to other work goes as a dated line
   in `state/design-review.md` (date, card, screen or file, finding, screenshot), and the card closes.
   Don't add §5 cards: the owner reviews design-review.md in one go, decides which lines become
   cards and batches them (§5's Batches) in the same step (checkpoint reviews still add §5 cards).
   Never edit your card's Verify or Done when, a pinning test, or any other check to obtain a pass,
   and never waive one yourself: only the owner waives (a RESUME decision).
6. **Context budget.** Keep raw logs, query results and big files out of the conversation (scratch
   files; read summaries). If the session gets heavy before the card is done, write the hand-off with
   what is finished, what is left and what you tried that did not work, add the remainder as a new §5
   card, and stop. Any session that stops before its card is done fills the hand-off's "Tried, did not
   work" line the same way.
7. **Owner's yes.** <deploys, production writes, paid steps (check the remaining budget first), new
   dependencies, outward messages> need the owner's yes **in that session**. After the yes the session
   runs the step, or hands the exact commands to the owner when it can't. Before repeating such a step
   after an interruption, check whether the earlier attempt already did it. A session the autopilot
   started (nobody reads it) asks through RESUME's Approvals table instead and stops; it is resumed with
   the answer. <Only the session holding
   RESUME's deploy lock deploys; take it before, release it after.>
8. **<Project rules from the constitution>** (data handling, secrets, words that must never appear in
   code, …).
9. **Finish.** (In a batch: steps 1–4 for each card with the card's own tests and <the fast checks,
   e.g. typecheck> instead of the full check, then at the batch's end <the full check command> and one
   browser walk-through over every card's screens, fixes for what they find, the batch's line ticked
   (`[x]`, in §4's checklist or §5's), and step 5.)
   1. Run the card's Verify and <the full check command>; everything green, a waiver the owner
      granted in RESUME, or a check recorded as `fail` in the Checks table with its design-review.md
      line (the scope item). Fill the hand-off's Checks table: one row per Done when criterion, with its
      evidence; write a `|` inside a cell as `\|`. An interface card's screenshots are in `state/screens/T0nn/`; when the browser window won't shrink to
      phone width, load the page in a phone-wide iframe and say so in the hand-off.
   2. Commit on the card's branch (conventional message, nothing else in the commit); merge back if the
      card is `[P]` (the rule under "Where things live").
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
- Tried, did not work (approach, why it failed; kept by every session of this card):
- Left open / cards added to §5:
- The next card must know (≤ 10 lines):

## Checks
| criterion | verdict | evidence | correction |
| --- | --- | --- | --- |
| <each Done when line> | pass / fail / unresolved | <command and its decisive output line, file or screenshot path> | <what was fixed, or why it is still open> |
```
(In the Checks table, write a `|` inside a cell as `\|`.)

**RESUME** — `state/RESUME.md`, created by T001 with this content and kept current by every card:

```markdown
# <feature> - resume

State file for the one-card-per-session protocol in <absolute path of tasks.md> (§1). Every session
reads this first and updates its own row at the end.

## Layout
(worktrees, branches; code map; baseline)

## Pins
(the stage's pinning tests, listed by each checkpoint; then the line `Pins reviewed up to <commit>`
once it has reviewed the pinning tests changed outside their writing card)

## Deploy lock
free

## Switch states
(feature flags and their values per environment, as last read)

## Decisions (spec Open Questions)
| # | question | recommended | needed before | answer | by, when |
| --- | --- | --- | --- | --- | --- |

## Approvals
(steps that need the owner's yes, asked by sessions the autopilot started; status pending | approved |
rejected | done)
| # | card | step | why | status | answer |
| --- | --- | --- | --- | --- | --- |

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
| UX-1 | <screen> | T003 |

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
- [ ] T004 <title> — fulfills FR-3
- [ ] B1 <batch name>
- [ ] CPA <stage> merged and pinned
- [ ] …
- [ ] T0nn Close: results against the success criteria
- [ ] CPEND Feature done

**Who may run beside whom:** Stage 2: B1 ∥ T003; …

Each card: **fulfills · after · size · effort · kind** (`backend`, `frontend`, `fullstack`, `owner`; then
`· model <name>` when the card needs a particular model), **Start with** (paste as the session's first message),
**Read**, **Do**, **Done when**, **Verify**, **Hand-off extras**, **Touches** (files, to see which cards
may run side by side), and **Never** where a card has its own.

**Batches** (`B1`, `B2`, …; chosen by the rules under §5's Batches): cards that run in one session, in
the listed order. A stage's batches are in a table under the stage's heading (batch | name | cards, in
order | effort | Start with), and each has a line `- [ ] B<n> <name>` in the checklist above, after its
last card, ticked when the batch is done (`[P]` on that line when it may run beside others). A batched card
keeps its full card and may be run alone by hand; while its batch is unfinished the supervisor and the
autopilot offer only the batch.

**Checkpoint routine** (CPA, CPB, …; each checkpoint card adds its own items):
1. Merge each `[P]` branch still open into the integration branch.
2. Merge the main branch into the integration branch.
3. <the full check command>, plus what the card adds (integration tests, migrations up and down, …).
4. The stage's pinning tests pass; list them in RESUME.
5. Every card of the stage has a Checks table with evidence for each pass (the supervisor's drift
   list and open checks show the gaps); a missing or `unresolved` row is checked now, and a pinning
   test changed outside its writing card is reviewed line by line for a weakened assertion. After
   that review, write `Pins reviewed up to <commit>` (the commit reviewed up to) in RESUME's Pins
   section, so the supervisor stops listing those changes as drift.
6. Review the stage's diff since the previous checkpoint at high effort. Each confirmed finding becomes
   a §5 card; a finding against a pinned rule blocks the next stage until it is fixed.
7. When the stage built screens: a design review at high effort. Run the merged app, take fresh
   screenshots of every screen the stage touched (`state/screens/<checkpoint>/`), and critique them
   against `ux.md`, the prototype and the constitution's UX standard (hierarchy, spacing, states,
   copy, accessibility, phone width) <with the design skill the project has>. Each confirmed finding
   becomes a §5 `kind frontend` card.
8. Batch the backlog: group §5's open small cards (this checkpoint's findings and any left over) into
   batches by the rules under §5's Batches, and write each in §5's batch table with its checklist
   line. When the findings changed the next stage's cards, re-batch that stage's table the same way.
9. RESUME: the checkpoint's commit, the pins, the findings, the batches. The owner reviews the stage
   on the dashboard before the next one starts (when the autopilot holds stages).

### Stage 1 — Re-verify and baseline

#### T001 — Re-verify, measure; worktree and RESUME
fulfills <baselines> · after: <precondition> · M · effort high · kind fullstack

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
after: T001 · S · effort low · kind backend

**Start with:** `<Feature> · CP0. Follow <absolute path>/tasks.md §1, then card CP0.`
**Read:** spec Open Questions; hand-off T001.
**Do:** One page for the owner in plain words: each open question with its recommended default and the
card that waits for it. Put each in RESUME's decisions table (question, recommended, needed before);
the owner answers there or on the dashboard, and each card waits only for its own questions.
**Done when:** every question is a decisions row with its recommendation and its waiting card.

### Stage 2 — <user scenario> (end to end: storage, API, screens)

| batch | name | cards, in order | effort | Start with |
| --- | --- | --- | --- | --- |
| B1 | <batch name> | T002, T004 | high | `<Feature> · B1. Follow <absolute path>/tasks.md §1, then the cards of batch B1 (Stage 2's batch table) in order.` |

#### T002 — <title>
fulfills FR-1 · after: CP0 · M · effort high · kind backend

**Start with:** `<Feature> · T002. Follow <absolute path>/tasks.md §1, then card T002.`
**Read:** spec §<n> (<which part>); plan <section>. Code by symbol: `<symbol>` (`<file>`), ….
**Do:** <numbered steps when there is more than one>
**Done when:** <observable criteria: tests that fail before and pass after, outputs that exist>.
**Verify:** `<exact commands>`.
**Hand-off extras:** <what later cards need from this one>.
**Touches:** `<files>`. **Never:** <card-specific prohibitions>.

#### T003 [P] — <screen title>
fulfills FR-2, UX-1 · after: CP0 (beside T002) · M · effort high · kind frontend

**Start with:** `<Feature> · T003. Follow <absolute path>/tasks.md §1, then card T003.`
**Read:** ux.md §UX-1; the prototype's <screen>; spec UX-1. Code by symbol: `<component>` (`<file>`), ….
**Do:** <build the screen and every state ux.md lists, against the API contract (a stub until T002
merges)>.
**Done when:** every UX-1 state reachable and matching ux.md; screenshots saved; tests green.
**Verify:** `<tests>`; run `<dev server command>`, drive the screen in a browser through each state,
save screenshots at 390 px and 1280 px wide to `state/screens/T003/`; browser console clean;
`<accessibility check>` passes.
**Hand-off extras:** the screenshot list; what differs from the prototype and why.
**Touches:** `<files>`.

#### T004 — <title>
fulfills FR-3 · after: T002 · S · effort medium · kind backend

**Start with:** `<Feature> · T004. Follow <absolute path>/tasks.md §1, then card T004.`
**Read:** spec §<n>; plan <section>; hand-off T002. Code by symbol: `<symbol>` (`<file>`), ….
**Do:** <the steps>.
**Done when:** <observable criteria>.
**Verify:** `<exact commands>`.
**Touches:** `<files, mostly T002's>`.

#### CPA — <stage> merged and pinned
after: T002–T004 · S · effort high · kind fullstack

**Start with:** `<Feature> · CPA. Follow <absolute path>/tasks.md §1, then card CPA.`
**Read:** RESUME; the hand-offs' "next card must know" lines; plan <checkpoint>.
**Do:** The checkpoint routine. Pins: <rules>. Review against spec §<n>; design review of <screens>.
**Done when:** green; the pins listed; the findings recorded as §5 cards and batched.

### Stage N — Rollout

<One paragraph every rollout card follows: take the deploy lock, check nothing else is running, how to
verify on the deployed system, the rollback path.>

### Close

#### T0nn — Results
after: <last rollout card>, §5 · M · effort medium · kind fullstack

**Do:** every success criterion with its measured number and source, as a Checks table in the
hand-off (criterion | verdict | evidence | correction)<; the project's phase report>.
**Done when:** every SC has a row with its number and evidence; none is left out because it failed.

#### CPEND — Feature done
after: T0nn · S · effort low · kind fullstack

**Do:** every card ticked or waived in RESUME; switches in their final state; locks released. Retro:
run `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --lessons` and append a dated
section for this feature to `specs/lessons.md` (create it with a `# Lessons` heading if missing): the
table it prints, then at most ten lines on what the next feature should do differently, each tied to
a number in the table or a hand-off (tiers too low or too high, card shapes that overran, failures
that came back, checks that were hard to evidence). Tell the owner what comes next.
**Done when:** every card finished; `specs/lessons.md` has this feature's section.

---

## 5. Backlog (cards added during execution)

### Batches
(one session per batch, as §1's first item and its Finish item say; the session ticks the batch's line
when the batch is done. Ids continue after the stages' batches. A checkpoint (routine step 8) and the
owner, when turning design-review.md lines into cards, batch the new small cards here. The rules, for
these and for a stage's batches:
- together: cards of one stage (here, of §5) that share Touches or a screen, or form a chain (B waits
  only on A), in dependency order;
- size: S = 1, M = 2, at most 4 per batch, never an L; efforts at most one tier apart, the batch runs
  at the highest;
- never in a batch: T001, a checkpoint, the Results card, an owner's card, a card waiting for an
  owner's decision, a card whose Do needs the owner's yes (deploy, production write, paid step), a
  walk-through;
- no card outside the batch waits on one of its cards while another of its cards waits on it;
- `[P]` on the batch's line only when none of its cards' Touches overlaps those it runs beside.)

- [ ] B2 <batch name>

| batch | name | cards, in order | effort | Start with |
| --- | --- | --- | --- | --- |
| B2 | <name> | T0nnB, T0mmC | high | `<Feature> · B2. Follow <absolute path>/tasks.md §1, then the cards of batch B2 (§5, Batches) in order.` |

Add cards here in the same format, numbered `T0nnB`, `T0nnC` after the card they split from (`A` is
taken by planned splits), with a one-line reason and a meta line `added by T0nn · after: T0nn ·
blocks: T0nn · S · effort <tier> · kind <kind>` (the supervisor reads `after:` and `blocks:`). Add the card's row to
RESUME's status table in the same session. When done, add under the heading:
`- [x] T0nnB done <date> (<commit>; hand-off T0nnB.md)`. A short backlog card may also be written inline: its
checklist line, then its meta line and fields indented under it; the supervisor reads both forms.

---

## 6. Supervisor

One long-lived session that is the owner's assistant for this feature. It is not a card: it does no card
work and follows none of §1 except reading files by absolute path. Start it once, beside the card sessions:

**Start with:** `<Feature> · SUP. Follow <absolute path>/tasks.md §6.`

**Autopilot:** the owner runs, in their own terminal,
`python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --serve --autopilot --open`; it starts
each ready card in its own session and puts everything that needs the owner on the dashboard.
**Runs as:** <the owner's account email> via `<the CLI launcher logged in as it, e.g. ~/.local/bin/claude-work>`
**App URL:** <the app's local URL, which the Chrome check opens>
**Never unattended:** `Bash(git push:*)`, <`Bash(<deploy command>:*)`, production and paid commands>
(the autopilot denies these to the sessions it starts; they go through the Approvals table).

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
6. **Live view.** If the autopilot is not already running (the report says), offer once to start the dashboard,
   `python3 <skill dir>/hooks/supervisor.py <absolute path>/tasks.md --serve`, as a background command
   and open the URL it prints (in a browser pane when the environment has one), so the owner can see the
   state at a glance. Without a browser, offer `--watch` in a terminal tab instead.
7. **Card sessions.** When the environment can list sessions, match their titles (`<NNN> T0nn …`) to
   cards: a `doing` card with no live session may be abandoned; a finished session whose card is not
   `done` left work unfinished. Say so; don't fix it.
8. **Starting cards.** With the autopilot running, never start card sessions: the dispatcher does, and
   the owner's buttons do. Without it, recommend the next card (or the autopilot, for long lists);
   start a card session only when the owner asks, one card per request, by handing the **Start with**
   line to a new session.
9. **Answers.** "What's next?", "what is T007 waiting for?", "what did T004 decide?", "how far are we?":
   answer in a few lines from the files. "Done" means `done` in RESUME and ticked here, with a hand-off.
10. **Replaceable.** Keep raw output out of the conversation beyond the report. If this session ends, a
    new one started from the line above picks up from the files with nothing lost.
````
