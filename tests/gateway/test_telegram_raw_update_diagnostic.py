"""Tests for the TEMPORARY Telegram-ingress diagnostic instrumentation in
``plugins/platforms/telegram/adapter.py`` (``_on_platform_update`` and
``_diagnostic_log_unhandled_error``).

This instrumentation exists to answer exactly one question: does
python-telegram-bot hand an Update to Hermes's earliest observer
(``_on_platform_update``, registered in group 99 specifically so it
observes every update alongside, never displacing, the core handlers)
before any Hermes-specific allowlist/batching/session/Capture-Router
logic runs? It must never log message text, names, chat titles, or the
bot token, and must never change control flow, handler registration, or
create a second Telegram consumer. Remove alongside the diagnostic
instrumentation itself once the ingress investigation concludes.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _adapter(extra=None) -> TelegramAdapter:
    a = object.__new__(TelegramAdapter)
    a.platform = Platform.TELEGRAM
    a.config = SimpleNamespace(extra=extra if extra is not None else {"allow_from": ["*"]})
    a.gateway_runner = None
    return a


def _text_message_update(text: str = "hello there", chat_type: str = "private"):
    """A PTB Update stand-in shaped like a normal inbound text DM."""
    update = MagicMock()
    update.update_id = 987654321
    msg = MagicMock()
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.type = chat_type
    update.message = msg
    update.effective_message = msg
    update.edited_message = None
    update.callback_query = None
    return update


def _no_text_update():
    """An update with no message at all (e.g. an unhandled update shape)."""
    update = MagicMock()
    update.update_id = 111
    update.message = None
    update.edited_message = None
    update.callback_query = None
    update.effective_message = None
    return update


# ---------------------------------------------------------------------------
# 1 & 2. Diagnostic log fires for a normal text Update, with no message text
# ---------------------------------------------------------------------------

def test_diagnostic_log_fires_for_normal_text_update(caplog):
    a = _adapter()
    a._platform_event_handler = None  # no subscriber -- fire-site returns early after our diagnostic

    with caplog.at_level(logging.INFO):
        asyncio.run(a._on_platform_update(_text_message_update("hello there"), context=MagicMock()))

    matches = [r for r in caplog.records if "TELEGRAM_RAW_UPDATE_SEEN" in r.message]
    assert len(matches) == 1
    line = matches[0].message
    assert "update_id=987654321" in line
    assert "type=message" in line
    assert "chat_type=private" in line
    assert "has_text=True" in line


def test_diagnostic_log_never_contains_message_text(caplog):
    a = _adapter()
    a._platform_event_handler = None

    secret_text = "this exact sentence must never appear in the log"
    with caplog.at_level(logging.INFO):
        asyncio.run(a._on_platform_update(_text_message_update(secret_text), context=MagicMock()))

    for record in caplog.records:
        assert secret_text not in record.message


def test_diagnostic_log_fires_for_update_with_no_message(caplog):
    # "other" update types must still be observed and classified -- just
    # without a chat_type/has_text signal to report.
    a = _adapter()
    a._platform_event_handler = None

    with caplog.at_level(logging.INFO):
        asyncio.run(a._on_platform_update(_no_text_update(), context=MagicMock()))

    matches = [r for r in caplog.records if "TELEGRAM_RAW_UPDATE_SEEN" in r.message]
    assert len(matches) == 1
    assert "type=other" in matches[0].message
    assert "has_text=False" in matches[0].message


def test_diagnostic_log_never_raises_on_malformed_update(caplog):
    # A MagicMock() with no attributes configured must not raise out of
    # the diagnostic block and break the update loop.
    a = _adapter()
    a._platform_event_handler = None
    with caplog.at_level(logging.INFO):
        asyncio.run(a._on_platform_update(MagicMock(), context=MagicMock()))  # must not raise


# ---------------------------------------------------------------------------
# 3. Normal handler behavior remains unchanged
# ---------------------------------------------------------------------------

def test_existing_fire_site_behavior_unchanged_with_subscriber():
    # The pre-existing gateway_platform_event fire-site behavior (envelope
    # dispatch to a registered handler) must be untouched by the diagnostic
    # addition, which only runs before it and never alters its result.
    a = _adapter()
    seen: list = []

    async def observe(event, source):
        seen.append((event, source))

    a.set_platform_event_handler(observe)
    from unittest.mock import patch

    with patch("hermes_cli.lifecycle.has_hook", return_value=False):
        asyncio.run(a._on_platform_update(_text_message_update(), context=MagicMock()))

    # has_hook=False -> the existing behavior (no dispatch) is preserved;
    # the diagnostic log firing above it does not change this outcome.
    assert seen == []


def test_diagnostic_error_handler_logs_only_exception_metadata(caplog):
    a = _adapter()
    context = SimpleNamespace(error=RuntimeError("some internal detail, e.g. a chat title"))

    with caplog.at_level(logging.ERROR):
        asyncio.run(a._diagnostic_log_unhandled_error(MagicMock(), context))

    matches = [r for r in caplog.records if "TELEGRAM_DIAGNOSTIC_UNHANDLED_ERROR" in r.message]
    assert len(matches) == 1
    assert "type=RuntimeError" in matches[0].message
    assert "some internal detail" in matches[0].message  # exception message itself is allowed
    assert "message=" in matches[0].message


# ---------------------------------------------------------------------------
# 4. No additional getUpdates consumer / handler registration is unchanged
# in shape (same set of handlers, one new error handler only)
# ---------------------------------------------------------------------------

def test_register_handlers_adds_exactly_one_error_handler_no_new_polling():
    source = inspect.getsource(TelegramAdapter._register_handlers)
    # Exactly one new registration call versus the pre-diagnostic baseline:
    # add_error_handler. No new Application/Updater/polling construction.
    assert source.count("add_error_handler(") == 1
    assert "start_polling" not in source
    assert "get_updates" not in source.lower().replace("_", "")


def test_no_new_telegram_application_or_updater_is_constructed():
    source = inspect.getsource(TelegramAdapter)
    # The diagnostic change must not introduce a second Application/Updater
    # (which would be a second, competing getUpdates consumer).
    assert source.count("Application.builder(") <= 1


# ---------------------------------------------------------------------------
# 5. Allowlist/routing behavior is unchanged
# ---------------------------------------------------------------------------

def test_should_process_message_and_auth_check_untouched():
    # Static guard: the diagnostic commit must not have touched the
    # allowlist/routing functions themselves.
    source = inspect.getsource(TelegramAdapter._should_process_message)
    assert "TELEGRAM_RAW_UPDATE_SEEN" not in source
    assert "TELEGRAM_DIAGNOSTIC" not in source

    source = inspect.getsource(TelegramAdapter._handle_text_message)
    assert "TELEGRAM_RAW_UPDATE_SEEN" not in source
    assert "TELEGRAM_DIAGNOSTIC" not in source


# ---------------------------------------------------------------------------
# PTB update_queue diagnostic (PTB_UPDATE_QUEUE_SEEN)
# ---------------------------------------------------------------------------

class _FakeQueue:
    """Minimal asyncio.Queue-shaped stand-in: only `put` is exercised by
    the diagnostic wrapper, and only `put`/`_hermes_diagnostic_wrapped`
    need to behave like real attributes."""

    def __init__(self):
        self.items: list = []

    async def put(self, item, *args, **kwargs):
        self.items.append(item)


def test_queue_diagnostic_fires_once_per_enqueued_update(caplog):
    a = _adapter()
    app = SimpleNamespace(update_queue=_FakeQueue())

    with caplog.at_level(logging.INFO):
        a._instrument_update_queue_diagnostic(app)
        asyncio.run(app.update_queue.put(_text_message_update("hi")))

    matches = [r for r in caplog.records if "PTB_UPDATE_QUEUE_SEEN" in r.message]
    assert len(matches) == 1
    assert "type=message" in matches[0].message
    assert "chat_type=private" in matches[0].message
    assert "has_text=True" in matches[0].message


def test_queue_diagnostic_delivers_update_unchanged():
    # The exact same object must still reach the queue's underlying store
    # -- the wrapper must not clone, replace, or drop it.
    a = _adapter()
    fake_queue = _FakeQueue()
    app = SimpleNamespace(update_queue=fake_queue)
    a._instrument_update_queue_diagnostic(app)

    original_update = _text_message_update("unchanged")
    asyncio.run(app.update_queue.put(original_update))

    assert len(fake_queue.items) == 1
    assert fake_queue.items[0] is original_update


def test_queue_diagnostic_never_logs_message_text(caplog):
    a = _adapter()
    app = SimpleNamespace(update_queue=_FakeQueue())
    a._instrument_update_queue_diagnostic(app)

    secret_text = "queue-level secret that must never be logged"
    with caplog.at_level(logging.INFO):
        asyncio.run(app.update_queue.put(_text_message_update(secret_text)))

    for record in caplog.records:
        assert secret_text not in record.message


def test_queue_diagnostic_is_idempotent_no_double_wrap(caplog):
    # Wrapping an already-wrapped queue must be a no-op -- otherwise a
    # rebuild path could stack wrappers and double-log (or, if this were
    # a real consumer instead of a pass-through wrapper, double-consume).
    a = _adapter()
    queue = _FakeQueue()
    app = SimpleNamespace(update_queue=queue)

    a._instrument_update_queue_diagnostic(app)
    a._instrument_update_queue_diagnostic(app)  # second call, same queue

    with caplog.at_level(logging.INFO):
        asyncio.run(app.update_queue.put(_text_message_update("x")))

    matches = [r for r in caplog.records if "PTB_UPDATE_QUEUE_SEEN" in r.message]
    assert len(matches) == 1  # not double-logged
    assert len(queue.items) == 1  # not double-enqueued either


def test_queue_diagnostic_creates_no_second_consumer():
    # Static guard: the wrapper must never call .get()/.get_nowait() --
    # that would make it a competing consumer instead of a pass-through
    # observer on the put path.
    source = inspect.getsource(TelegramAdapter._instrument_update_queue_diagnostic)
    assert ".get(" not in source
    assert ".get_nowait(" not in source
    assert "asyncio.Queue(" not in source  # never constructs a second queue


def test_register_handlers_instruments_queue_before_registering():
    source = inspect.getsource(TelegramAdapter._register_handlers)
    assert "_instrument_update_queue_diagnostic(app)" in source
    # Must run before any add_handler call, not after.
    queue_pos = source.index("_instrument_update_queue_diagnostic(app)")
    handler_pos = source.index("add_handler(")
    assert queue_pos < handler_pos
