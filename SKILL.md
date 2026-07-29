---
name: spec-grill
description: Turns a vague feature or project idea into a rigorous constitution.md, spec.md, plan.md, and tasks.md through relentless one-at-a-time interviewing ("grilling") before any code is written. Combines Spec-Driven Development (SDD) phase structure with a decision-tree interview method where every question comes with a recommended answer and nothing is written until the user confirms. Use this skill ONLY when the user explicitly asks to "spec this out," "grill me," invokes spec-driven development, or names this skill directly. Do NOT trigger proactively on ordinary feature requests, bug fixes, or small changes — this is an opt-in, heavyweight process.
---

# Spec-Grill

Spec-Grill turns an unspecified idea into a chain of approved artifacts — `constitution.md` → `spec.md` → `plan.md` → `tasks.md` — using two different modes depending on the phase:

- **Grilled phases** (constitution, spec): Claude interviews the user relentlessly, one question at a time, walking a decision tree, always proposing a recommended answer, and never proceeding until the user confirms. These phases are subjective/high-stakes (goals, constraints, requirements), so the cost of getting them wrong via assumption is high.
- **Drafted phases** (plan, tasks): Claude autonomously drafts the full artifact from the approved spec/plan, then presents it for holistic review and approval — not question-by-question. These phases are more mechanical derivations where interviewing would be exhausting and low-value.

Every phase ends with an explicit approval gate. Nothing is written to disk as final until the user approves it.

## Directory layout

```
specs/
├── constitution.md              # project-wide, created once, reused across features
└── 001-feature-slug/
    ├── spec.md
    ├── plan.md
    └── tasks.md
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

Once `plan.md` is approved, autonomously draft `tasks.md`:

- An ordered, dependency-aware checklist of discrete implementation tasks
- Each task tagged with the requirement(s)/user story it fulfills (traceability back to `spec.md` — every requirement should map to at least one task; flag any that don't)
- Grouped into phases (e.g. Setup, Core, Integration, Polish)
- Tasks that can run in parallel explicitly marked as such

Present the complete draft for holistic approval, revise as needed, then write `tasks.md`.

## Phase 5 — Handoff

Once `tasks.md` is approved, stop. Spec-grill's job is done — implementation is a separate concern. Offer to begin implementing the tasks if the user asks, but don't start unprompted.

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
```markdown
# Tasks: <feature name>

## Phase: Setup
- [ ] T001 [P] <task> — fulfills FR-1

## Phase: Core
- [ ] T002 <task> — fulfills FR-2, FR-3

## Phase: Integration
- [ ] T003 <task> — fulfills FR-4

## Phase: Polish
- [ ] T004 [P] <task> — fulfills NFR-1
```
`[P]` marks tasks safe to parallelize.
