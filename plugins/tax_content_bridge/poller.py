"""Background poller: the ONLY outbound-delivery path for this plugin.

There is no plugin hook to add an HTTP route to `hermes serve` (confirmed
by reading hermes_cli/plugins.py in full -- only register_tool,
register_cli_command, register_hook, and register_middleware for
agent-turn execution exist), so the Tax Agent cannot push events into
Hermes. Instead this thread polls the Tax Agent's own action_gateway for
pending outbound events and delivers each one via Hermes's existing
Telegram-sending path (tools.send_message_tool -- the same helper
mcp_serve.py's `messages_send` tool already uses), then acks it.

Duplicate-safety: see delivery_receipts.py. A claim is taken BEFORE
sending and only released on a send failure, so a successful send is
never repeated even if the subsequent ack fails and the event reappears
on a later poll -- see that module's docstring for the precise guarantee
and its one topology assumption (this Railway service currently runs
exactly one replica, one region -- verified via the Railway API; if that
ever changes, re-read that module's cross-replica note first).

Never rephrases or regenerates Tax Agent's authoritative content -- for
DRAFT_READY/FINAL_PACKAGE_READY it relays the exact text/hashtags Tax
Agent produced; every other event type gets a short, fixed Georgian
status line (never the raw `[EVENT_TYPE]` fallback, except for a type
this module genuinely doesn't recognize yet).
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from agent.secret_scope import get_secret
from tools.send_message_tool import send_message_tool

from .delivery_receipts import claim_for_delivery, release_claim

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 4
_TIMEOUT_SECONDS = 15
_ARTIFACT_EVENT_TYPES = ("DRAFT_READY", "FINAL_PACKAGE_READY")

# Fixed, concise Georgian status lines for every event type Tax Agent's
# spec names that isn't an authoritative-content event (those two are
# handled separately, above, so their exact text/hashtags are relayed
# verbatim rather than replaced by a fixed line). PUBLISH_* remain
# structurally supported for forward-compat; real publication stays
# disabled regardless (Tax Agent Core's own gate, untouched).
_STATUS_TEXT = {
    "DRAFT_BLOCKED": "⚠️ დრაფტის მომზადება შეჩერდა — საჭიროა დამატებითი შემოწმება.",
    "TEXT_REVISION_STARTED": "✏️ ტექსტის შესწორებაზე ვმუშაობ.",
    "VISUAL_REVISION_STARTED": "🖼️ ინფოგრაფიკის შესწორებაზე ვმუშაობ.",
    "COMBINED_REVISION_STARTED": "🔁 ტექსტისა და ინფოგრაფიკის შესწორებაზე ვმუშაობ.",
    "REVISION_BLOCKED": "⚠️ მოთხოვნილი შესწორება ვერ შესრულდა — საჭიროა დამატებითი განხილვა.",
    "VISUAL_GENERATION_STARTED": "✅ ტექსტი დამტკიცებულია. ვიწყებ ინფოგრაფიკის მომზადებას.",
    "FINAL_APPROVAL_CONFIRMED": "✅ საბოლოო პაკეტის დამტკიცება შენახულია.",
    "WORKFLOW_BLOCKED": "⚠️ სამუშაო პროცესი შეჩერდა — საჭიროა თქვენი ჩარევა.",
    "PUBLISH_SUCCESS": "✅ პოსტი წარმატებით გამოქვეყნდა LinkedIn-ზე.",
    "PUBLISH_FAILURE": "⚠️ LinkedIn-ზე გამოქვეყნება ვერ დასრულდა.",
}

_started = False
_start_lock = threading.Lock()


def _base_url() -> str:
    return (get_secret("TAX_AGENT_BRIDGE_URL", "") or "").rstrip("/")


def _shared_secret() -> str:
    return get_secret("TAX_AGENT_BRIDGE_SHARED_SECRET", "") or ""


def _delivery_chat_id() -> str:
    """Single-tenant bridge (exactly one authorized David identity) --
    pending-events doesn't carry a chat_id (there is only ever one), so
    it's configured here directly rather than round-tripped over the wire."""
    chat_id = get_secret("TAX_AGENT_BRIDGE_CHAT_ID", "") or ""
    return chat_id if chat_id.isdigit() else ""


