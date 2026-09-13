#!/usr/bin/env python3
"""gather_signals.py -- read-only data collection + deterministic priority
classification for the daily-executive-brief skill.

Every check_*() function is read-only: HTTP GET only, local files opened
for reading only. None of them write, POST, approve, publish, restart, or
otherwise mutate anything. A check that fails or is unconfigured reports
status "unknown" -- it never fabricates a plausible-looking result.

classify() and render() are pure functions (no I/O) so the priority model
and output formatting are unit-testable without a network or filesystem.
main() is the only impure entry point, and it only reads and prints.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - requests is a core Hermes dependency
    requests = None  # type: ignore[assignment]

_TIMEOUT_SECONDS = 10
_HEARTBEAT_STALE_AFTER_SECONDS = 180


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home())))


# ---------------------------------------------------------------------------
# Source 1: Hermes runtime health (local files, read-only)
# ---------------------------------------------------------------------------

# Mirrors gateway/restart_loop_guard.py's own defaults and chain algorithm.
# That module documents explicitly that restart_loop.json is written on
# every restart-interrupted boot and is *never* cleared by production code
# on a clean shutdown (only tests call its clear()) -- so the file's mere
# presence is normal, expected, persistent state, not an incident. The
# guard itself only calls the loop "tripped" once an unbroken chain of
# boots (successive gaps <= _RESTART_LOOP_MAX_GAP_SECONDS) reaches
# _RESTART_LOOP_MAX_RESTARTS. Treating file-presence alone as RED is a
# false positive: it would fire on every ordinary redeploy (which writes
# 1-2 boots) forever, since nothing ever deletes the file. See
# test_daily_executive_brief_skill.py's cross-check against the real
# gateway.restart_loop_guard constants/algorithm for drift protection.
_RESTART_LOOP_MAX_RESTARTS = 3
_RESTART_LOOP_MAX_GAP_SECONDS = 300


def _restart_loop_chain_length(boots: list, now: float) -> int:
    """Length of the unbroken chain of boots ending at `now`.

    Same algorithm as gateway.restart_loop_guard._chain_ending_at: walk
    backwards from `now`, keep boots while each successive gap stays
    within _RESTART_LOOP_MAX_GAP_SECONDS; the first wider gap ends the
    chain (older boots belong to a prior, already-resolved episode).
    """
    chain = []
    prev = now
    for t in sorted(boots, reverse=True):
        if t > now:
            chain.append(t)
            continue
        if prev - t > _RESTART_LOOP_MAX_GAP_SECONDS:
            break
        chain.append(t)
        prev = t
    return len(chain)


def check_hermes_runtime_health(home: Optional[Path] = None) -> dict:
    """Gateway heartbeat freshness + *tripped* restart-loop state.

    Reports `restart_loop_tripped` (bool), not raw file presence -- see
    the module-level comment above `_RESTART_LOOP_MAX_RESTARTS`.
    """
    home = home or _hermes_home()
    heartbeat_path = home / "state" / "gateway.heartbeat"
    restart_loop_path = home / "gateway" / "restart_loop.json"

    result: dict[str, Any] = {"status": "unknown"}

    try:
        age_seconds = time.time() - heartbeat_path.stat().st_mtime
        result["heartbeat_age_seconds"] = round(age_seconds, 1)
        result["status"] = "healthy" if age_seconds <= _HEARTBEAT_STALE_AFTER_SECONDS else "stale"
    except OSError:
        result["status"] = "unknown"

    try:
        loop_data = json.loads(restart_loop_path.read_text(encoding="utf-8"))
        boots = [float(t) for t in loop_data.get("boots", []) if isinstance(t, (int, float))]
        chain_length = _restart_loop_chain_length(boots, time.time())
        result["restart_loop_tripped"] = chain_length >= _RESTART_LOOP_MAX_RESTARTS
        result["restart_loop_chain_length"] = chain_length
    except (OSError, ValueError):
        # No file, or unreadable -- fails open, same as the guard itself.
        result["restart_loop_tripped"] = False

    return result


# ---------------------------------------------------------------------------
# Source 2 + 3: Tax Agent integration health + pending approval items (HTTP GET only)
# ---------------------------------------------------------------------------

def check_tax_agent_health(base_url: str, timeout: int = _TIMEOUT_SECONDS) -> dict:
    """GET /health and GET /ready. Read-only."""
    if not base_url or requests is None:
        return {"status": "unknown"}
    result: dict[str, Any] = {"status": "unknown"}
    try:
        health = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout)
        ready = requests.get(f"{base_url.rstrip('/')}/ready", timeout=timeout)
        result["health_status_code"] = health.status_code
        result["ready_status_code"] = ready.status_code
        healthy = health.status_code == 200 and ready.status_code == 200
        result["status"] = "healthy" if healthy else "degraded"
        if ready.status_code == 200:
            try:
                result["ready_body"] = ready.json()
            except ValueError:
                pass
    except Exception as exc:  # network error, timeout, DNS, etc.
        result["status"] = "unreachable"
        result["error"] = str(exc)[:200]
    return result


def check_pending_events(base_url: str, shared_secret: str, timeout: int = _TIMEOUT_SECONDS) -> dict:
    """Authenticated GET /hermes/pending-events. Read-only -- never acks."""
    if not base_url or not shared_secret or requests is None:
        return {"status": "unknown", "count": 0}
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/hermes/pending-events",
            headers={"Authorization": f"Bearer {shared_secret}"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return {"status": "error", "count": 0, "http_status": response.status_code}
        events = response.json().get("events", [])
        return {"status": "ok", "count": len(events), "event_types": [e.get("event_type") for e in events]}
    except Exception as exc:
        return {"status": "unreachable", "count": 0, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Source 4: cron job failures (local file, best-effort, read-only)
# ---------------------------------------------------------------------------

def check_cron_failures(home: Optional[Path] = None) -> dict:
    home = home or _hermes_home()
    jobs_path = home / "cron" / "jobs.json"
    try:
        jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unknown", "failed_jobs": []}

    if not isinstance(jobs, list):
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else []

    failed = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        last_status = str(job.get("last_status", "")).lower()
        if last_status in ("failed", "error"):
            failed.append({"name": job.get("name") or job.get("id"), "last_status": last_status})
    return {"status": "ok", "failed_jobs": failed}


# ---------------------------------------------------------------------------
# Source 5: existing project next-actions (best-effort, read-only)
# ---------------------------------------------------------------------------

def check_project_next_actions(home: Optional[Path] = None) -> dict:
    home = home or _hermes_home()
    path = home / "context" / "CURRENT_STATE.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "unknown", "p0_items": []}

    p0_items = []
    for line in text.splitlines():
        if "| P0 |" in line or line.strip().startswith("| 1 |") and "P0" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                p0_items.append(cells[1] if len(cells) > 1 else line.strip())
    return {"status": "ok", "p0_items": p0_items}


# ---------------------------------------------------------------------------
# Deterministic classification -- pure function, fully unit-testable
# ---------------------------------------------------------------------------

def classify(signals: dict) -> dict:
    """Applies the RED/ORANGE/GREEN priority model to gathered signals.

    Input shape: {"hermes_runtime": {...}, "tax_agent_health": {...},
    "pending_events": {...}, "cron_failures": {...}, "project_next_actions": {...}}
    (any key may be omitted; missing keys are treated as unknown/skip).

    Output: {"red": [item, ...], "orange": [item, ...],
    "automation_health": [item, ...], "sources_unknown": [name, ...]}
    where each item is {"text": str, "next_action": str | None}.
    """
    red: list[dict] = []
    orange: list[dict] = []
    automation_health: list[dict] = []
    sources_unknown: list[str] = []

    runtime = signals.get("hermes_runtime")
    if runtime is None or runtime.get("status") == "unknown":
        sources_unknown.append("hermes_runtime")
    elif runtime.get("status") == "stale":
        red.append({
            "text": "Hermes gateway heartbeat is stale — the process may be stuck or down.",
            "next_action": "Check Railway deploy logs for the hermes-agent service.",
        })
    if (runtime or {}).get("restart_loop_tripped"):
        red.append({
            "text": "Gateway restart-loop breaker has tripped — repeated crash/respawn cycle detected.",
            "next_action": "Inspect /opt/data/gateway/restart_loop.json and recent deploy logs.",
        })

    tax_health = signals.get("tax_agent_health")
    if tax_health is None or tax_health.get("status") == "unknown":
        sources_unknown.append("tax_agent_health")
    elif tax_health.get("status") == "unreachable":
        red.append({
            "text": "Tax Agent bridge is unreachable — content pipeline may be blocked.",
            "next_action": "Check the Tax Agent Railway service status.",
        })
    elif tax_health.get("status") == "degraded":
        orange.append({
            "text": "Tax Agent /health or /ready is not fully green.",
            "next_action": "Review Tax Agent's /ready response for the failing check.",
        })

    pending = signals.get("pending_events")
    if pending is None or pending.get("status") == "unknown":
        sources_unknown.append("pending_events")
    elif pending.get("status") == "unreachable":
        red.append({
            "text": "Could not reach Tax Agent to check pending approval items.",
            "next_action": "Verify TAX_AGENT_BRIDGE_URL and the bridge secret are still valid.",
        })
    elif pending.get("count", 0) > 0:
        orange.append({
            "text": f"{pending['count']} Tax Agent event(s) awaiting delivery/approval.",
            "next_action": "Review the pending items in Telegram.",
        })

    cron = signals.get("cron_failures")
    if cron is None or cron.get("status") == "unknown":
        sources_unknown.append("cron_failures")
    else:
        for job in cron.get("failed_jobs", []):
            automation_health.append({
                "text": f"Cron job '{job.get('name')}' last run failed.",
                "next_action": "Run `hermes cron history <job_id>` to see the failure.",
            })

    projects = signals.get("project_next_actions")
    if projects is None or projects.get("status") == "unknown":
        sources_unknown.append("project_next_actions")
    else:
        for item in projects.get("p0_items", []):
            orange.append({"text": f"P0 project item open: {item}", "next_action": None})

    return {
        "red": red,
        "orange": orange,
        "automation_health": automation_health,
        "sources_unknown": sources_unknown,
    }


# ---------------------------------------------------------------------------
# Rendering -- pure function, fully unit-testable
# ---------------------------------------------------------------------------

_MAX_ITEMS = 7
_FALLBACK_TEXT = "No critical actions require your attention today."


def render(classified: dict) -> str:
    red = classified.get("red", [])
    orange = classified.get("orange", [])
    automation_health = classified.get("automation_health", [])

    if not red and not orange and not automation_health:
        return _FALLBACK_TEXT

    lines = ["DAILY EXECUTIVE BRIEF", ""]
    remaining = _MAX_ITEMS

    def _emit_section(title: str, items: list[dict]) -> None:
        nonlocal remaining
        if not items or remaining <= 0:
            return
        lines.append(title)
        for item in items[:remaining]:
            line = f"- {item['text']}"
            lines.append(line)
            if item.get("next_action"):
                lines.append(f"  → {item['next_action']}")
            remaining -= 1
        lines.append("")

    _emit_section("🔴 NEEDS ATTENTION", red)
    _emit_section("🟠 IMPORTANT / UPCOMING", orange)
    _emit_section("🤖 AUTOMATION HEALTH", automation_health)

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Entry point -- the only impure function
# ---------------------------------------------------------------------------

def main() -> int:
    base_url = os.environ.get("TAX_AGENT_BRIDGE_URL", "")
    shared_secret = os.environ.get("TAX_AGENT_BRIDGE_SHARED_SECRET", "")

    signals = {
        "hermes_runtime": check_hermes_runtime_health(),
        "tax_agent_health": check_tax_agent_health(base_url),
        "pending_events": check_pending_events(base_url, shared_secret),
        "cron_failures": check_cron_failures(),
        "project_next_actions": check_project_next_actions(),
    }
    classified = classify(signals)
    print(json.dumps(classified, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
