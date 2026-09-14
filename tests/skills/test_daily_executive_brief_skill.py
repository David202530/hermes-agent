"""Tests for the daily-executive-brief skill
(skills/productivity/daily-executive-brief/).

classify() and render() are pure functions -- no network, no filesystem --
so every priority-model and formatting rule is directly testable. The
check_*() gather functions are exercised separately with a monkeypatched
`requests` module to prove they only ever call GET, never POST/mutate.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "productivity" / "daily-executive-brief"
SCRIPT_PATH = SKILL_DIR / "scripts" / "gather_signals.py"


def _load_module():
    name = "daily_executive_brief_gather_signals"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. No critical items
# ---------------------------------------------------------------------------

def test_no_items_produces_fallback_text():
    m = _load_module()
    classified = {"red": [], "orange": [], "automation_health": [], "sources_unknown": []}
    assert m.render(classified) == "No critical actions require your attention today."


# ---------------------------------------------------------------------------
# 2. One RED incident
# ---------------------------------------------------------------------------

def test_one_red_incident_is_classified_and_rendered():
    m = _load_module()
    signals = {
        "hermes_runtime": {"status": "stale"},
        "tax_agent_health": {"status": "healthy"},
        "pending_events": {"status": "ok", "count": 0},
        "cron_failures": {"status": "ok", "failed_jobs": []},
        "project_next_actions": {"status": "ok", "p0_items": []},
    }
    classified = m.classify(signals)
    assert len(classified["red"]) == 1
    assert "heartbeat is stale" in classified["red"][0]["text"]
    assert classified["orange"] == []
    output = m.render(classified)
    assert "🔴 NEEDS ATTENTION" in output
    assert "🟠 IMPORTANT" not in output


# ---------------------------------------------------------------------------
# 2b. Restart-loop file present but NOT tripped -- must not be noise
# (found via manual production execution: restart_loop.json is written on
# every restart-interrupted boot and never cleared by production code, so
# raw file-presence is normal/expected, not an incident by itself).
# ---------------------------------------------------------------------------

def test_restart_loop_present_but_not_tripped_is_not_red():
    m = _load_module()
    # Only 2 boots recorded, well under the trip threshold of 3.
    signals = {"hermes_runtime": {"status": "healthy", "restart_loop_tripped": False}}
    classified = m.classify(signals)
    assert classified["red"] == []


def test_restart_loop_tripped_is_red():
    m = _load_module()
    signals = {"hermes_runtime": {"status": "healthy", "restart_loop_tripped": True}}
    classified = m.classify(signals)
    assert any("restart-loop breaker has tripped" in item["text"] for item in classified["red"])


def test_check_hermes_runtime_health_computes_tripped_from_chain_length(tmp_path):
    m = _load_module()
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "gateway.heartbeat").write_text("", encoding="utf-8")

    now = m.time.time()
    # 3 boots chained tightly together (well inside the 300s gap) -> tripped.
    tripped_boots = [now - 200, now - 100, now]
    (gateway_dir / "restart_loop.json").write_text(
        m.json.dumps({"boots": tripped_boots}), encoding="utf-8"
    )
    result = m.check_hermes_runtime_health(home=tmp_path)
    assert result["restart_loop_tripped"] is True

    # 2 boots, hours apart from "now" -> chain of 1, not tripped (this is
    # the exact shape observed in production: an old, resolved restart).
    stale_boots = [now - 30000, now - 29900]
    (gateway_dir / "restart_loop.json").write_text(
        m.json.dumps({"boots": stale_boots}), encoding="utf-8"
    )
    result = m.check_hermes_runtime_health(home=tmp_path)
    assert result["restart_loop_tripped"] is False


def test_restart_loop_chain_algorithm_matches_the_real_guard_module():
    # Drift guard: gather_signals.py deliberately mirrors
    # gateway/restart_loop_guard.py's constants and chain algorithm rather
    # than importing it (the skill script must stay import-path-agnostic).
    # If the real module's threshold/gap/algorithm ever changes, this test
    # must fail so the mirror gets updated too.
    m = _load_module()
    from gateway import restart_loop_guard as real

    assert m._RESTART_LOOP_MAX_RESTARTS == real.DEFAULT_MAX_RESTARTS
    assert m._RESTART_LOOP_MAX_GAP_SECONDS == real.DEFAULT_MAX_GAP_SECONDS

    now = 1_000_000.0
    scenarios = [
        [],
        [now],
        [now - 50, now],
        [now - 400, now - 200, now],  # first gap (200) > 300? no -- check below
        [now - 200, now - 100, now],
        [now - 30000, now - 29900],
    ]
    for boots in scenarios:
        mirrored = m._restart_loop_chain_length(boots, now)
        real_chain = real._chain_ending_at(boots, now, real._chain_gap(60, real.DEFAULT_MAX_GAP_SECONDS))
        assert mirrored == len(real_chain), (boots, mirrored, len(real_chain))


# ---------------------------------------------------------------------------
# 3. Multiple priority items ordered correctly (RED before ORANGE before AUTOMATION HEALTH)
# ---------------------------------------------------------------------------

def test_multiple_items_render_in_priority_order():
    m = _load_module()
    classified = {
        "red": [{"text": "red item", "next_action": None}],
        "orange": [{"text": "orange item", "next_action": None}],
        "automation_health": [{"text": "automation item", "next_action": None}],
        "sources_unknown": [],
    }
    output = m.render(classified)
    red_pos = output.index("red item")
    orange_pos = output.index("orange item")
    automation_pos = output.index("automation item")
    assert red_pos < orange_pos < automation_pos


# ---------------------------------------------------------------------------
# 4. Maximum seven items
# ---------------------------------------------------------------------------

def test_output_never_exceeds_seven_items():
    m = _load_module()
    classified = {
        "red": [{"text": f"red {i}", "next_action": None} for i in range(4)],
        "orange": [{"text": f"orange {i}", "next_action": None} for i in range(4)],
        "automation_health": [{"text": f"auto {i}", "next_action": None} for i in range(4)],
        "sources_unknown": [],
    }
    output = m.render(classified)
    item_lines = [line for line in output.splitlines() if line.startswith("- ")]
    assert len(item_lines) == 7
    # Priority order preserved even under truncation: all 4 red items kept,
    # then orange fills the remaining 3, automation gets none.
    assert all(f"red {i}" in output for i in range(4))
    assert sum(1 for i in range(4) if f"orange {i}" in output) == 3
    assert not any(f"auto {i}" in output for i in range(4))


# ---------------------------------------------------------------------------
# 5. Healthy systems omitted
# ---------------------------------------------------------------------------

def test_all_healthy_signals_produce_no_items():
    m = _load_module()
    signals = {
        "hermes_runtime": {"status": "healthy"},
        "tax_agent_health": {"status": "healthy"},
        "pending_events": {"status": "ok", "count": 0},
        "cron_failures": {"status": "ok", "failed_jobs": []},
        "project_next_actions": {"status": "ok", "p0_items": []},
    }
    classified = m.classify(signals)
    assert classified["red"] == []
    assert classified["orange"] == []
    assert classified["automation_health"] == []
    assert m.render(classified) == "No critical actions require your attention today."


# ---------------------------------------------------------------------------
# 6. Failed Tax Agent integration surfaced
# ---------------------------------------------------------------------------

def test_unreachable_tax_agent_is_red():
    m = _load_module()
    signals = {"tax_agent_health": {"status": "unreachable"}}
    classified = m.classify(signals)
    assert any("unreachable" in item["text"] for item in classified["red"])


def test_degraded_tax_agent_is_orange_not_red():
    m = _load_module()
    signals = {"tax_agent_health": {"status": "degraded"}}
    classified = m.classify(signals)
    assert classified["red"] == []
    assert any("not fully green" in item["text"] for item in classified["orange"])


# ---------------------------------------------------------------------------
# 7. Pending approval surfaced
# ---------------------------------------------------------------------------

def test_pending_events_count_surfaces_as_orange():
    m = _load_module()
    signals = {"pending_events": {"status": "ok", "count": 2, "event_types": ["DRAFT_READY", "TOPIC_SHORTLIST_READY"]}}
    classified = m.classify(signals)
    assert any("2 Tax Agent event" in item["text"] for item in classified["orange"])


def test_zero_pending_events_surfaces_nothing():
    m = _load_module()
    signals = {"pending_events": {"status": "ok", "count": 0}}
    classified = m.classify(signals)
    assert classified["orange"] == []
    assert classified["red"] == []


# ---------------------------------------------------------------------------
# 8. No external mutation occurs
# ---------------------------------------------------------------------------

def test_gather_functions_never_issue_a_post_request(monkeypatch):
    m = _load_module()
    calls = {"get": 0, "post": 0}

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "ok", "events": []}

    def fake_get(url, headers=None, timeout=None):
        calls["get"] += 1
        return _Resp()

    def fake_post(*a, **kw):
        calls["post"] += 1
        raise AssertionError("gather_signals must never issue a POST request")

    monkeypatch.setattr(m.requests, "get", fake_get)
    monkeypatch.setattr(m.requests, "post", fake_post)

    m.check_tax_agent_health("https://example.test")
    m.check_pending_events("https://example.test", "secret")

    assert calls["post"] == 0
    assert calls["get"] == 3  # /health, /ready, /hermes/pending-events


def test_source_file_contains_no_post_or_write_calls():
    # Static guard: the script's source text itself must never reference
    # requests.post/put/delete or any file-write call.
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "requests.post(" not in source
    assert "requests.put(" not in source
    assert "requests.delete(" not in source
    assert ".write_text(" not in source
    assert "ack-event" not in source.lower()  # never acks a pending event


# ---------------------------------------------------------------------------
# 9. No fabricated source data
# ---------------------------------------------------------------------------

def test_missing_signal_goes_to_sources_unknown_not_fabricated():
    m = _load_module()
    classified = m.classify({})  # every source missing
    assert set(classified["sources_unknown"]) == {
        "hermes_runtime", "tax_agent_health", "pending_events", "cron_failures", "project_next_actions",
    }
    assert classified["red"] == []
    assert classified["orange"] == []


def test_unreachable_tax_agent_health_check_reports_unreachable_not_healthy(monkeypatch):
    m = _load_module()

    def fake_get(*a, **kw):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(m.requests, "get", fake_get)
    result = m.check_tax_agent_health("https://example.test")
    assert result["status"] == "unreachable"
    assert result["status"] != "healthy"


# ---------------------------------------------------------------------------
# 10. Output remains concise
# ---------------------------------------------------------------------------

def test_rendered_item_lines_are_single_line_plus_optional_action():
    m = _load_module()
    classified = {
        "red": [{"text": "Something broke", "next_action": "Do this specific thing"}],
        "orange": [], "automation_health": [], "sources_unknown": [],
    }
    output = m.render(classified)
    assert "- Something broke" in output
    assert "→ Do this specific thing" in output
    # No item line should itself exceed a reasonable single-sentence length.
    for line in output.splitlines():
        if line.startswith("- ") or line.strip().startswith("→"):
            assert len(line) < 200


# ---------------------------------------------------------------------------
# 11. Independent cron/session behavior (source-level guard)
# ---------------------------------------------------------------------------

def test_cron_scheduler_assigns_a_fresh_session_id_per_run():
    # Guards the architectural claim in SKILL.md's "Session design" section:
    # every cron tick gets a uniquely-timestamped session, never a shared
    # or long-running one.
    cron_scheduler = Path(__file__).resolve().parents[2] / "cron" / "scheduler.py"
    source = cron_scheduler.read_text(encoding="utf-8")
    assert 'f"cron_{job_id}_{' in source, (
        "cron/scheduler.py no longer constructs a per-run session id the way "
        "the daily-executive-brief skill's session-isolation claim depends on"
    )


# ---------------------------------------------------------------------------
# 12. [SILENT] is forbidden for this skill (regression: a real production run
# returned "[SILENT]" on a healthy day, following the generic cron-job
# suppression convention, and Telegram received nothing -- see SKILL.md's
# "NEVER return [SILENT] for this skill" section).
# ---------------------------------------------------------------------------

SKILL_MD_PATH = SKILL_DIR / "SKILL.md"


class TestNeverSilent:
    def test_healthy_input_renders_the_exact_fallback_text(self):
        m = _load_module()
        classified = {"red": [], "orange": [], "automation_health": [], "sources_unknown": []}
        assert m.render(classified) == "No critical actions require your attention today."

    def test_render_output_is_never_the_literal_string_silent(self):
        # Across every classify() shape this skill can produce -- empty,
        # one item per bucket, and a full 7-item truncation -- render()
        # must never itself produce the literal token the generic cron
        # convention looks for.
        m = _load_module()
        scenarios = [
            {"red": [], "orange": [], "automation_health": [], "sources_unknown": []},
            {"red": [{"text": "x", "next_action": None}], "orange": [], "automation_health": [], "sources_unknown": []},
            {"red": [], "orange": [{"text": "x", "next_action": None}], "automation_health": [], "sources_unknown": []},
            {"red": [], "orange": [], "automation_health": [{"text": "x", "next_action": None}], "sources_unknown": []},
        ]
        for classified in scenarios:
            output = m.render(classified)
            assert output != "[SILENT]"
            assert "[SILENT]" not in output

    def test_skill_md_explicitly_forbids_silent(self):
        # Static guard: a future prompt/procedure edit to SKILL.md cannot
        # silently drop this rule without failing this test. Checks both
        # the dedicated warning section and the inline reminder at the
        # exact point (Procedure step 3) where the fallback line is chosen.
        source = SKILL_MD_PATH.read_text(encoding="utf-8")
        assert "NEVER return `[SILENT]`" in source
        assert "does **NOT** apply here" in source or "does NOT apply here" in source
        assert "Do **not** respond with `[SILENT]`" in source
        # The rule must appear before the "Sources supported today" section
        # (i.e. near the top of the execution-relevant procedure, not
        # buried at the end of the file).
        silent_rule_pos = source.index("NEVER return `[SILENT]`")
        sources_section_pos = source.index("## Sources supported today")
        assert silent_rule_pos < sources_section_pos, (
            "the no-[SILENT] rule must appear before 'Sources supported "
            "today', i.e. near the top of the skill, not buried at the end"
        )
