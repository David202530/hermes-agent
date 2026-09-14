"""Tests for the capture-router skill
(skills/productivity/capture-router/).

All classification functions (parse_prefix, classify_domain,
classify_priority, match_project, generate_next_action, find_duplicate)
are pure -- no filesystem, no network -- so every rule is directly
testable. capture()/update_entry_field() are exercised against tmp_path
fixtures only; no test ever touches the real memory/ files or
/opt/data/context.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "productivity" / "capture-router"
SCRIPT_PATH = SKILL_DIR / "scripts" / "capture_router.py"

_PROJECTS_MD_FIXTURE = """# PROJECTS

| # | Project | Priority | Status | Next action |
|---|---|---|---|---|
| 1 | Hermes Personal AI Infrastructure | P1 | Active development | Verify hybrid memory end-to-end via Telegram |
| 2 | Accounting Mapping & Tax Automation | P1 | Architecture / development | Define transaction taxonomy + rule-engine schema |
| 3 | Tax Content Intelligence Agent | P1 | Writer component in development | Define Writer input/output contract |
| 4 | Professional Personal Brand | P1 | Ongoing | Set a sustainable publishing cadence |
| 5 | VAT / EU Alignment Research | P1 | Research / article development | Complete the concordance table |
| 6 | Tax Ruling Assistant | P2 | Design | Build the ruling knowledge base |
"""

_BACKLOG_MD_FIXTURE = """# BACKLOG

---

