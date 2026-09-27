"""Shared action-id vocabulary and card helpers, mirrored from WhatsApp
Agent's own src/whatsapp-agent/hermes/cards.ts (HERMES_CARD_ACTIONS /
ACTION_TARGETS, repo: D:\\AI Agent\\Whatsapp AI). These action ids are the
Agent's own fixed wire vocabulary -- not secret, just the shape of its
contract -- so mirroring the literal strings here is safe and exact.
"""
from __future__ import annotations

DRAFT_REPLY = "DRAFT_REPLY"
REMIND_LATER = "REMIND_LATER"
REMIND_30_MINUTES = "REMIND_30_MINUTES"
REMIND_1_HOUR = "REMIND_1_HOUR"
REMIND_3_HOURS = "REMIND_3_HOURS"
REMIND_TOMORROW = "REMIND_TOMORROW"
DISMISS = "DISMISS"
APPROVE_DRAFT = "APPROVE_DRAFT"
REGENERATE_DRAFT = "REGENERATE_DRAFT"
DISMISS_DRAFT = "DISMISS_DRAFT"
SEND_NOW = "SEND_NOW"
CANCEL_SEND = "CANCEL_SEND"
RESOLVE_UNKNOWN_SEND = "RESOLVE_UNKNOWN_SEND"

SNOOZE_CHOICES = {
    "30_minutes": REMIND_30_MINUTES,
    "1_hour": REMIND_1_HOUR,
    "3_hours": REMIND_3_HOURS,
    "tomorrow": REMIND_TOMORROW,
}

# card_type values (from HermesCardType in cards.ts) that mean "nothing more
# to do here": a completed send result, or a fixed notice (approval expired,
# conversation changed, etc.) that offers no further action. A thread whose
# current card is one of these no longer counts as "pending" -- see
# store.list_active_threads(), whose ambiguity check depends on stale,
# resolved threads dropping out of the active set rather than accumulating
# forever.
TERMINAL_CARD_TYPES = frozenset({"WHATSAPP_SEND_RESULT", "WHATSAPP_NOTICE"})


def extract_actions(card: dict) -> list[dict]:
    """card["actions"] verbatim, filtered to well-formed
    {action_id, label, action_token} entries. Tokens pass through as opaque
    strings only -- never decoded, never altered, never fabricated."""
    out: list[dict] = []
    for action in card.get("actions") or []:
        action_id = action.get("action_id")
        label = action.get("label")
        token = action.get("action_token")
        if isinstance(action_id, str) and isinstance(label, str) and isinstance(token, str):
            out.append({"action_id": action_id, "label": label, "action_token": token})
    return out
