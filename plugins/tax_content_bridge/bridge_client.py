"""HTTP client from this plugin to the Tax Content Intelligence Agent's
`src/hermes_integration/action_gateway.py` bridge (see that repo's
docs/HERMES_INTEGRATION.md for the full wire contract).

Every call reads the caller's REAL current Telegram identity from the
gateway's own session context -- never from a tool argument, which the
model could fill in itself (that would let conversational text spoof an
identity; see the Tax Agent spec's exact-version/authorization rules).
"""
from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import urlsplit

import requests

from agent.secret_scope import get_secret
from gateway.session_context import get_session_env

_TIMEOUT_SECONDS = 15
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class BridgeError(Exception):
    """Raised on any misconfiguration, auth failure, or non-2xx response.
    Tool handlers catch this and return it as a tool_error -- never let it
    propagate as a raw traceback into the agent's context."""


def _is_valid_bridge_url(url: str) -> bool:
    """HTTPS required for any real (non-local) bridge URL -- a plain http
    tunnel would leak the shared secret and every action payload. http is
    only ever accepted for localhost/127.0.0.1 (same-machine dev, no
    network hop)."""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS


def _is_valid_chat_id(chat_id: str) -> bool:
    return bool(chat_id) and chat_id.isdigit()


def is_configured() -> bool:
    """True only when EVERY value required for a correct round trip is
    present and valid -- inbound tool calls need the URL + secret, but
    outbound delivery (the poller) also needs TAX_AGENT_BRIDGE_CHAT_ID.
    Gating the tools on the URL/secret alone would let David approve
    actions that appear to succeed while the resulting event silently never
    reaches him -- so all three are required here, not just the two the
    inbound path itself touches."""
    url = get_secret("TAX_AGENT_BRIDGE_URL", "") or ""
    secret = get_secret("TAX_AGENT_BRIDGE_SHARED_SECRET", "") or ""
    chat_id = get_secret("TAX_AGENT_BRIDGE_CHAT_ID", "") or ""
    return _is_valid_bridge_url(url) and bool(secret) and _is_valid_chat_id(chat_id)


def _base_url() -> str:
    url = get_secret("TAX_AGENT_BRIDGE_URL", "") or ""
    if not _is_valid_bridge_url(url):
        raise BridgeError(
            "TAX_AGENT_BRIDGE_URL is not configured or is not a valid HTTPS URL "
            "(http is only accepted for localhost/127.0.0.1)"
        )
    return url.rstrip("/")


def _shared_secret() -> str:
    secret = get_secret("TAX_AGENT_BRIDGE_SHARED_SECRET", "") or ""
    if not secret:
        raise BridgeError("TAX_AGENT_BRIDGE_SHARED_SECRET is not configured on this Hermes instance")
    return secret


def _session_identity() -> tuple[str, str]:
    """(hermes_chat_id, hermes_user_id) for the CURRENT inbound turn."""
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    user_id = get_session_env("HERMES_SESSION_USER_ID", "")
    if not chat_id or not user_id:
        raise BridgeError("no active Telegram session context for this turn")
    return chat_id, user_id


def send_action(
    action_type: str,
    *,
    artifact_id: Optional[str] = None,
    revision_instruction: Optional[str] = None,
) -> dict:
    """POSTs one structured action to the Tax Agent bridge. Returns the
    bridge's own JSON response (APPLIED / ALREADY_PROCESSED / a rejection
    code) -- callers pass this straight back as the tool result so the
    model sees exactly what happened, never a paraphrase."""
    chat_id, user_id = _session_identity()
    payload: dict = {
        "action_type": action_type,
        "hermes_chat_id": chat_id,
        "hermes_user_id": user_id,
        "request_id": uuid.uuid4().hex,
    }
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if revision_instruction:
        payload["revision_instruction"] = revision_instruction

    try:
        response = requests.post(
            f"{_base_url()}/hermes/actions", json=payload,
            headers={"Authorization": f"Bearer {_shared_secret()}"}, timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise BridgeError(f"could not reach the Tax Agent bridge: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        raise BridgeError(f"Tax Agent bridge returned a non-JSON response (status {response.status_code})")
    if response.status_code >= 300:
        raise BridgeError(body.get("message") or body.get("error") or f"bridge returned {response.status_code}")
    return body
