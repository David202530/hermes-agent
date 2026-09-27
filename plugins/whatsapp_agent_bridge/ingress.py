"""Inbound HTTP ingress: the ONLY path WhatsApp Agent's HermesEventSink can
use to reach this Hermes instance (push model -- unlike
plugins/tax_content_bridge, which polls the Tax Agent, WhatsApp Agent has no
GET-style pending-events endpoint to poll; confirmed by grepping its whole
src/ tree for any such route -- none exists).

There is no lighter-weight Hermes extension point for receiving this push.
hermes_cli/plugins.py's PluginContext offers register_tool,
register_cli_command, register_hook, and register_middleware -- none carry
an HTTP route. register_platform exists, but it is the full
BasePlatformAdapter contract (gateway/platforms/base.py: session handling,
typing indicators, media, streaming TTS, debounce...) -- built for a
complete bidirectional chat platform, disproportionate for receiving one
Bearer-authenticated JSON POST and acking it. The existing generic
gateway/platforms/webhook.py DOES run a real inbound HTTP server already,
but its auth is HMAC-signature only (confirmed: no Bearer support anywhere
in it or webhook_filters.py) -- accepting WhatsApp Agent's Bearer scheme
there would require editing that shared, already-deployed adapter's own
auth code, which risks every existing webhook integration.

So this plugin owns a minimal stdlib http.server.ThreadingHTTPServer,
gated the same way tax_content_bridge's poller is: started only when
HERMES_GATEWAY_PROCESS == "1" (see __init__.py), exactly once per process.
Reaching it from the public internet requires a Railway domain/target-port
mapping onto WHATSAPP_AGENT_BRIDGE_PORT -- a separate, later deployment
step, deliberately not created by this plugin or this task.

Only WHATSAPP_REMINDER_DUE is ever pushed by WhatsApp Agent (confirmed in
its src/whatsapp-agent/events/hermes-event-sink.ts: every other event type
is filtered out before publish); every other card (draft, confirmation,
result) arrives synchronously as the return value of this plugin's own
POST /hermes/actions call, not as a second push -- see callback_client.py
and tools.py.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import requests

from . import cards, config, store
from .render import render_card

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 32_768
_ALLOWED_EVENT_TYPES = {"WHATSAPP_REMINDER_DUE"}
_INGRESS_PATH = config.INGRESS_PATH
_TELEGRAM_TIMEOUT_SECONDS = (5, 15)


def _notify(authorized_user_id: str, text: str) -> None:
    """Delivers the notification as a literal Telegram message by calling
    the Bot API directly -- NOT tools.send_message_tool / the Telegram
    platform adapter. That shared path auto-detects and renders Markdown/
    HTML in the message text (plugins/platforms/telegram/adapter.py's
    format_message chooses MarkdownV2/HTML parse_mode based on the text's
    own content), which would silently defeat the plain-text guarantee this
    plugin and WhatsApp Agent's own Phase 5 design both depend on: an
    inbound WhatsApp message is untrusted input (WhatsApp Agent's own
    architecture doc says so explicitly), so a contact could otherwise
    disguise a link as trusted text -- e.g. "[click here](evil.example)" --
    and have Telegram render it as a real, differently-targeted hyperlink.
    Sending with NO parse_mode at all guarantees Telegram shows the text
    exactly as given; Telegram's own always-on auto-linking of literal URLs
    is unrelated and unaffected (same text stays visible either way)."""
    from agent.secret_scope import get_secret

    token = get_secret("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured -- cannot deliver notification")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": authorized_user_id, "text": text},
        timeout=_TELEGRAM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API rejected the notification: {body.get('description')}")


def handle_event(*, authorization_header: Optional[str], body: bytes) -> tuple[int, dict]:
    """The ingress handler's core logic, kept import-light and directly
    unit-testable without a real socket. Side effects (only on a genuinely
    new, valid event): one store write, one Telegram send. Returns
    (http_status, response_body). On success the body is EXACTLY
    {"acknowledged": true, "event_id": ...} -- the shape WhatsApp Agent's
    own OutboxDispatcher requires before it marks the event delivered (see
    D:\\AI Agent\\Whatsapp AI docs/architecture/README.md)."""
    try:
        cfg = config.load()
    except config.ConfigError:
        return 503, {"error": "BRIDGE_NOT_CONFIGURED"}

    bearer = None
    if authorization_header and authorization_header.startswith("Bearer "):
        bearer = authorization_header[len("Bearer "):]
    if bearer is None or not hmac.compare_digest(bearer, cfg.shared_secret):
        return 401, {"error": "UNAUTHENTICATED"}

    if len(body) > _MAX_BODY_BYTES:
        return 400, {"error": "PAYLOAD_TOO_LARGE"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "MALFORMED"}
    if not isinstance(payload, dict):
        return 400, {"error": "MALFORMED"}

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    card = payload.get("card")
    if not isinstance(event_id, str) or not event_id:
        return 400, {"error": "INVALID_EVENT_ID"}
    if not isinstance(event_type, str) or event_type not in _ALLOWED_EVENT_TYPES:
        return 400, {"error": "INVALID_EVENT_TYPE"}
    if not isinstance(card, dict):
        return 400, {"error": "INVALID_CARD"}

    card_type = card.get("card_type", "")
    actions = cards.extract_actions(card)
    if not store.claim_and_store_event(event_id, event_id, card_type, card, actions):
        # Duplicate push (e.g. a retry after a lost ack) -- ack again, never
        # re-notify. Claim and storage commit in one transaction (see
        # store.claim_and_store_event's docstring), so this is never a
        # "claimed but not actually stored" false duplicate.
        return 200, {"acknowledged": True, "event_id": event_id}

    try:
        _notify(cfg.authorized_user_id, render_card(card))
        store.mark_delivered(event_id)
    except Exception:
        # Already claimed/stored -- a failed Telegram send must not be
        # reported back to WhatsApp Agent as an unacknowledged event (that
        # would make its dispatcher retry forever); the thread is still
        # queryable later via whatsapp_agent_get_current.
        logger.exception("whatsapp_agent_bridge: notification delivery failed for %s", event_id)

    return 200, {"acknowledged": True, "event_id": event_id}


class _Handler(BaseHTTPRequestHandler):
    # Bounds how long a single connection may sit idle (e.g. a slow-loris-
    # style client trickling headers/body one byte at a time) before this
    # thread gives up on it. Matters once this listener gets a public
    # Railway domain mapping -- harmless today while it's unreachable.
    timeout = 30

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence default access log
        pass  # never log request lines -- may include the Authorization header via some log configs

    def do_POST(self) -> None:  # noqa: N802
        if self.path != _INGRESS_PATH:
            self._respond(404, {"error": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._respond(400, {"error": "MALFORMED"})
            return
        if length > _MAX_BODY_BYTES:
            self._respond(400, {"error": "PAYLOAD_TOO_LARGE"})
            return
        body = self.rfile.read(length) if length > 0 else b""
        status, response_body = handle_event(authorization_header=self.headers.get("Authorization"), body=body)
        self._respond(status, response_body)

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


_server: Optional[ThreadingHTTPServer] = None
_start_lock = threading.Lock()


def start_ingress_server() -> None:
    """Idempotent -- one listener for the process lifetime, same discipline
    as tax_content_bridge.poller.start_poller(). Binding happens once, at
    startup; a config change on Railway requires a redeploy (which restarts
    the process) to take effect here, same as any other env-var change."""
    global _server
    with _start_lock:
        if _server is not None:
            return
        if not config.is_configured():
            logger.info("whatsapp_agent_bridge: ingress not started -- bridge not enabled/fully configured")
            return
        cfg = config.load()
        server = ThreadingHTTPServer((cfg.ingress_host, cfg.ingress_port), _Handler)
        _server = server
        threading.Thread(target=server.serve_forever, name="whatsapp-agent-bridge-ingress", daemon=True).start()
        logger.info("whatsapp_agent_bridge: ingress listening on %s:%s%s", cfg.ingress_host, cfg.ingress_port, _INGRESS_PATH)
