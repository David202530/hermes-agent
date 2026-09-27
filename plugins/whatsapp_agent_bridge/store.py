"""Minimal local SQLite store for the whatsapp-agent-bridge plugin.

Stores only what Hermes needs to relay actions: the event_id of each pushed
reminder (for idempotency), the current WhatsApp Agent card per "thread"
(keyed by the event_id of the reminder that started it -- the only stable
identifier this wire contract ever hands Hermes; see this plugin's README
for why), and the Agent-issued opaque action tokens attached to that
card's buttons. A thread has exactly one current card; only its action
tokens keep a superseded history, so an old button can never be replayed
once a newer card has minted fresh ones for the same thread.

Does NOT store: WhatsApp Agent's own conversation/message history, phone
numbers, WhatsApp internal IDs, or LocalAuth/session data. None of that is
ever present on the wire this plugin receives in the first place (see
HermesCard's own scope note in D:\\AI Agent\\Whatsapp AI
src/whatsapp-agent/hermes/cards.ts) -- this store never invents fields the
wire contract already omits. The WhatsApp Agent remains the sole
authoritative source for all conversation state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cards import TERMINAL_CARD_TYPES

DDL = """
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

-- One row per thread_id (the PRIMARY KEY already guarantees a single
-- current-state slot per thread -- there is nothing to "supersede" here,
-- unlike bridge_action_tokens below, which keeps every token minted over a
-- thread's lifetime so old ones can be told apart from the current ones).
CREATE TABLE IF NOT EXISTS bridge_threads (
    thread_id TEXT PRIMARY KEY,
    card_type TEXT NOT NULL,
    card_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS bridge_action_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    label TEXT NOT NULL,
    action_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except ImportError:
        import os
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "whatsapp_agent_bridge" / "bridge.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(DDL)
    return conn


def _upsert_thread_and_tokens(conn: sqlite3.Connection, now: str, thread_id: str, card_type: str, card: dict, actions: list[dict]) -> None:
    """Must run inside a caller-owned transaction. Supersedes any prior
    (non-superseded) action tokens for this thread, then upserts its
    current card. `actions` is the card's own `actions` list, already
    filtered by cards.extract_actions -- tokens are stored and later
    returned as opaque strings only, never decoded or altered."""
    conn.execute(
        "UPDATE bridge_action_tokens SET superseded_at = ? WHERE thread_id = ? AND superseded_at IS NULL",
        (now, thread_id),
    )
    conn.execute(
        "INSERT INTO bridge_threads (thread_id, card_type, card_json, received_at, delivered_at) "
        "VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(thread_id) DO UPDATE SET card_type = excluded.card_type, "
        "card_json = excluded.card_json",
        (thread_id, card_type, json.dumps(card, ensure_ascii=False), now),
    )
    for action in actions:
        conn.execute(
            "INSERT INTO bridge_action_tokens (thread_id, action_id, label, action_token, created_at, superseded_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (thread_id, action["action_id"], action["label"], action["action_token"], now),
        )


def claim_and_store_event(event_id: str, thread_id: str, card_type: str, card: dict, actions: list[dict]) -> bool:
    """Atomically claims event_id AND stores its card/tokens in ONE
    transaction, so a crash between "claimed" and "stored" can never happen
    -- either both commit together, or neither does (a retried push then
    sees the event as unclaimed and reprocesses it cleanly from scratch,
    rather than being permanently swallowed). Returns True the first time
    (go ahead and notify), False for a duplicate (already fully processed
    -- ack again, never re-notify). Same claim-before-effect discipline as
    plugins/tax_content_bridge/delivery_receipts.py, but as one transaction
    instead of two separate store round-trips."""
    now = _now()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO processed_events (event_id, received_at) VALUES (?, ?)", (event_id, now))
        _upsert_thread_and_tokens(conn, now, thread_id, card_type, card, actions)
        conn.execute("COMMIT")
        return True
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return False
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def store_thread(thread_id: str, card_type: str, card: dict, actions: list[dict]) -> None:
    """Upserts this thread's current card (one row per thread_id -- a later
    call for the same thread_id simply updates it in place) and supersedes
    any prior (non-superseded) action tokens for that thread before storing
    the new ones. Used by tools.py for the synchronous response card
    returned by an action callback (no event_id/idempotency concern there
    -- see claim_and_store_event for the pushed-event path)."""
    now = _now()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _upsert_thread_and_tokens(conn, now, thread_id, card_type, card, actions)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def mark_delivered(thread_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE bridge_threads SET delivered_at = ? WHERE thread_id = ? AND delivered_at IS NULL",
            (_now(), thread_id),
        )
    finally:
        conn.close()


def list_active_threads() -> list[dict]:
    """Every thread whose current card is NOT a terminal type (see
    cards.TERMINAL_CARD_TYPES), newest first. This plugin has no way to ask
    the Agent for canonical pending state -- there is no pull/query
    endpoint on the wire (see README) -- so this is Hermes's own local
    view, populated only by what has actually been pushed or returned so
    far. The Agent's own state remains authoritative; a stale/mismatched
    token is always rejected by the Agent itself, never silently trusted
    here.

    Deliberately excludes terminal-card threads rather than deleting their
    rows, so a completed thread doesn't count as "currently pending" (which
    would make every tool that acts on "the current thread" become
    permanently ambiguous after the very first reminder is ever resolved)."""
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in TERMINAL_CARD_TYPES)
        rows = conn.execute(
            f"SELECT thread_id, card_type, card_json, received_at FROM bridge_threads "
            f"WHERE card_type NOT IN ({placeholders}) ORDER BY received_at DESC",
            tuple(TERMINAL_CARD_TYPES),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"thread_id": r[0], "card_type": r[1], "card": json.loads(r[2]), "received_at": r[3]}
        for r in rows
    ]


def find_tokens(thread_id: str, action_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT label, action_token FROM bridge_action_tokens "
            "WHERE thread_id = ? AND action_id = ? AND superseded_at IS NULL",
            (thread_id, action_id),
        ).fetchall()
    finally:
        conn.close()
    return [{"label": r[0], "action_token": r[1]} for r in rows]


def find_token(thread_id: str, action_id: str) -> Optional[dict]:
    """Unambiguous lookup only -- returns None (fail closed) if zero or more
    than one candidate rather than guess which button was meant. Callers
    needing to disambiguate multiple same-action_id buttons (e.g.
    RESOLVE_UNKNOWN_SEND's two resolutions) use find_tokens() directly."""
    matches = find_tokens(thread_id, action_id)
    return matches[0] if len(matches) == 1 else None
