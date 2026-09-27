"""Agent-callable tools for the whatsapp-agent-bridge plugin.

Every tool is a thin, schema-validated forward to WhatsApp Agent's own
Phase 5 opaque-token callback (see D:\\AI Agent\\Whatsapp AI
docs/whatsapp-agent/PHASE_5.md) -- no drafting, approval, classification,
or send logic lives here, and no tool can decode, fabricate, or reuse a
stale action token. A tool that finds no current Agent-issued token for
its action fails closed with a plain message; it never guesses.
"""
from __future__ import annotations

from tools.registry import tool_error, tool_result

from . import callback_client, cards, config, store
from .render import render_card, render_error


def check_bridge_configured() -> bool:
    """`check_fn` for every tool below: registered (visible in `hermes
    tools`) but dispatch is refused with a clear error until the bridge env
    vars are actually set -- same pattern plugins/tax_content_bridge uses
    for its own auth gate."""
    return config.is_configured()


def _current_thread():
    """Returns (thread, None) or (None, tool_error_string). Fails closed
    (asks the user to disambiguate via whatsapp_agent_list_pending) rather
    than silently guessing which thread "the current one" means whenever
    more than one is active at once -- acting on the wrong contact's thread
    is a real risk here even though WhatsApp Agent's own token binding
    still prevents any actual cross-conversation send."""
    threads = store.list_active_threads()
    if not threads:
        return None, tool_error("Nothing currently pending from the WhatsApp Agent.")
    if len(threads) > 1:
        return None, tool_error(
            "More than one WhatsApp thread is currently pending -- use whatsapp_agent_list_pending "
            "to see them; acting on 'the current one' is ambiguous right now."
        )
    return threads[0], None


def _dispatch(thread_id: str, action_id: str, action_token: str) -> str:
    try:
        result = callback_client.send_action(action_id, action_token)
    except (config.ConfigError, callback_client.CallbackError) as exc:
        return tool_error(str(exc))

    card = result.get("card")
    if isinstance(card, dict):
        store.store_thread(thread_id, card.get("card_type", ""), card, cards.extract_actions(card))

    error_code = result.get("error")
    if error_code:
        return tool_result({"outcome": "error", "code": error_code, "message": render_error(error_code, card)})
    return tool_result({"outcome": result.get("status", "APPLIED"), "message": render_card(card or {})})


def _act_on_current(action_id: str) -> str:
    thread, error = _current_thread()
    if error:
        return error
    token = store.find_token(thread["thread_id"], action_id)
    if token is None:
        return tool_error(f"No current '{action_id}' action available from the WhatsApp Agent -- nothing to do.")
    return _dispatch(thread["thread_id"], action_id, token["action_token"])


_NO_PARAMS = {"type": "object", "properties": {}}

