---
name: daily-executive-brief
description: "Read-only daily system status brief: Hermes health, Tax Agent bridge health, pending approvals, automation failures."
version: 0.1.0
author: David Mamrikishvili, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Daily-Brief, Chief-of-Staff, Monitoring, Telegram, Cron]
    related_skills: [weekly-review-planning, google-workspace]
---

# Daily Executive Brief

Act as David's AI Chief of Staff for exactly one purpose: surface only what
needs his attention today, from systems Hermes can actually observe right
now. This is not a general assistant prompt — it is a bounded, read-only
status check with a deterministic priority model.

## When to Use

- A scheduled cron tick fires the "Daily Executive Brief" job.
- David asks "give me today's brief" / "what needs my attention" /
  "status check."

Don't use for: weekly planning (`weekly-review-planning`), calendar/email
triage (`google-workspace`'s own `references/daily-brief.md`), or anything
that mutates state. This skill never approves, publishes, sends, or
configures anything — see **Read-only guarantee** below.

## ⚠️ NEVER return `[SILENT]` for this skill

**This is an intentional, scoped exception to the generic cron-job
convention** that lets a job respond with exactly `[SILENT]` to suppress
delivery when "there is genuinely nothing new to report." That generic
instruction does **NOT** apply here, no matter how it is worded in the
prompt that invokes this skill:

- Daily Executive Brief is a scheduled daily check-in David expects to
  receive every single day it runs, healthy or not — silence is
  indistinguishable from the job being broken, so it is never the
  correct response.
- If RED, ORANGE, or AUTOMATION HEALTH items exist: render and deliver
  them per **Output Format** below.
- If none exist: deliver exactly the fallback line —
  `No critical actions require your attention today.` — never
  `[SILENT]`, never an empty response, never nothing.

A prior production run of this exact skill returned `[SILENT]` on a
healthy day (following the generic cron convention) and delivered
nothing to Telegram — confirmed via `agent.log`: `agent returned
[SILENT] — skipping delivery`. This section exists specifically to
prevent that recurring. See `tests/skills/test_daily_executive_brief_skill.py::TestNeverSilent`
for the regression test guarding this rule, and its check that this
section itself still exists in this file.

## Sources supported today

Only sources Hermes can reliably reach without new integrations:

1. **Hermes runtime health** — gateway heartbeat freshness, and whether
   the restart-loop breaker (`gateway/restart_loop_guard.py`) has actually
   *tripped* (an unbroken chain of ≥3 restart-interrupted boots within a
   300s gap of each other) — not merely whether
   `/opt/data/gateway/restart_loop.json` exists. That file is written on
   every restart-interrupted boot and is never cleared by production code
   on a clean shutdown, so its bare presence is normal, expected, and not
   itself news.
2. **Tax Agent integration health** — `GET /health`, `GET /ready` against
   `TAX_AGENT_BRIDGE_URL` (the same bridge config the poller already uses;
   no new credentials).
3. **Pending Tax Agent / content approval items** — authenticated
   `GET /hermes/pending-events` (the same call the poller makes).
4. **Cron job failures** — best-effort read of `/opt/data/cron/jobs.json`
   if present.
5. **Existing captured/project next-actions** — best-effort read of
   `context/CURRENT_STATE.md`'s "Immediate next actions" table, P0/P1 rows
   only, if the file exists.

**Explicitly NOT supported yet** (do not fabricate these): Calendar, Gmail,
Drive, or any other integration not already connected to this Hermes
instance. If a source is unreachable or unconfigured, the script reports
`"status": "unknown"` for it — never guess, never invent a plausible-
sounding status.

Adding a new source later means adding one more `check_*()` function to
`scripts/gather_signals.py` and one more entry in `classify()`'s input —
the procedure below and the output format do not change.

## Procedure

1. Run the gathering + classification script (single, self-contained,
   read-only):

   ```bash
   python3 skills/productivity/daily-executive-brief/scripts/gather_signals.py
   ```

   This prints one JSON object: `{"red": [...], "orange": [...],
   "automation_health": [...], "sources_unknown": [...]}`. Each item is
   `{"text": "...", "next_action": "..." | null}`. This step is entirely
   deterministic — do not re-derive priorities yourself; the script has
   already applied the priority model in **Priority Model** below.

2. Render the JSON into the **Output Format** below. Truncate to the 7
   highest-priority items total (RED first, then ORANGE, then
   AUTOMATION HEALTH), preserving the script's own ordering within each
   bucket. If a bucket is empty, omit its heading entirely.

3. If every bucket is empty, output exactly:

   ```
   No critical actions require your attention today.
   ```

   Do **not** respond with `[SILENT]` here or anywhere else in this
   skill — see **⚠️ NEVER return `[SILENT]` for this skill** above. An
   empty brief is not "nothing to report"; it is a normal, expected,
   positive result David should still see delivered.

4. Deliver via whatever channel invoked this skill (Telegram for the
   scheduled job; the calling conversation otherwise). Do not deliver
   anywhere else, and do not take any action based on what the brief
   contains — this skill only reports.

## Output Format

```
DAILY EXECUTIVE BRIEF

🔴 NEEDS ATTENTION
<only items requiring action or a decision>

🟠 IMPORTANT / UPCOMING
<important but not urgent>

🤖 AUTOMATION HEALTH
<only degraded or noteworthy systems — omit if everything is healthy>

✅ NO ACTION REQUIRED
<optional single line, only if every section above is empty or near-empty>
```

Rules:
- Maximum 7 meaningful items total, highest priority first.
- No long explanations, no generic motivational text, no invented tasks,
  no raw log dumps.
- Include a concrete recommended next action where the script provided
  one (`next_action`); omit the line otherwise.
- Do not repeat an unchanged, already-known-healthy status as if it were
  news — healthy/routine items are omitted by the classifier already, not
  just hidden by formatting.

## Priority Model (implemented in `scripts/gather_signals.py`, not re-judged by the agent)

**RED** — production failure, action waiting on David, a deadline/action
due today, a failed approval/workflow, or a security/data-integrity
concern.

**ORANGE** — an upcoming decision, a degraded-but-functioning automation,
a pending non-urgent approval, or an important project item.

**GREEN / OMIT** — healthy normal polling, routine cron success, unchanged
status, or informational noise. These never reach the rendered brief.

## Read-only guarantee

This skill may only **observe**: runtime status, existing cron/job state,
pending bridge events (via `GET`), and already-maintained project/context
files. It must **never**: approve or act on a Tax Agent action, send an
email, publish to LinkedIn, mutate Tax Agent state, change a cron job,
restart a service, or change Railway configuration. `gather_signals.py`
makes HTTP `GET` requests only and reads local files read-only — it has no
write path, no `POST`, no subprocess that mutates anything.

## Session design

Scheduled runs of this skill use Hermes's own cron session model: every
cron tick gets a freshly-named session
(`cron_<job_id>_<YYYYMMDD_HHMMSS>`, see `cron/scheduler.py`), never the
long-running conversational session. Do not invoke this skill from inside
a long-lived chat session for the scheduled job — only the cron trigger
should run it that way, precisely so daily runs never accumulate into an
ever-growing transcript the way the general conversation did.

## Verification

- [ ] Every item in the rendered brief traces to a real value from
      `gather_signals.py`'s output — nothing was added by the formatting
      step.
- [ ] No Calendar/Gmail/Drive content appears unless those integrations
      are actually connected.
- [ ] No action was taken — this run only printed/delivered text.
- [ ] Total rendered items ≤ 7.
- [ ] If nothing needed attention, the exact fallback line was used.
- [ ] The response was never `[SILENT]` — this skill always delivers
      either the itemized brief or the fallback line.
