#!/usr/bin/env python3
"""capture_router.py -- deterministic classification + persistence for the
capture-router skill.

Every classification decision (type, domain, project, priority, next
action, duplicate detection) is a PURE function -- no network, no
filesystem -- so the rules are fully unit-testable and the agent never
has to re-derive them. Only `append_entry`, `update_entry`, and the
`load_*`/`main` functions touch the filesystem, and none of them ever
touch anything under `/opt/data/context` in this phase -- callers pass
explicit paths (see SKILL.md's "Persistence" section).

v1 scope, deliberately: explicit-prefix capture only (Idea:/Task:/
Research:/Decision:/Reminder:/Project:). `parse_prefix()` returns None
for any message that does not start with one of these six prefixes, and
the caller (the agent, guided by SKILL.md) must treat a None result as
"not a capture -- continue the normal conversation." This script never
classifies arbitrary conversational text.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data model (Phase 2B, Step 2)
# ---------------------------------------------------------------------------

TYPES = ("IDEA", "TASK", "RESEARCH", "DECISION", "REMINDER", "PROJECT")
STATUSES = ("BACKLOG", "READY", "IN_PROGRESS", "WAITING", "DONE", "CANCELLED")
PRIORITIES = ("HIGH", "MEDIUM", "LOW")
_OPEN_STATUSES = {"BACKLOG", "READY", "IN_PROGRESS", "WAITING"}

_PREFIX_TO_TYPE = {
    "idea": "IDEA",
    "task": "TASK",
    "research": "RESEARCH",
    "decision": "DECISION",
    "reminder": "REMINDER",
    "project": "PROJECT",
}

_ENTRY_FIELDS = (
    "id", "type", "domain", "project", "title", "priority", "status",
    "next_action", "created", "source", "notes",
)


@dataclass
class BacklogEntry:
    id: str
    type: str
    domain: str
    project: str
    title: str
    priority: str
    status: str
    next_action: str
    created: str
    source: str
    notes: str = ""


# ---------------------------------------------------------------------------
# Step 1 companion: prefix recognition (safest integration point)
# ---------------------------------------------------------------------------
#
# gateway/run.py already has a plugin hook, `pre_gateway_dispatch`, that runs
# before every inbound message is dispatched. tax_content_bridge's own
# docstring records that this exact hook was tried first and abandoned:
# "hook delivery itself was unreliable even though the plugin code was
# correct" -- it was replaced with `register_tool`, an agent-mediated path.
# capture-router follows the same lesson: instead of a hard interception
# hook, this is a normal skill. The conversational agent (guided by
# SKILL.md's description and the explicit-prefix rule below) decides
# whether an inbound message is a capture, exactly the same way it already
# decides whether to invoke daily-executive-brief or any other skill. This
# never touches gateway/run.py's message routing, so it cannot regress
# normal conversation and carries none of pre_gateway_dispatch's known
# reliability risk.

_PREFIX_RE = re.compile(
    r"^\s*(idea|task|research|decision|reminder|project)\s*:\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def parse_prefix(text: str) -> Optional[tuple[str, str]]:
    """Return (TYPE, title) if `text` starts with a recognized capture
    prefix, else None. Deliberately conservative: only the six literal
    prefixes trigger a capture (Step 1 -- "prefer explicit prefixes
    first... do not try to classify every message automatically in v1")."""
    match = _PREFIX_RE.match(text or "")
    if not match:
        return None
    prefix_word, rest = match.group(1), match.group(2)
    title = " ".join(rest.split()).strip()
    if not title:
        return None
    return _PREFIX_TO_TYPE[prefix_word.lower()], title


# ---------------------------------------------------------------------------
# Step 3: deterministic classification
# ---------------------------------------------------------------------------

def _contains_keyword(lowered_text: str, keyword: str) -> bool:
    """Word-boundary match, not plain substring -- a bare `in` check would
    match "tax" inside "taxonomy" or "vat" inside "elevator". `\\b` treats
    the space in a multi-word phrase like "transfer pricing" as a normal
    boundary-safe literal, so phrases work the same way as single words."""
    return re.search(r"\b" + re.escape(keyword) + r"\b", lowered_text) is not None


# Checked in order; first match wins. Order matches the sequence the
# domains were given in (Tax, Academic, Content, Accounting Automation,
# Training) -- an item that could plausibly match more than one (e.g. "VAT
# training material") resolves to the first-listed domain, deterministically.
_DOMAIN_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Tax", ("vat", "cit", "pit", "wht", "withholding", "transfer pricing", "tax")),
    ("Academic", ("meteor", "cost action", "article", "paper", "conference", "academic")),
    ("Content", ("linkedin", "content calendar", "social post")),
    ("Accounting Automation", (
        "accounting mapping", "gl mapping", "transaction mapping",
        "transaction taxonomy", "chart of accounts", "rule-engine",
    )),
    ("Training", ("training", "slides", "workshop", "deck")),
)
_DEFAULT_DOMAIN = "General"


