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

Render the returned entries as a short list (ID, Title, Priority,
Status — one line each). This call **never** writes to `BACKLOG.md`.
If the list is empty, say so in one line; do not invent items.

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

v1 uses `BACKLOG.md` only — no SQLite. `PROJECTS.md` is **read** for the
current project list (never written by this skill except when the user
explicitly asks to update project-level metadata, which is a separate,
manual edit outside this skill's scope — this skill only appends/updates
items in `BACKLOG.md`).

Every write is append-only (new capture) or single-field, full-file
re-render (status/priority update) via an atomic temp-file-then-replace,
so a crash mid-write can never corrupt or truncate existing entries.
IDs are `BL-0001`, `BL-0002`, … — strictly increasing, never reused,
computed from the highest existing ID already in the file.

**This skill never writes to `/opt/data/context/` in this phase.**
Paths are explicit arguments/derived from `HERMES_HOME` — sync to the
deployed context store is a deliberate future step, not automatic.

## Verification

- [ ] A message without one of the six exact prefixes was never treated
      as a capture.
- [ ] The confirmation reply matches the exact template — no extra
      prose.
- [ ] A likely duplicate was flagged, not silently created or merged.
- [ ] `PROJECTS.md` was only ever read, never written, by this skill.
- [ ] `BACKLOG.md`'s existing entries (including `BL-0001`) were
      preserved byte-for-byte other than the one intended change.
- [ ] No write touched `/opt/data/context/`.
