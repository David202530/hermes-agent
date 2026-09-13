"""tax-content-bridge -- Hermes plugin connecting to the Tax Content
Intelligence Agent's `src/hermes_integration/action_gateway.py` bridge
(see that repo's `docs/HERMES_INTEGRATION.md` for the full contract,
rationale, and tunnel-setup instructions).

Uses `register_tool` (agent-callable tools) -- never `pre_gateway_dispatch`
or any other raw-text lifecycle hook. A prior attempt at this integration
used that hook and was abandoned after live testing showed hook delivery
itself was unreliable even though the plugin code was correct; register_tool
is Hermes's primary, most-exercised extension point instead.

This plugin never talks to LinkedIn, never calls a Publisher, and never
implements tax reasoning, evidence checking, Writer, Style Gate, Human
Loop, or approval logic itself. Every tool is a thin, schema-validated
forward to the Tax Agent's own allowlisted action_gateway, which remains
the sole authority for all of that. PUBLISH_LINKEDIN is deliberately not
exposed as a tool this stage -- publication stays disabled end to end.

Inert until configured: TAX_AGENT_BRIDGE_URL, TAX_AGENT_BRIDGE_SHARED_SECRET,
and TAX_AGENT_BRIDGE_CHAT_ID must all be set (see plugin.yaml) or every
tool call and the background poller both fail closed with a clear error /
idle quietly, never a crash or a silent fallback.

Tools are registered in every process that discovers plugins (gateway,
and `hermes serve` when a dashboard request happens to trigger discovery --
see hermes_cli/web_server.py), so introspection/UI surfaces see this
plugin consistently everywhere. The background poller is different: it is
a single outbound network loop that must run exactly once per deployment,
not once per process. HERMES_GATEWAY_PROCESS is set in hermes_cli/main.py
BEFORE the first plugin discovery pass for `hermes gateway run` (not
inside gateway/run.py's own later, already-idempotent-and-thus-too-late
discover_plugins() call -- discovery only truly runs once per process,
and register() will not be re-invoked on a later pass), specifically so
this check sees the correct value on the one pass that actually calls
register().
"""
from __future__ import annotations

import os

from . import tools
from .poller import start_poller
from .tools import check_bridge_configured

_TOOLS = (
    ("tax_agent_status", tools.TAX_AGENT_STATUS_SCHEMA, tools._handle_tax_agent_status, "\U0001fa7a"),
    ("tax_agent_get_workflow_status", tools.TAX_AGENT_GET_WORKFLOW_STATUS_SCHEMA,
     tools._handle_tax_agent_get_workflow_status, "\U0001f4cb"),
    ("tax_agent_list_topics", tools.TAX_AGENT_LIST_TOPICS_SCHEMA, tools._handle_tax_agent_list_topics, "\U0001f4da"),
    ("tax_agent_select_topic", tools.TAX_AGENT_SELECT_TOPIC_SCHEMA, tools._handle_tax_agent_select_topic, "✅"),
    ("tax_agent_research_more", tools.TAX_AGENT_RESEARCH_MORE_SCHEMA, tools._handle_tax_agent_research_more, "\U0001f50e"),
    ("tax_agent_ask_about_case", tools.TAX_AGENT_ASK_ABOUT_CASE_SCHEMA, tools._handle_tax_agent_ask_about_case, "❓"),
    ("tax_agent_accept_draft", tools.TAX_AGENT_ACCEPT_DRAFT_SCHEMA, tools._handle_tax_agent_accept_draft, "\U0001f4dd"),
    ("tax_agent_request_text_revision", tools.TAX_AGENT_REQUEST_TEXT_REVISION_SCHEMA,
     tools._handle_tax_agent_request_text_revision, "✏️"),
    ("tax_agent_request_visual_revision", tools.TAX_AGENT_REQUEST_VISUAL_REVISION_SCHEMA,
     tools._handle_tax_agent_request_visual_revision, "\U0001f5bc️"),
    ("tax_agent_request_combined_revision", tools.TAX_AGENT_REQUEST_COMBINED_REVISION_SCHEMA,
     tools._handle_tax_agent_request_combined_revision, "\U0001f501"),
    ("tax_agent_approve_final_package", tools.TAX_AGENT_APPROVE_FINAL_PACKAGE_SCHEMA,
     tools._handle_tax_agent_approve_final_package, "\U0001f4e6"),
)


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="tax_content_bridge",
            schema=schema,
            handler=handler,
            check_fn=check_bridge_configured,
            emoji=emoji,
        )
    if os.environ.get("HERMES_GATEWAY_PROCESS") == "1":
        start_poller()
