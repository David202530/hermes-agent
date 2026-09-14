---
name: capture-router
description: "Structured capture of ideas/tasks/research/decisions/reminders/projects from an explicit-prefix Telegram message into BACKLOG.md."
version: 0.1.0
author: David Mamrikishvili, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Capture, Backlog, Chief-of-Staff, Telegram]
    related_skills: [daily-executive-brief, weekly-review-planning]
---

# Capture Router

Turn a short, explicitly-prefixed message into a structured backlog item —
classified, deduplicated, and persisted — with a concise confirmation.
This is not a general note-taking or task-management assistant: it only
handles the six exact prefixes below, and only that one message.

## When to use

Only when the user's message **starts with** one of these prefixes
(case-insensitive), followed by `:`:

```
Idea:
Task:
Research:
Decision:
Reminder:
Project:
```

Examples:

```
Idea: compare Georgian VAT treatment of corporate events with ECJ practice
Task: update training slides on VAT business transfer
Research: find ECJ cases on employee entertainment
Decision: use Hermes as the approval layer for Tax Agent
```

**If the message does not start with one of these six prefixes, this
skill does not apply — do not invoke it, and do not try to classify the
message some other way.** v1 is explicit-prefix-only by design (see
`scripts/capture_router.py`'s `parse_prefix()`): every ordinary
conversational message must pass through completely unaffected. This is
the same reasoning that moved `tax_content_bridge` off the
`pre_gateway_dispatch` hook — a hard interception point risked normal
conversation; a skill the agent chooses to invoke does not.

This skill also handles two other message shapes from the same six-item
vocabulary — see **Read-only queries** and **Update commands** below —
but capture (this section) is the primary path.

## Integration mode: agent-mediated live chat skill only

**Capture Router v1 integration: AGENT-MEDIATED LIVE CHAT SKILL.** This
is a deliberate architectural decision, not a placeholder:

- **No cron job.** This skill is never scheduled — it only runs inside
  the conversational session, triggered by the message that invoked it.
- **No gateway hook.** It is not wired into `gateway/run.py`'s
  `pre_gateway_dispatch` or any other message-interception point.
- **No automatic classification of ordinary conversation.** The
  conversational agent invokes this skill only when a message starts
  with one of the six explicit prefixes, or matches one of the explicit
  query/update command shapes below. Everything else is unaffected,
  full stop.

## Read-only guarantee for queries

`Backlog` / `My backlog` / `Show HIGH priority` / `Show Tax research` /
`Show items for <project>` never mutate `BACKLOG.md` — they only read
and filter. See **Read-only queries** below.

## Procedure — capture

1. Run the router script in `capture` mode with the user's exact raw
   message text:

   ```bash
   python3 skills/productivity/capture-router/scripts/capture_router.py capture "<raw message>"
   ```

   This is entirely deterministic — do not re-classify type, domain,
   project, priority, or next action yourself; the script already
   applied every rule in **Classification** below.

2. The script prints one JSON object. Three possible shapes:

   - `{"matched": false}` — the message did not start with a recognized
     prefix. Do not reply as this skill; continue the conversation
     normally (this should not normally happen if the skill was invoked
     correctly, since invocation itself should already be gated on the
     prefix).
   - `{"matched": true, "duplicate": true, "duplicate_of": "BL-xxxx"}` —
     a likely-duplicate open item already exists. Reply **exactly**:

     ```
     Possible duplicate of BL-xxxx — merge with it, or create a separate item?
     ```

     Do **not** create the new item and do **not** merge automatically.
     Wait for the user's answer before taking any further action.
   - `{"matched": true, "duplicate": false, "entry": {...}}` — a new
     item was persisted. Reply with **exactly** this format (fill in
     the real values from `entry`, Title Case for Type/Priority):

     ```
     Captured as BL-0002

     Type: Research
     Domain: Tax
     Project: VAT / EU Alignment Research
     Priority: Medium
     Next action: Find relevant ECJ cases
     ```

     No extra explanation, no restating the original message, no
     apology text — this confirmation must stay this short every time.

## Procedure — read-only queries

If the message is exactly one of: `Backlog`, `My backlog`, or matches
`Show HIGH/MEDIUM/LOW priority`, `Show <Domain> research`, `Show items
for <project>` — run:

```bash
python3 skills/productivity/capture-router/scripts/capture_router.py query "<raw message>"
```

Render the returned `entries` the same way `scripts/capture_router.py`'s
`render_query_results()` does — this is the deterministic reference
implementation; reproduce its output exactly rather than improvising a
different layout:

```
BL-0002 — Test Capture Router production persistence
MEDIUM · BACKLOG

Reply: Start BL-0002 · Done BL-0002 · Cancel BL-0002
```

One block per item, separated by a blank line. The action-hint line
(`Reply: ...`) is derived **only** from the item's ID and status —
`BACKLOG`/`READY` → `Start`/`Done`/`Cancel`; `IN_PROGRESS`/`WAITING` →
`Done`/`Cancel`; `DONE`/`CANCELLED` → no hint line at all (nothing
further to do). Never substitute the item's title, notes, or any other
field into the hint. This call **never** writes to `BACKLOG.md`. If the
list is empty, say so in one line (`No matching backlog items.`); do
not invent items.

## Procedure — update commands

If the message matches exactly `Done BL-xxxx`, `Start BL-xxxx`, `Cancel
BL-xxxx`, or `Priority BL-xxxx High/Medium/Low`, run:

```bash
python3 skills/productivity/capture-router/scripts/capture_router.py update "<raw message>"
```

This is the **only** way status/priority changes happen — never infer a
status change from ordinary conversation (e.g. the user saying "I
finished that VAT research" is not, by itself, `Done BL-xxxx`; ask them
to confirm the ID, or use the exact command). Reply with one line
confirming the change, or "No such item: BL-xxxx" if `found` is false.

## Classification (implemented in `scripts/capture_router.py`, not re-judged by the agent)

- **Type** — the recognized prefix maps 1:1 to `IDEA` / `TASK` /
  `RESEARCH` / `DECISION` / `REMINDER` / `PROJECT`.
- **Domain** — deterministic keyword rules, checked in this order (first
  match wins): Tax → Academic → Content → Accounting Automation →
  Training. No match → `General` (not invented as something more
  specific).
- **Project** — deterministic keyword rules matched only against
  projects that currently exist in `PROJECTS.md` (read fresh each time,
  never hardcoded as a static list). No confident match → `UNASSIGNED`.
  **Never invents a new project.**
- **Priority** — `HIGH` on explicit urgency wording (deadline, urgent,
  client-impacting, production issue, …), `LOW` on explicit
  low-pressure wording (exploratory, someday, …), otherwise `MEDIUM`
  (the default for a normal research/action item).
- **Next action** — one concrete line, generated from a fixed template
  per Type (never a multi-step plan).
- **Duplicate detection** — title-word overlap (Jaccard similarity ≥
  0.5) against **open** (non-`DONE`/`CANCELLED`) existing entries only.

## Persistence

v1 uses `BACKLOG.md` only — no SQLite. `PROJECTS.md` is **read-only**:
this skill reads it for the current project list and **never** writes
to it, under any circumstance — updating a project's own status/next
action in `PROJECTS.md` is a separate, manual edit entirely outside
this skill's scope.

`BACKLOG.md` is the intended persistent **mutable** store — that is the
whole point of this skill. Concretely, by default (no explicit
`--projects-path`/`--backlog-path` override):

- The script resolves `$HERMES_HOME/context/PROJECTS.md` (read) and
  `$HERMES_HOME/context/BACKLOG.md` (read + write).
- **In production, `HERMES_HOME=/opt/data`, so this resolves to
  `/opt/data/context/BACKLOG.md` — the canonical, live backlog store.**
  This skill *does* write there in production; that is by design, not
  an oversight.
- The local workspace's `memory/BACKLOG.md` is a **development/mirror/
  seed file only** — it is what this skill was built and tested
  against locally, and what a one-time seed (see the project's
  production-seeding plan, tracked outside this skill) may copy into
  `/opt/data/context/BACKLOG.md` before first production use. It is
  **not** a separate live source of truth, and it is **not**
  automatically synchronized with the production file in either
  direction after that seed — a local edit does not appear in
  production, and a production capture does not appear locally, unless
  someone explicitly pulls/pushes it (the same manual-sync convention
  already used for `PROJECTS.md`/`CURRENT_STATE.md` — see
  `memory/README.md`).

Every write is append-only (new capture) or single-field, full-file
re-render (status/priority update) via an atomic temp-file-then-replace,
so a crash mid-write can never corrupt or truncate existing entries.
IDs are `BL-0001`, `BL-0002`, … — strictly increasing, never reused,
computed from the highest existing ID already in the file.

No path is ever hardcoded to a literal `/opt/data` string in the
script — production behavior falls out entirely from `HERMES_HOME`
already being set to `/opt/data` in that environment, the same
mechanism every other Hermes skill and cron job relies on.

## Verification

- [ ] A message without one of the six exact prefixes was never treated
      as a capture.
- [ ] The confirmation reply matches the exact template — no extra
      prose.
- [ ] A likely duplicate was flagged, not silently created or merged.
- [ ] `PROJECTS.md` was only ever read, never written, by this skill.
- [ ] `BACKLOG.md`'s existing entries (including `BL-0001`) were
      preserved byte-for-byte other than the one intended change.
- [ ] In production the write landed in `/opt/data/context/BACKLOG.md`
      (the canonical store) — this is expected, not a leak. What must
      never happen is a write anywhere else: `PROJECTS.md`, a hardcoded
      path bypassing `HERMES_HOME`, or any file other than `BACKLOG.md`.
