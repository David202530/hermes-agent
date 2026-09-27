"""Configuration for the whatsapp-agent-bridge plugin.

Fail-closed: WHATSAPP_AGENT_BRIDGE_ENABLED=true with any missing or invalid
required value means every tool call is refused and the ingress listener
never binds a port -- mirrors tax_content_bridge/bridge_client.py's
is_configured() and WhatsApp Agent's own loadConfig() fail-closed style
(D:\\AI Agent\\Whatsapp AI src/whatsapp-agent/config.ts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from agent.secret_scope import get_secret

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_INGRESS_PORT = 8878
INGRESS_PATH = "/whatsapp_agent_bridge/events"
CALLBACK_PATH = "/hermes/actions"


class ConfigError(Exception):
    """Raised by load() when the bridge is enabled but config is incomplete
    or invalid. Callers that only need a passive yes/no should call
    is_configured() instead (same split as tax_content_bridge)."""


def _is_valid_callback_url(url: str) -> bool:
    """A bare HTTPS origin only -- no path/query -- so a tool can never be
    tricked into hitting an attacker-chosen path via a "clever" configured
    value; the callback path is always the hardcoded CALLBACK_PATH. http is
    accepted only for localhost/127.0.0.1 (same-machine dev, no network
    hop), matching tax_content_bridge's own bridge_client._is_valid_bridge_url."""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS


def _is_valid_user_id(value: str) -> bool:
    return bool(value) and value.isdigit()


@dataclass(frozen=True)
class BridgeConfig:
    shared_secret: str
    callback_url: str
    authorized_user_id: str
    cf_access_client_id: Optional[str]
    cf_access_client_secret: Optional[str]
    ingress_host: str
    ingress_port: int


def _raw() -> dict:
    return {
        "enabled": (get_secret("WHATSAPP_AGENT_BRIDGE_ENABLED", "false") or "false").strip().lower() == "true",
        "shared_secret": get_secret("WHATSAPP_AGENT_SHARED_SECRET", "") or "",
        "callback_url": (get_secret("WHATSAPP_AGENT_CALLBACK_URL", "") or "").rstrip("/"),
        "authorized_user_id": get_secret("WHATSAPP_AGENT_AUTHORIZED_USER_ID", "") or "",
        "cf_id": get_secret("WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID", "") or "",
        "cf_secret": get_secret("WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET", "") or "",
        "ingress_host": get_secret("WHATSAPP_AGENT_BRIDGE_HOST", "") or "0.0.0.0",
        "ingress_port": get_secret("WHATSAPP_AGENT_BRIDGE_PORT", "") or str(DEFAULT_INGRESS_PORT),
    }


def _validate(raw: dict) -> Optional[str]:
    """Returns an error message, or None if everything required is valid."""
    if not raw["enabled"]:
        return "WHATSAPP_AGENT_BRIDGE_ENABLED is not true"
    if not raw["shared_secret"] or len(raw["shared_secret"]) < 32:
        return "WHATSAPP_AGENT_SHARED_SECRET must be at least 32 characters"
    if not _is_valid_callback_url(raw["callback_url"]):
        return (
            "WHATSAPP_AGENT_CALLBACK_URL must be a bare HTTPS origin "
            "(http only accepted for localhost/127.0.0.1), no path or query"
        )
    if not _is_valid_user_id(raw["authorized_user_id"]):
        return "WHATSAPP_AGENT_AUTHORIZED_USER_ID must be numeric"
    if bool(raw["cf_id"]) != bool(raw["cf_secret"]):
        return (
            "WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID and WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET "
            "must be set together or not at all"
        )
    try:
        int(raw["ingress_port"])
    except ValueError:
        return "WHATSAPP_AGENT_BRIDGE_PORT must be an integer"
    return None


def is_configured() -> bool:
    return _validate(_raw()) is None


def load() -> BridgeConfig:
    raw = _raw()
    error = _validate(raw)
    if error is not None:
        raise ConfigError(error)
    return BridgeConfig(
        shared_secret=raw["shared_secret"],
        callback_url=raw["callback_url"],
        authorized_user_id=raw["authorized_user_id"],
        cf_access_client_id=raw["cf_id"] or None,
        cf_access_client_secret=raw["cf_secret"] or None,
        ingress_host=raw["ingress_host"],
        ingress_port=int(raw["ingress_port"]),
    )