def _render_topic_shortlist(payload: dict) -> str:
    cards = payload.get("cards") or payload.get("topics") or []
    if not cards:
        return "📚 დღევანდელი საინტერესო საგადასახადო საკითხები მზადაა."
    lines = ["📚 დღევანდელი საინტერესო საგადასახადო საკითხები:"]
    for i, card in enumerate(cards, 1):
        title = (card.get("suggested_title") or card.get("case_id") or f"თემა {i}") if isinstance(card, dict) else str(card)
        lines.append(f"{i}. {title}")
    return "\n".join(lines)


def render_text(event: dict) -> str:
    """The exact text handed to send_message_tool. Authoritative content
    (draft/final-package text+hashtags) is relayed verbatim; every other
    known event type gets a fixed, concise Georgian line; a genuinely
    unrecognized type falls back to a bracketed tag rather than silence."""
    payload = event.get("payload") or {}
    event_type = event.get("event_type")

    if event_type in _ARTIFACT_EVENT_TYPES:
        text = str(payload.get("text", ""))
        hashtags = " ".join(str(tag) for tag in payload.get("hashtags", []))
        return f"{text}\n\n{hashtags}" if hashtags else text

    if event_type == "TOPIC_SHORTLIST_READY":
        return _render_topic_shortlist(payload)

    if event_type in _STATUS_TEXT:
        return _STATUS_TEXT[event_type]

    return f"[{event_type}]"


def _ack(base_url: str, headers: dict, event_id: str) -> None:
    try:
        requests.post(f"{base_url}/hermes/ack-event", json={"event_id": event_id}, headers=headers, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:
        # Delivered (or already-delivered) but not acked: worst case is
        # this same event reappears next poll and we retry only the ack
        # (claim_for_delivery already prevents a resend) -- never a lost
        # message, just a delayed ack.
        logger.warning("tax-content-bridge poller: ack failed for %s: %s", event_id, exc)


def _poll_once() -> None:
    base_url = _base_url()
    secret = _shared_secret()
    chat_id = _delivery_chat_id()
    if not base_url or not secret or not chat_id:
        return  # not fully configured yet -- idle quietly, never crash the gateway process

    headers = {"Authorization": f"Bearer {secret}"}
    try:
        response = requests.get(f"{base_url}/hermes/pending-events", headers=headers, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        events = response.json().get("events", [])
    except Exception as exc:
        logger.warning("tax-content-bridge poller: could not reach the Tax Agent bridge: %s", exc)
        return

    for event in events:
        event_id = event.get("event_id")
        if not event_id:
            continue

        if not claim_for_delivery(event_id):
            # Already sent by an earlier poll (or another replica sharing
            # this store) -- the ack apparently never landed. Retry only
            # the ack; never resend to Telegram.
            _ack(base_url, headers, event_id)
            continue

        try:
            send_message_tool({"action": "send", "target": f"telegram:{chat_id}", "message": render_text(event)})
        except Exception as exc:
            release_claim(event_id)  # not actually delivered -- let a later poll retry
            logger.warning("tax-content-bridge poller: delivery failed for %s: %s", event_id, exc)
            continue

        _ack(base_url, headers, event_id)


def _loop() -> None:
    while True:
        try:
            _poll_once()
        except Exception:
            logger.exception("tax-content-bridge poller: unexpected error")
        time.sleep(_POLL_INTERVAL_SECONDS)


def start_poller() -> None:
    """Idempotent: safe to call every time the plugin registers (e.g. a
    hot-reload) -- only ever starts one background thread for the process
    lifetime. Assumes exactly one Hermes process/replica for this service
    (verified via the Railway API at the time this plugin was written --
    multiRegionConfig: one region, numReplicas: 1); delivery_receipts.py's
    SQLite store additionally protects against a future multi-replica
    deployment PROVIDED replicas share its underlying volume -- if they do
    not, re-verify this assumption before scaling out."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="tax-content-bridge-poller", daemon=True).start()