def classify_domain(text: str) -> str:
    lowered = (text or "").lower()
    for domain, keywords in _DOMAIN_KEYWORDS:
        if any(_contains_keyword(lowered, kw) for kw in keywords):
            return domain
    return _DEFAULT_DOMAIN


_HIGH_PRIORITY_KEYWORDS = (
    "urgent", "asap", "deadline", "critical", "client-impacting",
    "client impacting", "production issue", "production down", "immediately",
)
_LOW_PRIORITY_KEYWORDS = (
    "exploratory", "someday", "no rush", "low priority", "just an idea", "maybe worth",
)


def classify_priority(text: str) -> str:
    lowered = (text or "").lower()
    if any(_contains_keyword(lowered, kw) for kw in _HIGH_PRIORITY_KEYWORDS):
        return "HIGH"
    if any(_contains_keyword(lowered, kw) for kw in _LOW_PRIORITY_KEYWORDS):
        return "LOW"
    return "MEDIUM"


# ---------------------------------------------------------------------------
# Step 4: project matching
# ---------------------------------------------------------------------------
#
# Keywords are deliberately narrow/specific rather than generic ("ecj",
# "concordance" -- not the bare word "vat") so an uncertain item falls
# through to UNASSIGNED rather than being pinned to the wrong project.
# "Do not invent a project" means false negatives (-> UNASSIGNED) are the
# safe failure mode here, not false positives.
_PROJECT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VAT / EU Alignment Research", (
        "ecj", "concordance", "eu vat directive", "eu alignment", "vat directive",
    )),
    ("Accounting Mapping & Tax Automation", (
        "transaction taxonomy", "transaction mapping", "rule-engine schema",
        "accounting mapping",
    )),
    ("Tax Content Intelligence Agent", (
        "tax agent", "tax content", "human approval", "approval layer",
        "writer component", "linkedin publishing",
    )),
    ("Professional Personal Brand", (
        "personal brand", "publishing cadence", "professional brand",
    )),
    ("Tax Ruling Assistant", (
        "ruling knowledge base", "tax ruling", "advance tax ruling",
    )),
    ("Client Historical Knowledge Agent", (
        "client historical", "mailbox access", "client confidentiality",
    )),
    ("COST HUMAN-IT (CA24167)", ("cost human-it", "ca24167", "wg3")),
    ("METEOR / Horizon Europe", ("meteor", "horizon europe")),
    ("International Academic Opportunities", (
        "academic opportunit", "screened pipeline",
    )),
    ("English to C1", ("english practice", "c1 english", "english to c1")),
    ("Local Files MCP (Windows document access)", (
        "local files mcp", "cloudflare login",
    )),
    ("Hermes Personal AI Infrastructure", (
        "hermes infrastructure", "hybrid memory", "cron scheduler",
    )),
)
UNASSIGNED = "UNASSIGNED"