WHATSAPP_AGENT_LIST_PENDING_SCHEMA = {
    "name": "whatsapp_agent_list_pending",
    "description": (
        "List every WhatsApp thread this Hermes instance currently has an open card for "
        "(reminders, drafts, or confirmations already relayed here). This is Hermes's own "
        "local view, not a live query of the WhatsApp Agent -- it only reflects what has "
        "already been pushed or returned so far."
    ),
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_GET_CURRENT_SCHEMA = {
    "name": "whatsapp_agent_get_current",
    "description": "Show the current WhatsApp card (reminder, draft, or confirmation) Hermes has on file, if any.",
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_DRAFT_REPLY_SCHEMA = {
    "name": "whatsapp_agent_draft_reply",
    "description": (
        "Ask the WhatsApp Agent to draft a reply to the current pending WhatsApp message. "
        "Returns the Agent's own generated draft; this never generates or edits text itself."
    ),
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_REGENERATE_DRAFT_SCHEMA = {
    "name": "whatsapp_agent_regenerate_draft",
    "description": "Ask the WhatsApp Agent to regenerate the current draft reply.",
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_APPROVE_DRAFT_SCHEMA = {
    "name": "whatsapp_agent_approve_draft",
    "description": (
        "Approve the current draft reply. This never sends anything by itself -- the WhatsApp "
        "Agent returns a final confirmation card with the exact text that would be sent; a "
        "separate whatsapp_agent_send_now call is required to actually send it."
    ),
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_SEND_NOW_SCHEMA = {
    "name": "whatsapp_agent_send_now",
    "description": (
        "Send the currently approved WhatsApp reply. Only works if the WhatsApp Agent has "
        "already issued a current, unexpired send authorization (i.e. approve_draft succeeded "
        "and its confirmation is still current) -- otherwise this fails closed with no send."
    ),
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_CANCEL_SEND_SCHEMA = {
    "name": "whatsapp_agent_cancel_send",
    "description": "Cancel the current pending send authorization without sending anything.",
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_REMIND_LATER_SCHEMA = {
    "name": "whatsapp_agent_remind_later",
    "description": (
        "Snooze the current WhatsApp reminder. Call with no arguments to ask the WhatsApp Agent "
        "for the snooze-choice options; call again with `choice` set to one of the options it "
        "returned to actually pick one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "choice": {
                "type": "string",
                "enum": list(cards.SNOOZE_CHOICES.keys()),
                "description": "One of the snooze options the WhatsApp Agent's snooze-choice card offered.",
            },
        },
    },
}

WHATSAPP_AGENT_DISMISS_SCHEMA = {
    "name": "whatsapp_agent_dismiss",
    "description": (
        "Dismiss the current WhatsApp reminder or draft (whichever is current). Dismissing a "
        "draft discards only the draft -- the underlying reminder stays open."
    ),
    "parameters": _NO_PARAMS,
}

WHATSAPP_AGENT_RESOLVE_UNKNOWN_SEND_SCHEMA = {
    "name": "whatsapp_agent_resolve_unknown_send",
    "description": (
        "Resolve a WhatsApp send whose outcome came back unclear, after checking the phone "
        "directly. Use resolution='sent' only if the message is visibly there, 'not_sent' "
        "otherwise."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "resolution": {
                "type": "string",
                "enum": ["sent", "not_sent"],
                "description": "Whether the message actually went through, per manual confirmation on the phone.",
            },
        },
        "required": ["resolution"],
    },
}


def _handle_list_pending(args: dict, **kw) -> str:
    threads = store.list_active_threads()
    if not threads:
        return tool_result({"pending": []})
    return tool_result({
        "pending": [
            {"card_type": t["card_type"], "summary": render_card(t["card"])}
            for t in threads
        ],
    })


def _handle_get_current(args: dict, **kw) -> str:
    thread, error = _current_thread()
    if error:
        return error
    return tool_result({"card_type": thread["card_type"], "message": render_card(thread["card"])})


def _handle_draft_reply(args: dict, **kw) -> str:
    return _act_on_current(cards.DRAFT_REPLY)


def _handle_regenerate_draft(args: dict, **kw) -> str:
    return _act_on_current(cards.REGENERATE_DRAFT)


def _handle_approve_draft(args: dict, **kw) -> str:
    return _act_on_current(cards.APPROVE_DRAFT)


def _handle_send_now(args: dict, **kw) -> str:
    return _act_on_current(cards.SEND_NOW)


def _handle_cancel_send(args: dict, **kw) -> str:
    return _act_on_current(cards.CANCEL_SEND)


def _handle_remind_later(args: dict, **kw) -> str:
    choice = str(args.get("choice") or "").strip()
    if not choice:
        return _act_on_current(cards.REMIND_LATER)
    action_id = cards.SNOOZE_CHOICES.get(choice)
    if action_id is None:
        return tool_error(f"choice must be one of: {', '.join(cards.SNOOZE_CHOICES.keys())}")
    return _act_on_current(action_id)


def _handle_dismiss(args: dict, **kw) -> str:
    thread, error = _current_thread()
    if error:
        return error
    for action_id in (cards.DISMISS, cards.DISMISS_DRAFT):
        token = store.find_token(thread["thread_id"], action_id)
        if token is not None:
            return _dispatch(thread["thread_id"], action_id, token["action_token"])
    return tool_error("No current dismiss action available from the WhatsApp Agent.")


def _handle_resolve_unknown_send(args: dict, **kw) -> str:
    resolution = str(args.get("resolution") or "").strip().lower()
    if resolution not in ("sent", "not_sent"):
        return tool_error("resolution must be 'sent' or 'not_sent'")
    thread, error = _current_thread()
    if error:
        return error
    candidates = store.find_tokens(thread["thread_id"], cards.RESOLVE_UNKNOWN_SEND)
    wanted_not = resolution == "not_sent"
    picked = [c for c in candidates if ("not" in c["label"].lower()) == wanted_not]
    if len(picked) != 1:
        return tool_error("No current action available for that resolution.")
    return _dispatch(thread["thread_id"], cards.RESOLVE_UNKNOWN_SEND, picked[0]["action_token"])
