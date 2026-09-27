"""SSRF-hardened HTTP client from this plugin to WhatsApp Agent's own
POST /hermes/actions (see D:\\AI Agent\\Whatsapp AI
docs/whatsapp-agent/PHASE_5.md for the full wire contract this implements
against). Never accepts a caller-supplied URL: the base origin comes only
from WHATSAPP_AGENT_CALLBACK_URL (validated at config load -- bare HTTPS
origin, no path/query), and the path this client POSTs to is the hardcoded
CALLBACK_PATH. No tool argument can influence either.

`hermes_user_id` is read from the real, current Telegram session context
(gateway.session_context.get_session_env), never from a tool argument or
static config -- exactly mirroring
plugins/tax_content_bridge/bridge_client.py's `_session_identity()`, and
for the same reason: a tool argument is something the model could fill in
itself from conversational text, which would let it spoof an identity. If
that identity doesn't match WhatsApp Agent's own single allowlisted actor,
the Agent's HermesActionService rejects it (UNAUTHORIZED_ACTOR) -- this
client does not need, and must not attempt, to duplicate that check.
"""
from __future__ import annotations

import uuid
from typing import Optional

import requests

from gateway.session_context import get_session_env

from . import config

_CONNECT_TIMEOUT_SECONDS = 5
_READ_TIMEOUT_SECONDS = 15


class CallbackError(Exception):
    """Raised on any misconfiguration, transport failure, or malformed
    response. tools.py catches this and returns a tool_error -- never lets
    it propagate as a raw traceback into the agent's context."""


def new_request_id() -> str:
    """Matches WhatsApp Agent's own REQUEST_ID_PATTERN
    (^[A-Za-z0-9_-]{8,128}$) -- a uuid4 hex string is 32 lowercase hex
    characters, well within range."""
    return uuid.uuid4().hex


def _session_user_id() -> str:
    user_id = get_session_env("HERMES_SESSION_USER_ID", "")
    if not user_id:
        raise CallbackError("no active Telegram session context for this turn")
    return user_id


def send_action(action_type: str, action_token: str) -> dict:
    """POSTs the exact four-field body WhatsApp Agent's HermesActionService
    requires -- any other field is rejected on its side
    (INVALID_REQUEST_FIELDS). Returns the Agent's own JSON body --
    {status, action_type, card} on success, {error, card?} on a rejection --
    unmodified, plus an internal "_status" key with the HTTP status code, so
    callers render exactly what the Agent said, never a paraphrase."""
    cfg = config.load()
    user_id = _session_user_id()
    payload = {
        "request_id": new_request_id(),
        "action_type": action_type,
        "hermes_user_id": user_id,
        "action_token": action_token,
    }
    headers = {"Authorization": f"Bearer {cfg.shared_secret}"}
    if cfg.cf_access_client_id and cfg.cf_access_client_secret:
        headers["CF-Access-Client-Id"] = cfg.cf_access_client_id
        headers["CF-Access-Client-Secret"] = cfg.cf_access_client_secret

    url = f"{cfg.callback_url}{config.CALLBACK_PATH}"
    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise CallbackError(f"could not reach the WhatsApp Agent bridge: {exc}") from exc

    if 300 <= response.status_code < 400:
        # No redirect is ever legitimate for this fixed, single-purpose
        # endpoint -- treat any 3xx as a hard error rather than follow it
        # (which could silently send the shared secret/token to a
        # different origin).
        raise CallbackError("WhatsApp Agent bridge returned a redirect, which is refused")

    try:
        body = response.json()
    except ValueError:
        raise CallbackError(f"WhatsApp Agent bridge returned a non-JSON response (status {response.status_code})")
    if not isinstance(body, dict):
        raise CallbackError("WhatsApp Agent bridge returned a non-object response")

    return {"_status": response.status_code, **body}
