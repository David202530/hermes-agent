"""whatsapp-agent-bridge -- Hermes plugin connecting to David's own,
locally-run WhatsApp Agent (a separate repo, D:\\AI Agent\\Whatsapp AI) via
its Phase 5 Hermes relay contract (see that repo's
docs/whatsapp-agent/PHASE_5.md for the full card/token contract).

Uses register_tool exclusively for outbound actions (Hermes -> Agent),
exactly like plugins/tax_content_bridge. Unlike that plugin, WhatsApp
Agent's own event delivery is PUSH-based (its HermesEventSink posts to a
Hermes-side URL) rather than pull-based -- there is no GET-style
pending-events endpoint on the Agent to poll (confirmed: no such route
anywhere in that repo's src/ tree). So this plugin also owns a small
inbound HTTP listener (ingress.py) instead of a poller. No other Hermes
extension point fits this: the plugin framework's
register_tool/register_cli_command/register_hook/register_middleware carry
no HTTP route, and register_platform's BasePlatformAdapter is the full
bidirectional chat-platform contract (session/typing/media/streaming) --
disproportionate for one Bearer-authenticated JSON POST. See ingress.py's
own docstring and this plugin's README for the full rationale and the
resulting deployment requirement (a future Railway domain/target-port
mapping onto WHATSAPP_AGENT_BRIDGE_PORT -- not created here; this change is
implementation and local verification only, no deploy).

This plugin never imports whatsapp-web.js, never touches a phone number,
WhatsApp internal ID, or LocalAuth/session data (none of that is ever on
the wire it receives -- see HermesCard's own scope note in WhatsApp
Agent's src/whatsapp-agent/hermes/cards.ts), and never decides whether a
draft or send authorization is valid. WhatsApp Agent's own HumanSendService
remains the sole, canonical authority for all of that; every tool here is a
thin, schema-validated forward of a button's own opaque action_token,
exactly as Phase 5 requires ("Hermes is a generic relay").

Inert until configured: WHATSAPP_AGENT_BRIDGE_ENABLED, _SHARED_SECRET,
_CALLBACK_URL, and _AUTHORIZED_USER_ID must all be set (see config.py) or
every tool call fails closed and the ingress listener never binds a port.

HERMES_GATEWAY_PROCESS gating follows tax_content_bridge's own established
placement (hermes_cli/main.py, set immediately before
_prepare_agent_startup(args), before the one real plugin discovery pass for
`hermes gateway run`) -- the ingress listener, like that plugin's poller,
must run exactly once per deployment, not once per process.
"""
from __future__ import annotations

import os

from . import tools
from .config import is_configured
from .ingress import start_ingress_server

_TOOLS = (
    ("whatsapp_agent_list_pending", tools.WHATSAPP_AGENT_LIST_PENDING_SCHEMA, tools._handle_list_pending, "\U0001f4cb"),
    ("whatsapp_agent_get_current", tools.WHATSAPP_AGENT_GET_CURRENT_SCHEMA, tools._handle_get_current, "\U0001f4f1"),
    ("whatsapp_agent_draft_reply", tools.WHATSAPP_AGENT_DRAFT_REPLY_SCHEMA, tools._handle_draft_reply, "\U0001f4dd"),
    ("whatsapp_agent_regenerate_draft", tools.WHATSAPP_AGENT_REGENERATE_DRAFT_SCHEMA, tools._handle_regenerate_draft, "\U0001f504"),
    ("whatsapp_agent_approve_draft", tools.WHATSAPP_AGENT_APPROVE_DRAFT_SCHEMA, tools._handle_approve_draft, "\u2705"),
    ("whatsapp_agent_send_now", tools.WHATSAPP_AGENT_SEND_NOW_SCHEMA, tools._handle_send_now, "\U0001f4e4"),
    ("whatsapp_agent_cancel_send", tools.WHATSAPP_AGENT_CANCEL_SEND_SCHEMA, tools._handle_cancel_send, "\u274c"),
    ("whatsapp_agent_remind_later", tools.WHATSAPP_AGENT_REMIND_LATER_SCHEMA, tools._handle_remind_later, "\u23f0"),
    ("whatsapp_agent_dismiss", tools.WHATSAPP_AGENT_DISMISS_SCHEMA, tools._handle_dismiss, "\U0001f5d1"),
    ("whatsapp_agent_resolve_unknown_send", tools.WHATSAPP_AGENT_RESOLVE_UNKNOWN_SEND_SCHEMA, tools._handle_resolve_unknown_send, "\u2753"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="whatsapp_agent_bridge",
            schema=schema,
            handler=handler,
            check_fn=tools.check_bridge_configured,
            emoji=emoji,
        )
    if os.environ.get("HERMES_GATEWAY_PROCESS") == "1":
        start_ingress_server()
