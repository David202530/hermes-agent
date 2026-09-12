"""Agent-callable tools for the tax-content-bridge plugin.

Each tool maps 1:1 onto exactly one allowlisted Tax Agent action_type (see
the Tax Agent repo's docs/HERMES_INTEGRATION.md) and is a thin,
schema-validated forward to bridge_client.send_action -- no tax reasoning,
Writer, approval, or Publisher logic lives here, and no tool accepts an
arbitrary action name, shell command, file path, or Publisher call.
PUBLISH_LINKEDIN is deliberately not exposed as a tool this stage.
"""
from __future__ import annotations

from tools.registry import tool_error, tool_result

from .bridge_client import BridgeError, is_configured, send_action

_NO_PARAMS = {"type": "object", "properties": {}}


def check_bridge_configured() -> bool:
    """`check_fn` for every tool below: registered (visible in `hermes
    tools`) but dispatch is refused with a clear error until the bridge
    env vars are actually set -- same pattern plugins/spotify uses for its
    own auth gate."""
    return is_configured()


def _run(action_type: str, **kwargs) -> str:
    try:
        result = send_action(action_type, **kwargs)
    except BridgeError as exc:
        return tool_error(str(exc))
    return tool_result(result)


def _handle_tax_agent_status(args: dict, **kw) -> str:
    return _run("GET_WORKFLOW_STATUS")


def _handle_tax_agent_get_workflow_status(args: dict, **kw) -> str:
    return _run("GET_WORKFLOW_STATUS")


def _handle_tax_agent_list_topics(args: dict, **kw) -> str:
    return _run("RUN_DISPUTE_SHORTLIST")


def _handle_tax_agent_select_topic(args: dict, **kw) -> str:
    case_id = str(args.get("case_id") or "").strip()
    if not case_id:
        return tool_error("case_id is required")
    return _run("APPROVE_CASE_TOPIC", artifact_id=case_id)


def _handle_tax_agent_research_more(args: dict, **kw) -> str:
    case_id = str(args.get("case_id") or "").strip() or None
    # A case_id only makes sense while topic cards are the presented
    # artifact set; once a draft/package exists there is a single active
    # artifact and no id to disambiguate -- see queue_schema's stage map.
    action_type = "RESEARCH_CASE_MORE" if case_id else "REQUEST_RESEARCH_MORE"
    return _run(action_type, artifact_id=case_id)


def _handle_tax_agent_ask_about_case(args: dict, **kw) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        return tool_error("question is required")
    case_id = str(args.get("case_id") or "").strip() or None
    return _run("ASK_ABOUT_CASE", artifact_id=case_id, revision_instruction=question)


def _handle_tax_agent_accept_draft(args: dict, **kw) -> str:
    return _run("ACCEPT_DRAFT_FOR_NEXT_STAGE")


def _handle_tax_agent_request_text_revision(args: dict, **kw) -> str:
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return tool_error("instruction is required")
    return _run("REQUEST_TEXT_REVISION", revision_instruction=instruction)


def _handle_tax_agent_request_visual_revision(args: dict, **kw) -> str:
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return tool_error("instruction is required")
    return _run("REQUEST_VISUAL_REVISION", revision_instruction=instruction)


def _handle_tax_agent_request_combined_revision(args: dict, **kw) -> str:
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return tool_error("instruction is required")
    return _run("REQUEST_COMBINED_REVISION", revision_instruction=instruction)


def _handle_tax_agent_approve_final_package(args: dict, **kw) -> str:
    return _run("APPROVE_FINAL_PACKAGE")


TAX_AGENT_STATUS_SCHEMA = {
    "name": "tax_agent_status",
    "description": (
        "Check whether the Tax Content Intelligence Agent bridge is reachable. "
        "Read-only, no workflow mutation. Use this for a simple connectivity check."
    ),
    "parameters": _NO_PARAMS,
}

TAX_AGENT_GET_WORKFLOW_STATUS_SCHEMA = {
    "name": "tax_agent_get_workflow_status",
    "description": "Get the current Tax Agent workflow status for this conversation. Read-only.",
    "parameters": _NO_PARAMS,
}

