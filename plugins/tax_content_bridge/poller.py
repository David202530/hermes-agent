"""Background poller: the ONLY outbound-delivery path for this plugin.

There is no plugin hook to add an HTTP route to `hermes serve` (confirmed
by reading hermes_cli/plugins.py in full -- only register_tool,
register_cli_command, register_hook, and register_middleware for
agent-turn execution exist), so the Tax Agent cannot push events into
Hermes. Instead this thread polls the Tax Agent's own action_gateway for
pending outbound events and delivers each one via Hermes's existing
Telegram-sending path (tools.send_message_tool -- the same helper
mcp_serve.py's `messages_send` tool already uses), then acks it.

Never rephrases or regenerates Tax Agent's content -- it relays the exact
text/hashtags Tax Agent produced (spec: Hermes may explain status
conversationally, but must never alter approved content).
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from agent.secret_scope import get_secret
from tools.send_message_tool import send_message_tool

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 4
_TIMEOUT_SECONDS = 15
_ARTIFACT_EVENT_TYPES = ("DRAFT_READY", "FINAL_PACKAGE_READY")

_started = False
_start_lock = threading.Lock()


def _base_url() -> str:
    return (get_secret("TAX_AGENT_BRIDGE_URL", "") or "").rstrip("/")


def _shared_secret() -> str:
    return get_secret("TAX_AGENT_BRIDGE_SHARED_SECRET", "") or ""


def _delivery_chat_id() -> str:
    """This is a single-tenant bridge (exactly one authorized David
    identity) -- pending-events doesn't carry a chat_id (there is only
    ever one), so it's configured here directly rather than round-tripped
    through the wire contract."""
    return get_secret("TAX_AGENT_BRIDGE_CHAT_ID", "") or ""


def render_text(event: dict) -> str:
    """Exact-content rendering only -- for DRAFT_READY/FINAL_PACKAGE_READY
    this is the approved text + hashtags verbatim; everything else gets a
    short, fixed Georgian status line. Never paraphrases Tax Agent's own
    content."""
    payload = event.get("payload") or {}
    event_type = event.get("event_type")
    if event_type in _ARTIFACT_EVENT_TYPES:
        text = str(payload.get("text", ""))
        hashtags = " ".join(str(tag) for tag in payload.get("hashtags", []))
        return f"{text}\n\n{hashtags}" if hashtags else text
    if event_type == "VISUAL_GENERATION_STARTED":
        return "✅ ტექსტი დამტკიცებულია. ვიწყებ ინფოგრაფიკის მომზადებას."
    if event_type == "PUBLISH_FAILURE":
        return "⚠️ LinkedIn-ზე გამოქვეყნება ვერ დასრულდა."
    return f"[{event_type}]"


def _poll_once() -> None:
    base_url = _base_url()
    secret = _shared_secret()
    chat_id = _delivery_chat_id()
    if not base_url or not secret or not chat_id:
        return  # not configured yet -- idle quietly, never crash the gateway process

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
        try:
            send_message_tool({"action": "send", "target": f"telegram:{chat_id}", "message": render_text(event)})
        except Exception as exc:
            logger.warning("tax-content-bridge poller: delivery failed for %s: %s", event_id, exc)
            continue  # leave it PENDING -- Tax Agent's own bounded retry/backoff covers this
        try:
            requests.post(
                f"{base_url}/hermes/ack-event", json={"event_id": event_id}, headers=headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Delivered but not acked: worst case is one redundant re-send
            # on the next poll, never a lost message -- acceptable, logged.
            logger.warning("tax-content-bridge poller: ack failed for %s: %s", event_id, exc)


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
    lifetime."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="tax-content-bridge-poller", daemon=True).start()