def match_project(text: str, known_projects: list[str]) -> str:
    """`known_projects` is the canonical list read from PROJECTS.md -- a
    keyword match only counts if the target project still exists there,
    so a removed/renamed project can never be silently matched."""
    lowered = (text or "").lower()
    known = set(known_projects)
    for project, keywords in _PROJECT_KEYWORDS:
        if project not in known:
            continue
        if any(_contains_keyword(lowered, kw) for kw in keywords):
            return project
    return UNASSIGNED


def load_known_projects(projects_path: Path) -> list[str]:
    """Read the canonical project names from PROJECTS.md's summary table.
    Best-effort: a missing/unparseable file yields an empty list (every
    item then resolves to UNASSIGNED rather than crashing)."""
    try:
        text = projects_path.read_text(encoding="utf-8")
    except OSError:
        return []
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0].lower() in ("#", "---") or set(cells[0]) <= {"-"}:
            continue
        if not cells[0].isdigit():
            continue
        names.append(cells[1])
    return names


# ---------------------------------------------------------------------------
# Step 5: next action (one concrete line, never a task plan)
# ---------------------------------------------------------------------------

def generate_next_action(type_: str, title: str) -> str:
    if type_ == "IDEA":
        return f"Evaluate feasibility and evidence: {title}"
    if type_ == "TASK":
        return title
    if type_ == "RESEARCH":
        return f"Find and summarize relevant sources: {title}"
    if type_ == "DECISION":
        return f"Record rationale and affected workflows: {title}"
    if type_ == "REMINDER":
        return title
    if type_ == "PROJECT":
        return f"Define scope and next milestone: {title}"
    return title


# ---------------------------------------------------------------------------
# Step 8: duplicate detection (title-similarity over OPEN entries only)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "is",
    "are", "at", "by", "from", "into", "about",
})


def _significant_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _title_similarity(a: str, b: str) -> float:
    tokens_a, tokens_b = _significant_tokens(a), _significant_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


_DUPLICATE_SIMILARITY_THRESHOLD = 0.5


def find_duplicate(title: str, entries: list[BacklogEntry]) -> Optional[str]:
    """Return the id of a likely-duplicate OPEN entry, or None. Never
    considers DONE/CANCELLED entries a duplicate target -- a closed item
    finishing its life cycle should not block a fresh, unrelated capture
    that happens to share vocabulary."""
    best_id, best_score = None, 0.0
    for entry in entries:
        if entry.status.upper() not in _OPEN_STATUSES:
            continue
        score = _title_similarity(title, entry.title)
        if score > best_score:
            best_id, best_score = entry.id, score
    if best_score >= _DUPLICATE_SIMILARITY_THRESHOLD:
        return best_id
    return None


# ---------------------------------------------------------------------------
# BACKLOG.md parsing/rendering (Step 2 format, Step 6 persistence)
# ---------------------------------------------------------------------------

_ENTRY_HEADER_RE = re.compile(r"^###\s+(BL-\d+)\s*$")
_FIELD_RE = re.compile(r"^-\s+([A-Za-z ]+):\s*(.*)$")
_FIELD_NAME_TO_ATTR = {
    "type": "type", "domain": "domain", "project": "project", "title": "title",
    "priority": "priority", "status": "status", "next action": "next_action",
    "created": "created", "source": "source", "notes": "notes",
}


def parse_backlog(text: str) -> list[BacklogEntry]:
    entries: list[BacklogEntry] = []
    current_id: Optional[str] = None
    fields: dict[str, str] = {}

    def _flush():
        if current_id is None:
            return
        entries.append(BacklogEntry(
            id=current_id,
            type=fields.get("type", ""),
            domain=fields.get("domain", ""),
            project=fields.get("project", ""),
            title=fields.get("title", ""),
            priority=fields.get("priority", ""),
            status=fields.get("status", ""),
            next_action=fields.get("next_action", ""),
            created=fields.get("created", ""),
            source=fields.get("source", ""),
            notes=fields.get("notes", ""),
        ))

    for line in text.splitlines():
        header_match = _ENTRY_HEADER_RE.match(line)
        if header_match:
            _flush()
            current_id = header_match.group(1)
            fields = {}
            continue
        field_match = _FIELD_RE.match(line)
        if field_match and current_id is not None:
            name = field_match.group(1).strip().lower()
            attr = _FIELD_NAME_TO_ATTR.get(name)
            if attr:
                fields[attr] = field_match.group(2).strip()
    _flush()
    return entries


