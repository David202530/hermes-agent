"""Pure rendering: a WhatsApp Agent HermesCard -> exact plain text.

No Markdown/HTML parsing, no Unicode normalization, no rewriting. The
Agent's own domain/text-safety.ts already reveals unsafe/invisible
characters as visible [U+XXXX] markers before a card ever reaches this
plugin (see D:\\AI Agent\\Whatsapp AI docs/whatsapp-agent/PHASE_5.md,
"Control and invisible characters" / "Relay requirements": render every
card string as plain text, show every field/notice/button unmodified).
This module must pass that text through byte-for-byte -- it never escapes,
strips, or re-encodes anything the Agent already decided to show.
"""
from __future__ import annotations

from typing import Optional


def render_card(card: dict) -> str:
    lines: list[str] = []
    title = card.get("title")
    if title:
        lines.append(str(title))
    for field in card.get("fields") or []:
        label = field.get("label")
        value = field.get("value")
        if label is not None and value is not None:
            lines.append(f"{label}: {value}")
    notice = card.get("notice")
    if notice:
        lines.append(str(notice))
    actions = card.get("actions") or []
    if actions:
        # Informational only -- this is never rendered as a Telegram inline
        # button (Phase 1 decision: natural-language tool calls only).
        seen: list[str] = []
        for action in actions:
            label = action.get("label")
            if label and label not in seen:
                seen.append(str(label))
        if seen:
            lines.append("Available: " + ", ".join(seen))
    return "\n".join(lines)


def render_error(error_code: Optional[str], card: Optional[dict]) -> str:
    if card:
        return render_card(card)
    return f"WhatsApp Agent error: {error_code}"
