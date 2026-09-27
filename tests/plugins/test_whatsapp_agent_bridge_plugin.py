"""Tests for the whatsapp-agent-bridge plugin (plugins/whatsapp_agent_bridge/).

Bridges this Hermes instance to WhatsApp Agent's own Phase 5 relay contract
(a separate repo -- D:\\AI Agent\\Whatsapp AI, docs/whatsapp-agent/PHASE_5.md
has the full wire contract this plugin implements against). No real network
call, no tunnel, no WhatsApp message anywhere in this file.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "whatsapp_agent_bridge"
REPO_ROOT = Path(__file__).resolve().parents[2]

_VALID_SECRET = "s" * 32
_VALID_URL = "https://example.trycloudflare.com"
_VALID_USER_ID = "111222333"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for key in (
        "WHATSAPP_AGENT_BRIDGE_ENABLED", "WHATSAPP_AGENT_SHARED_SECRET", "WHATSAPP_AGENT_CALLBACK_URL",
        "WHATSAPP_AGENT_AUTHORIZED_USER_ID", "WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID",
        "WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET", "WHATSAPP_AGENT_BRIDGE_HOST", "WHATSAPP_AGENT_BRIDGE_PORT",
        "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_USER_ID", "HERMES_GATEWAY_PROCESS",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def _configure_valid(monkeypatch, **overrides):
    values = {
        "WHATSAPP_AGENT_BRIDGE_ENABLED": "true",
        "WHATSAPP_AGENT_SHARED_SECRET": _VALID_SECRET,
        "WHATSAPP_AGENT_CALLBACK_URL": _VALID_URL,
        "WHATSAPP_AGENT_AUTHORIZED_USER_ID": _VALID_USER_ID,
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _load_module(name: str):
    """Loads one module from plugins/whatsapp_agent_bridge/ directly by file
    path, registering it under the plugin's real relative-import package
    name so `from . import X` inside it resolves -- same pattern
    tests/plugins/test_tax_content_bridge_plugin.py uses for its own plugin."""
    package_name = "hermes_plugins.whatsapp_agent_bridge"
    if package_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            package_name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)],
        )
        pkg = importlib.util.module_from_spec(pkg_spec)
        pkg.__path__ = [str(PLUGIN_DIR)]
        sys.modules[package_name] = pkg
        if "hermes_plugins" not in sys.modules:
            ns = types.ModuleType("hermes_plugins")
            ns.__path__ = []
            sys.modules["hermes_plugins"] = ns
        pkg_spec.loader.exec_module(pkg)

    full_name = f"{package_name}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = package_name
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _reset_modules():
    for name in list(sys.modules):
        if name.startswith("hermes_plugins.whatsapp_agent_bridge") or name == "hermes_plugins":
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _fresh_modules():
    _reset_modules()
    yield
    _reset_modules()


def _draft_reply_card() -> dict:
    return {
        "card_type": "WHATSAPP_REMINDER",
        "title": "WhatsApp reply waiting",
        "fields": [
            {"label": "From", "value": "Nino"},
            {"label": "Preview", "value": "Can we move tomorrow's call?"},
            {"label": "Waiting", "value": "42 minutes"},
        ],
        "actions": [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-draft-abc"},
            {"action_id": "REMIND_LATER", "label": "Remind later", "action_token": "tok-remind-abc"},
            {"action_id": "DISMISS", "label": "Dismiss", "action_token": "tok-dismiss-abc"},
        ],
    }


class _FakeTelegramResponse:
    def __init__(self, ok=True):
        self._ok = ok

    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": self._ok}


def _mock_telegram(monkeypatch, ing, *, ok=True):
    """Patches ing._notify's Telegram Bot API call and returns the list of
    captured `requests.post` kwargs (one dict per call)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    captured = []

    def fake_post(url, json, timeout):
        captured.append({"url": url, "json": json, "timeout": timeout})
        return _FakeTelegramResponse(ok=ok)

    monkeypatch.setattr(ing.requests, "post", fake_post)
    return captured


def _push_event(mod_ingress, *, event_id="evt-1", card=None):
    import json as _json

    body = _json.dumps({"event_id": event_id, "event_type": "WHATSAPP_REMINDER_DUE", "card": card or _draft_reply_card()}).encode()
    return mod_ingress.handle_event(authorization_header=f"Bearer {_VALID_SECRET}", body=body)