def format_entry(entry: BacklogEntry) -> str:
    lines = [
        f"### {entry.id}",
        f"- Type: {entry.type}",
        f"- Domain: {entry.domain}",
        f"- Project: {entry.project}",
        f"- Title: {entry.title}",
        f"- Priority: {entry.priority}",
        f"- Status: {entry.status}",
        f"- Next action: {entry.next_action}",
        f"- Created: {entry.created}",
        f"- Source: {entry.source}",
    ]
    if entry.notes:
        lines.append(f"- Notes: {entry.notes}")
    return "\n".join(lines)


def next_id(entries: list[BacklogEntry]) -> str:
    max_n = 0
    for entry in entries:
        m = re.match(r"^BL-(\d+)$", entry.id)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"BL-{max_n + 1:04d}"


_HEADER_TEMPLATE = """# BACKLOG

Structured capture items awaiting triage -- research ideas, follow-ups, and
next-action candidates that are not yet part of a project's primary
"Next action" in `PROJECTS.md`. `PROJECTS.md` stays project-level only
(status, primary next action, key decisions); individual backlog items
live here instead.

This is preparation for the future Capture Router, which will own
persistence/sync of backlog items. Until it exists, entries are added
here manually, in this workspace mirror only -- **not** pushed to the
deployed Hermes's `/opt/data/context/`.

Format per entry:

```
### <ID>
- Type: ...
- Domain: ...
- Project: ...
- Title: ...
- Priority: ...
- Status: ...
- Next action: ...
- Created: YYYY-MM-DD
- Source: ...
```

---
"""


def append_entry(backlog_path: Path, entry: BacklogEntry) -> None:
    """Append one entry, preserving everything already in the file.
    Writes to a temp file in the same directory and replaces atomically
    (os.replace) so a crash mid-write can never corrupt or truncate the
    existing backlog."""
    if backlog_path.exists():
        current = backlog_path.read_text(encoding="utf-8")
    else:
        current = _HEADER_TEMPLATE
    if not current.endswith("\n"):
        current += "\n"
    new_text = current + "\n" + format_entry(entry) + "\n"
    _atomic_write(backlog_path, new_text)


def update_entry_field(backlog_path: Path, entry_id: str, field: str, value: str) -> Optional[BacklogEntry]:
    """Rewrite exactly one field of exactly one entry, byte-preserving
    every other entry's formatting via a full parse+re-render (the format
    is fully deterministic, so re-rendering an unrelated entry is
    lossless). Returns the updated entry, or None if `entry_id` doesn't
    exist."""
    if not backlog_path.exists():
        return None
    entries = parse_backlog(backlog_path.read_text(encoding="utf-8"))
    updated = None
    for e in entries:
        if e.id == entry_id:
            setattr(e, field, value)
            updated = e
            break
    if updated is None:
        return None
    rendered = _HEADER_TEMPLATE + "\n" + "\n\n".join(format_entry(e) for e in entries) + "\n"
    _atomic_write(backlog_path, rendered)
    return updated


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Step 9/10: read-only queries and explicit update commands
# ---------------------------------------------------------------------------

_QUERY_RE = re.compile(
    # More specific alternatives (items-for) must precede the more general
    # "show X research" pattern: regex alternation takes the first
    # alternative that matches, not the longest, so "show items for VAT
    # Research" would otherwise be mis-parsed as a domain query with
    # domain="items for VAT" by the "research" branch.
    r"^\s*(backlog|my backlog|show\s+(high|medium|low)\s+priority|show\s+items\s+for\s+(.+)|show\s+(.+?)\s+research)\s*$",
    re.IGNORECASE,
)