### BL-0001
- Type: RESEARCH
- Domain: Tax
- Project: VAT / EU Alignment Research
- Title: Research ECJ cases on corporate events VAT
- Priority: MEDIUM
- Status: BACKLOG
- Next action: Research relevant ECJ cases
- Created: 2026-09-14
- Source: Telegram
"""


def _load_module():
    name = "capture_router_module"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def projects_path(tmp_path):
    p = tmp_path / "PROJECTS.md"
    p.write_text(_PROJECTS_MD_FIXTURE, encoding="utf-8")
    return p


@pytest.fixture
def backlog_path(tmp_path):
    p = tmp_path / "BACKLOG.md"
    p.write_text(_BACKLOG_MD_FIXTURE, encoding="utf-8")
    return p


@pytest.fixture
def empty_backlog_path(tmp_path):
    return tmp_path / "BACKLOG.md"


# ---------------------------------------------------------------------------
# 1-4. Capture by type
# ---------------------------------------------------------------------------

def test_idea_capture(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Idea: Build a monthly tax risk dashboard for Nexia clients",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["matched"] is True
    assert result["duplicate"] is False
    assert result["entry"]["type"] == "IDEA"
    assert result["entry"]["id"] == "BL-0001"


def test_task_capture(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Task: Update the VAT training material with the latest business-transfer guidance",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["entry"]["type"] == "TASK"
    # TASK next_action reuses the title verbatim (already action-shaped).
    assert result["entry"]["next_action"] == result["entry"]["title"]


def test_research_capture(backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Research: find ECJ cases on employee entertainment",
        projects_path=projects_path, backlog_path=backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["matched"] is True
    assert result["entry"]["type"] == "RESEARCH"
    assert "Find and summarize relevant sources" in result["entry"]["next_action"]


def test_decision_capture(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Decision: use Hermes as the approval layer for Tax Agent",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["entry"]["type"] == "DECISION"
    assert result["entry"]["project"] == "Tax Content Intelligence Agent"


# ---------------------------------------------------------------------------
# 5. Domain classification
# ---------------------------------------------------------------------------

def test_domain_classification():
    m = _load_module()
    assert m.classify_domain("VAT concordance table") == "Tax"
    assert m.classify_domain("METEOR Horizon Europe article") == "Academic"
    assert m.classify_domain("LinkedIn post scheduling") == "Content"
    assert m.classify_domain("accounting mapping transaction taxonomy") == "Accounting Automation"
    assert m.classify_domain("update training slides for the workshop") == "Training"
    assert m.classify_domain("something with no keyword match at all") == "General"


def test_domain_classification_tax_checked_before_training_on_ambiguous_text():
    # Deliberate, documented behavior: Tax is checked first, so a message
    # that could plausibly match more than one domain (VAT + training)
    # resolves to Tax, not Training -- deterministic, not a coin flip.
    m = _load_module()
    assert m.classify_domain("Update the VAT training material") == "Tax"


# ---------------------------------------------------------------------------
# 6. Existing project match
# ---------------------------------------------------------------------------

def test_existing_project_match(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Research: Find ECJ cases on corporate events VAT concordance",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["entry"]["project"] == "VAT / EU Alignment Research"


def test_accounting_project_match():
    m = _load_module()
    known = ["Accounting Mapping & Tax Automation", "VAT / EU Alignment Research"]
    assert m.match_project("Accounting transaction taxonomy", known) == "Accounting Mapping & Tax Automation"


# ---------------------------------------------------------------------------
# 7. Unassigned project
# ---------------------------------------------------------------------------

def test_unassigned_project_when_uncertain(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Idea: Build a monthly tax risk dashboard for Nexia clients",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["entry"]["project"] == "UNASSIGNED"


def test_match_project_never_invents_a_project():
    m = _load_module()
    # Even a strong keyword hit must not match a project absent from the
    # known list read from PROJECTS.md (e.g. renamed/removed project).
    assert m.match_project("ecj concordance", known_projects=[]) == "UNASSIGNED"


# ---------------------------------------------------------------------------
# 8. Stable ID increment
# ---------------------------------------------------------------------------

def test_stable_id_increment(backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Idea: something entirely new and unrelated",
        projects_path=projects_path, backlog_path=backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["entry"]["id"] == "BL-0002"

    result2 = m.capture(
        "Idea: yet another distinct idea about something else",
        projects_path=projects_path, backlog_path=backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result2["entry"]["id"] == "BL-0003"


# ---------------------------------------------------------------------------
# 9. Existing BL-0001 preserved
# ---------------------------------------------------------------------------

def test_existing_bl0001_preserved_after_new_capture(backlog_path, projects_path):
    m = _load_module()
    m.capture(
        "Idea: something entirely new and unrelated",
        projects_path=projects_path, backlog_path=backlog_path,
        created="2026-09-14", source="Telegram",
    )
    entries = m.parse_backlog(backlog_path.read_text(encoding="utf-8"))
    bl0001 = next(e for e in entries if e.id == "BL-0001")
    assert bl0001.title == "Research ECJ cases on corporate events VAT"
    assert bl0001.status == "BACKLOG"
    assert bl0001.priority == "MEDIUM"
    assert bl0001.project == "VAT / EU Alignment Research"


def test_existing_bl0001_preserved_after_status_update(backlog_path):
    m = _load_module()
    m.update_entry_field(backlog_path, "BL-0001", "status", "DONE")
    entries = m.parse_backlog(backlog_path.read_text(encoding="utf-8"))
    bl0001 = next(e for e in entries if e.id == "BL-0001")
    assert bl0001.status == "DONE"
    # every other field must be untouched
    assert bl0001.title == "Research ECJ cases on corporate events VAT"
    assert bl0001.priority == "MEDIUM"


# ---------------------------------------------------------------------------
# 10. Duplicate detection
# ---------------------------------------------------------------------------

def test_duplicate_detection_flags_similar_title(backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "Research: Find ECJ cases on corporate events VAT",
        projects_path=projects_path, backlog_path=backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result["matched"] is True
    assert result["duplicate"] is True
    assert result["duplicate_of"] == "BL-0001"
    # a flagged duplicate must NOT be persisted
    entries = m.parse_backlog(backlog_path.read_text(encoding="utf-8"))
    assert len(entries) == 1


def test_duplicate_detection_ignores_done_items():
    m = _load_module()
    done_entry = m.BacklogEntry(
        id="BL-0001", type="RESEARCH", domain="Tax", project="X",
        title="Research ECJ cases on corporate events VAT", priority="MEDIUM",
        status="DONE", next_action="x", created="2026-09-14", source="Telegram",
    )
    dup = m.find_duplicate("Research ECJ cases on corporate events VAT", [done_entry])
    assert dup is None


# ---------------------------------------------------------------------------
# 11. High-priority classification
# ---------------------------------------------------------------------------

def test_high_priority_classification():
    m = _load_module()
    assert m.classify_priority("Task: fix this urgent client-impacting issue") == "HIGH"
    assert m.classify_priority("Idea: something exploratory, no rush at all") == "LOW"
    assert m.classify_priority("Research: find ECJ cases") == "MEDIUM"


# ---------------------------------------------------------------------------
# 12. Normal message ignored by Capture Router
# ---------------------------------------------------------------------------

def test_normal_conversational_message_is_not_matched():
    m = _load_module()
    assert m.parse_prefix("How is the Tax Agent poller doing today?") is None
    assert m.parse_prefix("Can you check the daily brief?") is None
    assert m.parse_prefix("") is None


def test_capture_returns_unmatched_for_normal_message(empty_backlog_path, projects_path):
    m = _load_module()
    result = m.capture(
        "How is the Tax Agent poller doing today?",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    assert result == {"matched": False}
    # nothing should have been written
    assert not empty_backlog_path.exists()


# ---------------------------------------------------------------------------
# 13. Backlog query read-only
# ---------------------------------------------------------------------------

def test_backlog_query_is_read_only(backlog_path):
    m = _load_module()
    before = backlog_path.read_text(encoding="utf-8")
    entries = m.parse_backlog(before)
    filters = m.parse_query("Backlog")
    assert filters == {}
    matched = m.apply_query(entries, filters)
    assert len(matched) == 1
    after = backlog_path.read_text(encoding="utf-8")
    assert before == after


def test_query_priority_and_domain_filters(backlog_path):
    m = _load_module()
    entries = m.parse_backlog(backlog_path.read_text(encoding="utf-8"))
    assert m.parse_query("Show HIGH priority") == {"priority": "HIGH"}
    high = m.apply_query(entries, {"priority": "HIGH"})
    assert high == []
    medium = m.apply_query(entries, {"priority": "MEDIUM"})
    assert len(medium) == 1


def test_query_for_project():
    m = _load_module()
    assert m.parse_query("Show items for VAT / EU Alignment Research") == {
        "project": "VAT / EU Alignment Research"
    }


def test_unrecognized_text_is_not_a_query():
    m = _load_module()
    assert m.parse_query("What's the weather like") is None


# ---------------------------------------------------------------------------
# 14. Done/status update
# ---------------------------------------------------------------------------

def test_done_command_updates_status(backlog_path):
    m = _load_module()
    parsed = m.parse_update_command("Done BL-0001")
    assert parsed == ("BL-0001", "status", "DONE")
    updated = m.update_entry_field(backlog_path, *parsed)
    assert updated.status == "DONE"


def test_start_and_cancel_commands():
    m = _load_module()
    assert m.parse_update_command("Start BL-0002") == ("BL-0002", "status", "IN_PROGRESS")
    assert m.parse_update_command("Cancel BL-0002") == ("BL-0002", "status", "CANCELLED")


def test_priority_update_command(backlog_path):
    m = _load_module()
    parsed = m.parse_update_command("Priority BL-0001 High")
    assert parsed == ("BL-0001", "priority", "HIGH")
    updated = m.update_entry_field(backlog_path, *parsed)
    assert updated.priority == "HIGH"


def test_update_command_for_missing_id_returns_none(backlog_path):
    m = _load_module()
    result = m.update_entry_field(backlog_path, "BL-9999", "status", "DONE")
    assert result is None


def test_update_command_never_inferred_from_plain_conversation():
    m = _load_module()
    assert m.parse_update_command("I finished that VAT research") is None
    assert m.parse_update_command("that's done now") is None


# ---------------------------------------------------------------------------
# 15. No PROJECTS.md item-level mutation
# ---------------------------------------------------------------------------

def test_capture_never_writes_to_projects_md(empty_backlog_path, projects_path):
    m = _load_module()
    before = projects_path.read_text(encoding="utf-8")
    m.capture(
        "Idea: Build a monthly tax risk dashboard for Nexia clients",
        projects_path=projects_path, backlog_path=empty_backlog_path,
        created="2026-09-14", source="Telegram",
    )
    after = projects_path.read_text(encoding="utf-8")
    assert before == after


def test_source_file_never_writes_projects_md():
    # Static guard: the script must contain no write call against a
    # variable/path named "projects" anywhere.
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "projects_path.write_text" not in source
    assert "load_known_projects" in source  # confirms it IS read, just never written


# ---------------------------------------------------------------------------
# 16. No /opt/data/context mutation
# ---------------------------------------------------------------------------

def test_source_file_never_hardcodes_opt_data_as_a_path_default():
    # The docstring/comments may mention /opt/data/context as prose
    # explanation of the deployed-side convention -- that's documentation,
    # not a mutation risk. What must never exist is an actual hardcoded
    # Path(...) construction pointing there as a live default.
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'Path("/opt/data' not in source
    assert "Path('/opt/data" not in source


def test_default_paths_are_derived_not_hardcoded_to_opt_data():
    # main()'s defaults must be derived from HERMES_HOME, not a literal
    # /opt/data path, so tests/dev runs never accidentally target it.
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '_default_home()' in source
    assert 'os.environ.get("HERMES_HOME"' in source


# ---------------------------------------------------------------------------
# 17. Concise confirmation response
# ---------------------------------------------------------------------------

def test_confirmation_response_is_concise_and_exact_format():
    m = _load_module()
    entry = {
        "id": "BL-0002", "type": "research", "domain": "Tax",
        "project": "VAT / EU Alignment Research", "priority": "medium",
        "next_action": "Find relevant ECJ cases",
    }
    rendered = m.render_confirmation(entry)
    assert rendered == (
        "Captured as BL-0002\n\n"
        "Type: Research\n"
        "Domain: Tax\n"
        "Project: VAT / EU Alignment Research\n"
        "Priority: Medium\n"
        "Next action: Find relevant ECJ cases"
    )
    # concise: no more than 6 non-blank lines (one blank separator line
    # after the header is part of the documented exact template).
    non_blank = [line for line in rendered.splitlines() if line.strip()]
    assert len(non_blank) <= 6


def test_duplicate_notice_is_concise():
    m = _load_module()
    notice = m.render_duplicate_notice("BL-0001")
    assert notice.startswith("Possible duplicate of BL-0001")
    assert len(notice.splitlines()) == 1