# ---------------------------------------------------------------------------
# config.py -- fail-closed behaviour (checks 27, 28, 22)
# ---------------------------------------------------------------------------

class TestConfig:
    def test_disabled_by_default(self):
        cfg = _load_module("config")
        assert cfg.is_configured() is False

    def test_enabled_but_missing_secret_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_SHARED_SECRET=None)
        assert cfg.is_configured() is False

    def test_short_secret_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_SHARED_SECRET="short")
        assert cfg.is_configured() is False

    def test_non_https_non_local_url_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_CALLBACK_URL="http://example.com")
        assert cfg.is_configured() is False

    def test_url_with_path_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_CALLBACK_URL="https://example.com/hermes/actions")
        assert cfg.is_configured() is False

    def test_local_http_is_true(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_CALLBACK_URL="http://127.0.0.1:8767")
        assert cfg.is_configured() is True

    def test_non_numeric_user_id_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_AUTHORIZED_USER_ID="not_numeric")
        assert cfg.is_configured() is False

    def test_cf_access_one_sided_is_false(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch, WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID="id-only")
        assert cfg.is_configured() is False

    def test_cf_access_both_set_is_true(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(
            monkeypatch,
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID="id-value",
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET="secret-value",
        )
        assert cfg.is_configured() is True

    def test_fully_valid_is_true(self, monkeypatch):
        cfg = _load_module("config")
        _configure_valid(monkeypatch)
        assert cfg.is_configured() is True

    def test_load_raises_when_disabled(self):
        cfg = _load_module("config")
        with pytest.raises(cfg.ConfigError):
            cfg.load()


# ---------------------------------------------------------------------------
# ingress.py -- Bearer auth, idempotency, notification (checks 1-9, 21)
# ---------------------------------------------------------------------------

class TestIngressAuth:
    def test_valid_bearer_accepted(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        sent = _mock_telegram(monkeypatch, ing)
        status, body = _push_event(ing)
        assert status == 200
        assert body == {"acknowledged": True, "event_id": "evt-1"}
        assert len(sent) == 1

    def test_missing_bearer_rejected(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        import json as _json
        body = _json.dumps({"event_id": "e1", "event_type": "WHATSAPP_REMINDER_DUE", "card": _draft_reply_card()}).encode()
        status, resp = ing.handle_event(authorization_header=None, body=body)
        assert status == 401
        assert resp == {"error": "UNAUTHENTICATED"}

    def test_invalid_bearer_rejected(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        import json as _json
        body = _json.dumps({"event_id": "e1", "event_type": "WHATSAPP_REMINDER_DUE", "card": _draft_reply_card()}).encode()
        status, resp = ing.handle_event(authorization_header="Bearer wrong-secret-value-thats-still-32-chars", body=body)
        assert status == 401
        assert resp == {"error": "UNAUTHENTICATED"}

    def test_uses_constant_time_comparison(self, monkeypatch):
        """Asserts the actual comparison call is hmac.compare_digest, not `==`."""
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        calls = []
        real_compare = ing.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real_compare(a, b)

        monkeypatch.setattr(ing.hmac, "compare_digest", spy)
        _mock_telegram(monkeypatch, ing)
        _push_event(ing, event_id="evt-timing")
        assert len(calls) == 1

    def test_bridge_not_configured_returns_503(self):
        ing = _load_module("ingress")
        status, resp = ing.handle_event(authorization_header=f"Bearer {_VALID_SECRET}", body=b"{}")
        assert status == 503
        assert resp == {"error": "BRIDGE_NOT_CONFIGURED"}

    def test_malformed_body_rejected(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        status, resp = ing.handle_event(authorization_header=f"Bearer {_VALID_SECRET}", body=b"not json")
        assert status == 400
        assert resp["error"] == "MALFORMED"

    def test_unknown_event_type_rejected(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        import json as _json
        body = _json.dumps({"event_id": "e1", "event_type": "SOMETHING_ELSE", "card": {}}).encode()
        status, resp = ing.handle_event(authorization_header=f"Bearer {_VALID_SECRET}", body=body)
        assert status == 400
        assert resp["error"] == "INVALID_EVENT_TYPE"

    def test_notification_sent_with_no_parse_mode(self, monkeypatch):
        """Regression for the reviewed High finding: the shared
        send_message_tool/Telegram-adapter path auto-detects and renders
        Markdown/HTML, which would let an inbound WhatsApp contact disguise
        a link as trusted text. _notify must call the Bot API directly with
        no parse_mode at all, so Telegram never interprets formatting."""
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        captured = _mock_telegram(monkeypatch, ing)
        _push_event(ing, event_id="evt-plain")
        assert len(captured) == 1
        assert "api.telegram.org" in captured[0]["url"]
        assert "parse_mode" not in captured[0]["json"]

    def test_markdown_lookalike_text_reaches_telegram_byte_identical(self, monkeypatch):
        """A contact's WhatsApp message that happens to look like Markdown
        (e.g. a disguised link) must reach Telegram's API completely
        unmodified -- proving no escaping/transformation happens, and that
        the literal text is what will be displayed, not a rendered link."""
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        captured = _mock_telegram(monkeypatch, ing)
        malicious_preview = "[click here for the invoice](http://evil.example/phish)"
        card = {
            "card_type": "WHATSAPP_REMINDER",
            "title": "WhatsApp reply waiting",
            "fields": [{"label": "Preview", "value": malicious_preview}],
            "actions": [{"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-x"}],
        }
        _push_event(ing, event_id="evt-phish", card=card)
        assert malicious_preview in captured[0]["json"]["text"]

    def test_no_actor_field_on_inbound_push_by_design(self):
        """WhatsApp Agent's own wire contract for the push leg is exactly
        {event_id, event_type, card} -- no actor/user field exists to check
        (confirmed: src/whatsapp-agent/events/hermes-event-sink.ts only ever
        sends these three keys). The Bearer secret is therefore the only
        inbound identity check this leg can perform; WHATSAPP_AGENT_
        AUTHORIZED_USER_ID is used only as the Telegram delivery target."""
        ing = _load_module("ingress")
        import inspect
        source = inspect.getsource(ing.handle_event)
        assert "hermes_user_id" not in source
        assert "actor" not in source.lower()


class TestIngressIdempotency:
    def test_duplicate_event_does_not_notify_twice(self, monkeypatch):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        sent = _mock_telegram(monkeypatch, ing)
        status1, body1 = _push_event(ing, event_id="evt-dup")
        status2, body2 = _push_event(ing, event_id="evt-dup")
        assert status1 == status2 == 200
        assert body1 == body2 == {"acknowledged": True, "event_id": "evt-dup"}
        assert len(sent) == 1, "duplicate push must not notify a second time"

    def test_token_stored_opaquely_and_never_logged(self, monkeypatch, caplog):
        _configure_valid(monkeypatch)
        ing = _load_module("ingress")
        store = _load_module("store")
        _mock_telegram(monkeypatch, ing)
        with caplog.at_level("DEBUG"):
            _push_event(ing, event_id="evt-token")
        token = store.find_token("evt-token", "DRAFT_REPLY")
        assert token is not None
        assert token["action_token"] == "tok-draft-abc"  # stored verbatim, never decoded/altered
        assert "tok-draft-abc" not in caplog.text, "action tokens must never appear in log output"

    def test_claim_and_store_is_atomic_across_a_mid_transaction_failure(self, monkeypatch):
        """Regression for the reviewed gap: if the store write fails partway
        through, the event must NOT be left permanently "claimed but never
        stored" -- a retried push must be able to reprocess it from scratch
        (Medium finding #2)."""
        store = _load_module("store")
        cards_mod = _load_module("cards")
        card = _draft_reply_card()
        actions = cards_mod.extract_actions(card)

        real_upsert = store._upsert_thread_and_tokens

        def boom(*a, **kw):
            raise RuntimeError("simulated mid-transaction failure")

        monkeypatch.setattr(store, "_upsert_thread_and_tokens", boom)
        with pytest.raises(RuntimeError):
            store.claim_and_store_event("evt-crash", "evt-crash", card.get("card_type", ""), card, actions)

        monkeypatch.setattr(store, "_upsert_thread_and_tokens", real_upsert)
        # The failed attempt must not have left the event claimed -- a retry
        # (e.g. the Agent's own OutboxDispatcher redelivering) can succeed.
        assert store.claim_and_store_event("evt-crash", "evt-crash", card.get("card_type", ""), card, actions) is True
        assert store.find_token("evt-crash", "DRAFT_REPLY") is not None


# ---------------------------------------------------------------------------
# store.py -- opaque custody, supersession, disambiguation (checks 7, 9, 10)
# ---------------------------------------------------------------------------

class TestStore:
    def test_claim_and_store_first_true_second_false(self):
        store = _load_module("store")
        card = _draft_reply_card()
        actions = [{"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-a"}]
        assert store.claim_and_store_event("e-1", "e-1", card.get("card_type", ""), card, actions) is True
        assert store.claim_and_store_event("e-1", "e-1", card.get("card_type", ""), card, actions) is False

    def test_terminal_card_type_excluded_from_active_threads(self):
        store = _load_module("store")
        store.store_thread("t-done", "WHATSAPP_SEND_RESULT", {"card_type": "WHATSAPP_SEND_RESULT"}, [])
        assert store.list_active_threads() == []

    def test_notice_card_type_excluded_from_active_threads(self):
        store = _load_module("store")
        store.store_thread("t-notice", "WHATSAPP_NOTICE", {"card_type": "WHATSAPP_NOTICE"}, [])
        assert store.list_active_threads() == []

    def test_find_token_missing_action_fails_closed(self):
        store = _load_module("store")
        store.store_thread("t1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-a"},
        ])
        assert store.find_token("t1", "SEND_NOW") is None

    def test_new_card_supersedes_old_tokens(self):
        store = _load_module("store")
        store.store_thread("t1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-old"},
        ])
        store.store_thread("t1", "WHATSAPP_DRAFT", {"card_type": "WHATSAPP_DRAFT"}, [
            {"action_id": "APPROVE_DRAFT", "label": "Approve", "action_token": "tok-new"},
        ])
        assert store.find_token("t1", "DRAFT_REPLY") is None, "superseded token must no longer be findable"
        assert store.find_token("t1", "APPROVE_DRAFT")["action_token"] == "tok-new"

    def test_ambiguous_action_id_returns_none_without_hint(self):
        """RESOLVE_UNKNOWN_SEND can legitimately appear twice on one card
        (Mark as sent / Mark as not sent each mint their own token under the
        same action_id) -- find_token must refuse to guess."""
        store = _load_module("store")
        store.store_thread("t1", "WHATSAPP_SEND_UNCLEAR", {}, [
            {"action_id": "RESOLVE_UNKNOWN_SEND", "label": "Mark as sent", "action_token": "tok-sent"},
            {"action_id": "RESOLVE_UNKNOWN_SEND", "label": "Mark as not sent", "action_token": "tok-not-sent"},
        ])
        assert store.find_token("t1", "RESOLVE_UNKNOWN_SEND") is None
        assert len(store.find_tokens("t1", "RESOLVE_UNKNOWN_SEND")) == 2


# ---------------------------------------------------------------------------
# render.py -- exact plain text (checks 16-20)
# ---------------------------------------------------------------------------

class TestRender:
    def test_georgian_text_preserved_exactly(self):
        render = _load_module("render")
        card = {"title": "T", "fields": [{"label": "შენიშვნა", "value": "გამარჯობა, როგორ ხარ?"}], "actions": []}
        assert "გამარჯობა, როგორ ხარ?" in render.render_card(card)

    def test_russian_text_preserved_exactly(self):
        render = _load_module("render")
        card = {"title": "T", "fields": [{"label": "Заметка", "value": "Привет, как дела?"}], "actions": []}
        assert "Привет, как дела?" in render.render_card(card)

    def test_english_text_preserved_exactly(self):
        render = _load_module("render")
        card = {"title": "T", "fields": [{"label": "Note", "value": "Hello, how are you?"}], "actions": []}
        assert "Hello, how are you?" in render.render_card(card)

    def test_marker_preserved_exactly(self):
        render = _load_module("render")
        text_with_marker = "Right-to-left override marker: [U+202E] revealed here"
        card = {"title": "T", "fields": [{"label": "Draft", "value": text_with_marker}], "actions": []}
        assert text_with_marker in render.render_card(card)

    def test_no_markdown_or_html_transformation(self):
        render = _load_module("render")
        raw = "*not bold* <b>not html</b> _not italic_"
        card = {"title": "T", "fields": [{"label": "Draft", "value": raw}], "actions": []}
        assert raw in render.render_card(card)

    def test_error_falls_back_to_code_when_no_card(self):
        render = _load_module("render")
        assert "UNKNOWN_TOKEN" in render.render_error("UNKNOWN_TOKEN", None)


# ---------------------------------------------------------------------------
# callback_client.py -- exact body, SSRF/redirect hardening, CF headers
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def json(self):
        return self._json_body


class TestCallbackClient:
    def test_exact_four_field_body_and_fresh_request_id(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        client = _load_module("callback_client")
        captured = []

        def fake_post(url, json, headers, timeout, allow_redirects):
            captured.append({"url": url, "json": json, "headers": headers, "allow_redirects": allow_redirects})
            return _FakeResponse(200, {"status": "APPLIED", "action_type": "DRAFT_REPLY", "card": {"card_type": "WHATSAPP_DRAFT"}})

        monkeypatch.setattr(client.requests, "post", fake_post)
        result1 = client.send_action("DRAFT_REPLY", "tok-a")
        result2 = client.send_action("DRAFT_REPLY", "tok-a")

        assert captured[0]["url"] == f"{_VALID_URL}/hermes/actions"
        assert set(captured[0]["json"].keys()) == {"request_id", "action_type", "hermes_user_id", "action_token"}
        assert captured[0]["json"]["hermes_user_id"] == "999888777"
        assert captured[0]["allow_redirects"] is False
        assert captured[0]["json"]["request_id"] != captured[1]["json"]["request_id"], "each call must mint a fresh request_id"
        assert result1["_status"] == 200

    def test_missing_session_identity_fails_closed(self, monkeypatch):
        _configure_valid(monkeypatch)
        client = _load_module("callback_client")
        with pytest.raises(client.CallbackError):
            client.send_action("DRAFT_REPLY", "tok-a")

    def test_redirect_rejected(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        client = _load_module("callback_client")
        monkeypatch.setattr(client.requests, "post", lambda *a, **kw: _FakeResponse(302, {}))
        with pytest.raises(client.CallbackError):
            client.send_action("DRAFT_REPLY", "tok-a")

    def test_cf_access_headers_added_only_when_both_configured(self, monkeypatch):
        _configure_valid(
            monkeypatch,
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID="cf-id-value",
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET="cf-secret-value",
        )
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        client = _load_module("callback_client")
        captured = {}

        def fake_post(url, json, headers, timeout, allow_redirects):
            captured.update(headers)
            return _FakeResponse(200, {"status": "APPLIED", "action_type": "DRAFT_REPLY", "card": {}})

        monkeypatch.setattr(client.requests, "post", fake_post)
        client.send_action("DRAFT_REPLY", "tok-a")
        assert captured["CF-Access-Client-Id"] == "cf-id-value"
        assert captured["CF-Access-Client-Secret"] == "cf-secret-value"

    def test_cf_access_headers_absent_when_not_configured(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        client = _load_module("callback_client")
        captured = {}

        def fake_post(url, json, headers, timeout, allow_redirects):
            captured.update(headers)
            return _FakeResponse(200, {"status": "APPLIED", "action_type": "DRAFT_REPLY", "card": {}})

        monkeypatch.setattr(client.requests, "post", fake_post)
        client.send_action("DRAFT_REPLY", "tok-a")
        assert "CF-Access-Client-Id" not in captured
        assert "CF-Access-Client-Secret" not in captured

    def test_cf_access_secrets_never_logged(self, monkeypatch, caplog):
        _configure_valid(
            monkeypatch,
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID="cf-id-value",
            WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET="cf-secret-value",
        )
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        client = _load_module("callback_client")
        monkeypatch.setattr(
            client.requests, "post",
            lambda *a, **kw: _FakeResponse(200, {"status": "APPLIED", "action_type": "DRAFT_REPLY", "card": {}}),
        )
        with caplog.at_level("DEBUG"):
            client.send_action("DRAFT_REPLY", "tok-a")
        assert "cf-secret-value" not in caplog.text


# ---------------------------------------------------------------------------
# tools.py -- end-to-end simulation against fake Agent responses
# ---------------------------------------------------------------------------

class TestToolsEndToEnd:
    def _setup(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "999888777")
        store = _load_module("store")
        client = _load_module("callback_client")
        tools = _load_module("tools")
        return store, client, tools

    def test_get_current_with_nothing_pending(self, monkeypatch):
        _store, _client, tools = self._setup(monkeypatch)
        result = tools._handle_get_current({})
        assert "error" in result

    def test_ambiguous_when_multiple_threads_pending(self, monkeypatch):
        """Regression for the reviewed Medium finding: with more than one
        active thread, tools must fail closed and ask the user to
        disambiguate rather than silently act on whichever was created
        most recently -- acting on the wrong contact's thread is a real
        risk even though the Agent's own token binding still prevents an
        actual cross-conversation send."""
        store, _client, tools = self._setup(monkeypatch)
        store.store_thread("evt-1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-a"},
        ])
        store.store_thread("evt-2", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-b"},
        ])
        result = tools._handle_get_current({})
        assert "error" in result
        assert "more than one" in result.lower()

    def test_terminal_thread_does_not_block_a_later_new_reminder(self, monkeypatch):
        """A completed thread (message already sent) must not count toward
        "currently pending" -- otherwise every tool would become
        permanently ambiguous after the very first reminder is resolved."""
        store, _client, tools = self._setup(monkeypatch)
        store.store_thread("evt-old", "WHATSAPP_SEND_RESULT", {"card_type": "WHATSAPP_SEND_RESULT"}, [])
        store.store_thread("evt-new", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-new"},
        ])
        result = tools._handle_get_current({})
        assert "error" not in result

    def test_send_now_unavailable_before_approve(self, monkeypatch):
        store, _client, tools = self._setup(monkeypatch)
        store.store_thread("evt-1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-draft"},
        ])
        result = tools._handle_send_now({})
        assert "error" in result
        assert "SEND_NOW" in result

    def test_full_flow_draft_approve_send(self, monkeypatch):
        store, client, tools = self._setup(monkeypatch)
        store.store_thread("evt-1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-draft"},
        ])

        responses = iter([
            {"_status": 200, "status": "APPLIED", "action_type": "DRAFT_REPLY", "card": {
                "card_type": "WHATSAPP_DRAFT", "title": "Draft ready", "fields": [{"label": "Draft", "value": "Sure, works for me"}],
                "actions": [{"action_id": "APPROVE_DRAFT", "label": "Approve", "action_token": "tok-approve"}],
            }},
            {"_status": 200, "status": "APPLIED", "action_type": "APPROVE_DRAFT", "card": {
                "card_type": "WHATSAPP_SEND_CONFIRMATION", "title": "Ready to send", "fields": [{"label": "Text", "value": "Sure, works for me"}],
                "actions": [{"action_id": "SEND_NOW", "label": "Send now", "action_token": "tok-send"}],
            }},
            {"_status": 200, "status": "APPLIED", "action_type": "SEND_NOW", "card": {
                "card_type": "WHATSAPP_SEND_RESULT", "title": "Message sent", "fields": [], "actions": [],
            }},
        ])
        monkeypatch.setattr(client, "send_action", lambda action_type, action_token: next(responses))

        draft_result = tools._handle_draft_reply({})
        assert "Draft ready" in draft_result
        assert store.find_token("evt-1", "SEND_NOW") is None, "no send token exists before approve"

        approve_result = tools._handle_approve_draft({})
        assert "Ready to send" in approve_result
        assert store.find_token("evt-1", "SEND_NOW")["action_token"] == "tok-send"

        send_result = tools._handle_send_now({})
        assert "Message sent" in send_result

    def test_resolve_unknown_send_picks_correct_label(self, monkeypatch):
        store, client, tools = self._setup(monkeypatch)
        store.store_thread("evt-1", "WHATSAPP_SEND_UNCLEAR", {}, [
            {"action_id": "RESOLVE_UNKNOWN_SEND", "label": "Mark as sent", "action_token": "tok-sent"},
            {"action_id": "RESOLVE_UNKNOWN_SEND", "label": "Mark as not sent", "action_token": "tok-not-sent"},
        ])
        used = {}

        def fake_send_action(action_type, action_token):
            used["token"] = action_token
            return {"_status": 200, "status": "APPLIED", "action_type": action_type, "card": {"card_type": "WHATSAPP_NOTICE"}}

        monkeypatch.setattr(client, "send_action", fake_send_action)
        tools._handle_resolve_unknown_send({"resolution": "not_sent"})
        assert used["token"] == "tok-not-sent"

    def test_returned_agent_error_card_is_shown(self, monkeypatch):
        store, client, tools = self._setup(monkeypatch)
        store.store_thread("evt-1", "WHATSAPP_REMINDER", _draft_reply_card(), [
            {"action_id": "DRAFT_REPLY", "label": "Draft reply", "action_token": "tok-draft"},
        ])
        monkeypatch.setattr(
            client, "send_action",
            lambda action_type, action_token: {
                "_status": 409, "error": "TOKEN_EXPIRED",
                "card": {"title": "Action expired", "fields": [], "actions": []},
            },
        )
        result = tools._handle_draft_reply({})
        assert "Action expired" in result