_UPDATE_RE = re.compile(
    r"^\s*(done|start|cancel|priority)\s+(BL-\d+)(?:\s+(high|medium|low))?\s*$",
    re.IGNORECASE,
)

_UPDATE_VERB_TO_STATUS = {
    "done": "DONE",
    "start": "IN_PROGRESS",
    "cancel": "CANCELLED",
}


def parse_query(text: str) -> Optional[dict]:
    """Recognize a read-only backlog query. Returns a filter dict (never
    None-vs-empty-ambiguous: an empty dict means "show everything") or
    None if `text` is not a recognized query."""
    match = _QUERY_RE.match(text or "")
    if not match:
        return None
    lowered = text.strip().lower()
    if lowered in ("backlog", "my backlog"):
        return {}
    if match.group(2):
        return {"priority": match.group(2).upper()}
    if match.group(3):
        return {"project": match.group(3).strip()}
    if match.group(4):
        return {"domain": match.group(4).strip().title()}
    return {}


def apply_query(entries: list[BacklogEntry], filters: dict) -> list[BacklogEntry]:
    result = entries
    if "priority" in filters:
        result = [e for e in result if e.priority.upper() == filters["priority"]]
    if "domain" in filters:
        result = [e for e in result if e.domain.lower() == filters["domain"].lower()]
    if "project" in filters:
        needle = filters["project"].lower()
        result = [e for e in result if needle in e.project.lower()]
    return result


def parse_update_command(text: str) -> Optional[tuple[str, str, str]]:
    """Recognize an explicit update command. Returns (entry_id, field,
    new_value) or None. Never infers a status change from ordinary
    conversation -- only these exact verb+ID (+level) shapes match."""
    match = _UPDATE_RE.match(text or "")
    if not match:
        return None
    verb, entry_id, level = match.group(1).lower(), match.group(2).upper(), match.group(3)
    if verb == "priority":
        if not level:
            return None
        return entry_id, "priority", level.upper()
    return entry_id, "status", _UPDATE_VERB_TO_STATUS[verb]


# ---------------------------------------------------------------------------
# Orchestration (impure: reads/writes the two files)
# ---------------------------------------------------------------------------

def capture(raw_text: str, *, projects_path: Path, backlog_path: Path, created: str, source: str) -> dict:
    parsed = parse_prefix(raw_text)
    if parsed is None:
        return {"matched": False}

    type_, title = parsed
    domain = classify_domain(title)
    priority = classify_priority(raw_text)
    known_projects = load_known_projects(projects_path)
    project = match_project(title, known_projects)
    next_action = generate_next_action(type_, title)

    existing_entries = []
    if backlog_path.exists():
        existing_entries = parse_backlog(backlog_path.read_text(encoding="utf-8"))

    duplicate_of = find_duplicate(title, existing_entries)
    if duplicate_of:
        return {"matched": True, "duplicate": True, "duplicate_of": duplicate_of}

    entry = BacklogEntry(
        id=next_id(existing_entries),
        type=type_, domain=domain, project=project, title=title,
        priority=priority, status="BACKLOG", next_action=next_action,
        created=created, source=source, notes="",
    )
    append_entry(backlog_path, entry)
    return {"matched": True, "duplicate": False, "entry": asdict(entry)}


def render_confirmation(entry: dict) -> str:
    return (
        f"Captured as {entry['id']}\n\n"
        f"Type: {entry['type'].title()}\n"
        f"Domain: {entry['domain']}\n"
        f"Project: {entry['project']}\n"
        f"Priority: {entry['priority'].title()}\n"
        f"Next action: {entry['next_action']}"
    )


def render_duplicate_notice(duplicate_of: str) -> str:
    return f"Possible duplicate of {duplicate_of} -- merge with it, or create a separate item?"


