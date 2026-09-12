"""Durable claim-before-send receipts so the poller never delivers the
same Tax Agent event_id to Telegram twice.

Problem this closes: GET pending-events -> send -> POST ack-event has a
gap between "sent" and "acked" -- if the ack POST fails (network blip,
Hermes restart), the NEXT poll gets the SAME event again (Tax Agent still
sees it as undelivered), and a naive poller would send it to Telegram a
second time.

Fix: an atomic claim, backed by a SQLite UNIQUE constraint, taken BEFORE
sending and never released on success:

  1. claim_for_delivery(event_id) -- INSERT; True if this call just
     claimed it (nobody has sent it before, as far as this store knows),
     False if it was already claimed (already sent -- do not resend, just
     retry the ack).
  2. Only on a send FAILURE is the claim released (release_claim), so a
     genuinely undelivered event can still be retried later. A successful
     send's claim is permanent -- that event_id will never be sent again
     by this store, even across process restarts.

Guarantee, precisely stated: **at-most-once delivery to Telegram per
event_id, for as long as this receipts file survives** (it lives under
HERMES_HOME, which is on the same persistent volume Hermes's own gateway
state uses -- see the Tax Agent repo's docs/HERMES_INTEGRATION.md for the
Railway topology this assumes: exactly one replica). This is NOT a
mathematical exactly-once guarantee: if the underlying file were lost
between "claim" and "send" (e.g. disk corruption) the claim itself would
also be lost and a resend could occur; that risk is accepted as
negligible relative to a real SQLite file on a persistent volume. If
Hermes ever runs multiple replicas SHARING this same volume, SQLite's own
file locking extends the same at-most-once guarantee across replicas; if
replicas do NOT share a volume, this guarantee only holds per-replica and
the operational assumption of exactly one replica (documented in the Tax
Agent repo) must hold instead.

Ack delivery itself remains at-least-once (idempotent on the Tax Agent
side -- a repeat ack for an already-delivered event is a harmless no-op),
which is fine: retrying an ack is invisible to David, unlike retrying a
Telegram send.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS delivered_events (
    event_id TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL
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
    return home / "tax_content_bridge" / "delivery_receipts.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(DDL)
    return conn


def claim_for_delivery(event_id: str) -> bool:
    """Atomically claims event_id. Returns True the first time (go ahead
    and send), False every time after (already sent -- do not resend)."""
    conn = _connect()
    try:
        conn.execute("INSERT INTO delivered_events (event_id, claimed_at) VALUES (?, ?)", (event_id, _now()))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def release_claim(event_id: str) -> None:
    """Called ONLY when the send itself failed -- lets a later poll retry
    this event instead of silently dropping it forever."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM delivered_events WHERE event_id = ?", (event_id,))
    finally:
        conn.close()