# ---------------------------------------------------------------------------
# Static / structural checks (checks 29, 30, 31, 32)
# ---------------------------------------------------------------------------

class TestStructural:
    def test_no_whatsapp_client_dependency(self):
        """No Python import of a WhatsApp client library anywhere in this
        plugin (checked at the import-statement level, not prose -- module
        docstrings legitimately *mention* LocalAuth/whatsapp-web.js while
        explaining what this plugin deliberately does not touch)."""
        forbidden_imports = ("whatsapp-web.js", "puppeteer")
        for path in PLUGIN_DIR.glob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    for term in forbidden_imports:
                        assert term not in stripped, f"{path.name} must not import {term!r}"

    def test_no_sendmessage_call_in_hermes(self):
        # "send_message_tool" (Hermes's own generic cross-platform Telegram
        # helper, already used by tax_content_bridge) is fine; a WhatsApp
        # client `sendMessage` call is not, and none exists.
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert ".sendMessage(" not in text

    def test_no_poller_thread_created(self):
        """Unlike tax_content_bridge, this plugin's background thread is an
        HTTP *server* (serve_forever), never a polling loop with a sleep
        interval."""
        ingress_source = (PLUGIN_DIR / "ingress.py").read_text(encoding="utf-8")
        assert "serve_forever" in ingress_source
        assert "time.sleep" not in ingress_source
        assert "requests.get" not in ingress_source  # no GET polling anywhere

    def test_native_whatsapp_integration_untouched(self):
        result = subprocess.run(
            ["git", "diff", "--name-only", "main"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        changed = set(result.stdout.strip().splitlines())
        forbidden_paths = {p for p in changed if "whatsapp_cloud" in p or "whatsapp_common" in p}
        assert not forbidden_paths, f"native WhatsApp integration files must stay untouched: {forbidden_paths}"


# ---------------------------------------------------------------------------
# Real PluginManager discovery -- kind: backend must auto-load, no opt-in
# ---------------------------------------------------------------------------

class TestBundledDiscovery:
    def test_auto_loads_with_no_config_and_registers_all_tools(self, monkeypatch):
        from hermes_cli import plugins as pmod
        from tools.registry import registry

        mgr = pmod.PluginManager()
        mgr.discover_and_load()

        assert "whatsapp-agent-bridge" in mgr._plugins
        loaded = mgr._plugins["whatsapp-agent-bridge"]
        assert loaded.manifest.source == "bundled"
        assert loaded.manifest.kind == "backend"
        assert loaded.enabled is True, loaded.error

        expected_tools = {
            "whatsapp_agent_list_pending", "whatsapp_agent_get_current", "whatsapp_agent_draft_reply",
            "whatsapp_agent_regenerate_draft", "whatsapp_agent_approve_draft", "whatsapp_agent_send_now",
            "whatsapp_agent_cancel_send", "whatsapp_agent_remind_later", "whatsapp_agent_dismiss",
            "whatsapp_agent_resolve_unknown_send",
        }
        registered = {name for name in expected_tools if registry.get_entry(name) is not None}
        assert registered == expected_tools

    def test_ingress_not_started_without_gateway_process_flag(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.delenv("HERMES_GATEWAY_PROCESS", raising=False)
        _load_module("ingress")  # also bootstraps the package (__init__.py)
        pkg = sys.modules["hermes_plugins.whatsapp_agent_bridge"]
        started = []
        monkeypatch.setattr(pkg, "start_ingress_server", lambda: started.append(True))

        class _FakeCtx:
            def register_tool(self, **kwargs):
                pass

        pkg.register(_FakeCtx())
        assert started == [], "ingress must not start without HERMES_GATEWAY_PROCESS=1"

    def test_ingress_starts_when_gateway_process_flag_set(self, monkeypatch):
        _configure_valid(monkeypatch)
        monkeypatch.setenv("HERMES_GATEWAY_PROCESS", "1")
        _load_module("ingress")
        pkg = sys.modules["hermes_plugins.whatsapp_agent_bridge"]
        started = []
        monkeypatch.setattr(pkg, "start_ingress_server", lambda: started.append(True))

        class _FakeCtx:
            def register_tool(self, **kwargs):
                pass

        pkg.register(_FakeCtx())
        assert started == [True]