# ---------------------------------------------------------------------------
# Action hints for query results (Phase 2B-D)
# ---------------------------------------------------------------------------
#
# Copy-paste-ready update commands shown under each open item in a
# Backlog/My backlog query response, so a user can act without needing to
# recall the exact command syntax. Derived ONLY from entry.id and
# entry.status -- never from title/notes/any other field, so there is no
# free-form text/path/content interpolation into the hint. DONE/CANCELLED
# items show no hint (nothing further to do). This is a rendering-only
# change: it does not touch parse_update_command(), update_entry_field(),
# or any persistence/routing semantics.
_ACTION_HINT_VERBS: dict[str, tuple[str, ...]] = {
    # Not yet started -- offer Start as well as the terminal actions.
    "BACKLOG": ("Start", "Done", "Cancel"),
    "READY": ("Start", "Done", "Cancel"),
    # Already active -- Start no longer applies, only the terminal actions.
    "IN_PROGRESS": ("Done", "Cancel"),
    "WAITING": ("Done", "Cancel"),
    # DONE / CANCELLED intentionally absent: no hint line for closed items.
}


def _action_hint_line(entry_id: str, status: str) -> Optional[str]:
    """One copy-paste-ready hint line for an open entry, or None for a
    closed one (DONE/CANCELLED, or any unrecognized status)."""
    verbs = _ACTION_HINT_VERBS.get(status.upper())
    if not verbs:
        return None
    return "Reply: " + " · ".join(f"{verb} {entry_id}" for verb in verbs)


def render_query_results(entries: list[BacklogEntry]) -> str:
    """Render a backlog query's matched entries as a concise list: one
    block per item (ID + title, priority/status, and -- for open items
    only -- a copy-paste-ready action hint line). Read-only: this only
    formats already-gathered data and never mutates anything."""
    if not entries:
        return "No matching backlog items."
    blocks = []
    for entry in entries:
        lines = [f"{entry.id} — {entry.title}", f"{entry.priority} · {entry.status}"]
        hint = _action_hint_line(entry.id, entry.status)
        if hint:
            lines.append("")
            lines.append(hint)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# CLI entry point -- the only impure surface besides capture()'s file I/O
# ---------------------------------------------------------------------------

def _default_home() -> Path:
    import os
    return Path(os.environ.get("HERMES_HOME", str(Path.home())))


def main() -> int:
    import datetime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "query", "update"])
    parser.add_argument("text", nargs="?", default="")
    parser.add_argument("--projects-path", type=Path, default=None)
    parser.add_argument("--backlog-path", type=Path, default=None)
    parser.add_argument("--source", default="Telegram")
    args = parser.parse_args()

    home = _default_home()
    projects_path = args.projects_path or (home / "context" / "PROJECTS.md")
    backlog_path = args.backlog_path or (home / "context" / "BACKLOG.md")

    if args.mode == "capture":
        created = datetime.date.today().isoformat()
        result = capture(
            args.text, projects_path=projects_path, backlog_path=backlog_path,
            created=created, source=args.source,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.mode == "query":
        entries = []
        if backlog_path.exists():
            entries = parse_backlog(backlog_path.read_text(encoding="utf-8"))
        filters = parse_query(args.text)
        if filters is None:
            print(json.dumps({"matched": False}))
            return 0
        matched = apply_query(entries, filters)
        print(json.dumps({"matched": True, "entries": [asdict(e) for e in matched]}, ensure_ascii=False))
        return 0

    if args.mode == "update":
        parsed = parse_update_command(args.text)
        if parsed is None:
            print(json.dumps({"matched": False}))
            return 0
        entry_id, field, value = parsed
        updated = update_entry_field(backlog_path, entry_id, field, value)
        if updated is None:
            print(json.dumps({"matched": True, "found": False, "id": entry_id}))
            return 0
        print(json.dumps({"matched": True, "found": True, "entry": asdict(updated)}, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