TAX_AGENT_LIST_TOPICS_SCHEMA = {
    "name": "tax_agent_list_topics",
    "description": (
        "Ask the Tax Agent to run its full-corpus dispute shortlist and present the top tax "
        "topics. Call this when David asks to see today's tax topics (e.g. \"მაჩვენე "
        "საგადასახადო თემები\"). "
        "Never invent topics yourself -- only the Tax Agent's own result is authoritative."
    ),
    "parameters": _NO_PARAMS,
}

TAX_AGENT_SELECT_TOPIC_SCHEMA = {
    "name": "tax_agent_select_topic",
    "description": (
        "Approve/select one of the topic cards the Tax Agent already presented, to start "
        "drafting. Use the EXACT case_id from the card David is referring to -- never invent "
        "or guess one. This only expresses topic interest; it never approves a draft or "
        "publishes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "The exact case_id from the topic card David selected."},
        },
        "required": ["case_id"],
    },
}

TAX_AGENT_RESEARCH_MORE_SCHEMA = {
    "name": "tax_agent_research_more",
    "description": (
        "Ask the Tax Agent to research a topic or the current draft/package more deeply "
        "(e.g. \"უფრო ღრმად გამოიკვლიე"
        "\"). Pass case_id ONLY when David is clearly referring to a specific topic card that "
        "hasn't become a draft yet; omit it once there is already a single active draft/package."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "Optional: the exact case_id from a topic card, if that's what David means.",
            },
        },
        "required": [],
    },
}

TAX_AGENT_ASK_ABOUT_CASE_SCHEMA = {
    "name": "tax_agent_ask_about_case",
    "description": (
        "Ask the Tax Agent a question about a case/topic/draft it presented. Purely "
        "informational -- it never approves, changes, or publishes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "David's exact question, unparaphrased."},
            "case_id": {
                "type": "string",
                "description": "Optional: the exact case_id from a topic card, if David is asking about a specific one.",
            },
        },
        "required": ["question"],
    },
}

TAX_AGENT_ACCEPT_DRAFT_SCHEMA = {
    "name": "tax_agent_accept_draft",
    "description": (
        "Accept the CURRENT DRAFT TEXT for the next stage -- this starts premium infographic "
        "generation. Only call this when David has clearly approved the draft text itself. "
        "This is DIFFERENT from topic interest and from final-package approval -- never call "
        "it for either of those, and never treat it as publishing anything."
    ),
    "parameters": _NO_PARAMS,
}

TAX_AGENT_REQUEST_TEXT_REVISION_SCHEMA = {
    "name": "tax_agent_request_text_revision",
    "description": (
        "Request a free-text revision to the current draft or final package's TEXT. Pass "
        "David's instruction through exactly as he said it -- do not paraphrase, shorten, or "
        "reinterpret it; the Tax Agent's own Writer applies the actual revision."
    ),
    "parameters": {
        "type": "object",
        "properties": {"instruction": {"type": "string", "description": "David's exact revision instruction."}},
        "required": ["instruction"],
    },
}

TAX_AGENT_REQUEST_VISUAL_REVISION_SCHEMA = {
    "name": "tax_agent_request_visual_revision",
    "description": (
        "Request a free-text revision to the current final package's INFOGRAPHIC/visual. Pass "
        "David's instruction through exactly as he said it."
    ),
    "parameters": {
        "type": "object",
        "properties": {"instruction": {"type": "string", "description": "David's exact revision instruction."}},
        "required": ["instruction"],
    },
}

TAX_AGENT_REQUEST_COMBINED_REVISION_SCHEMA = {
    "name": "tax_agent_request_combined_revision",
    "description": (
        "Request a free-text revision to BOTH the text and the infographic of the current final "
        "package in one go. Pass David's instruction through exactly as he said it."
    ),
    "parameters": {
        "type": "object",
        "properties": {"instruction": {"type": "string", "description": "David's exact revision instruction."}},
        "required": ["instruction"],
    },
}

TAX_AGENT_APPROVE_FINAL_PACKAGE_SCHEMA = {
    "name": "tax_agent_approve_final_package",
    "description": (
        "Approve the current final package (exact approved text + exact infographic). This does "
        "NOT publish to LinkedIn -- publication is a separate, later, explicit action this "
        "plugin does not expose yet. Never call this for topic interest or draft-text approval "
        "alone, and never infer publishing permission from David saying he likes something."
    ),
    "parameters": _NO_PARAMS,
}
