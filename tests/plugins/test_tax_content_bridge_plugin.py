"""Tests for the tax-content-bridge plugin (plugins/tax_content_bridge/).

Bridges this Hermes instance to the Tax Content Intelligence Agent's own
action_gateway (a separate repo -- see that repo's
docs/HERMES_INTEGRATION.md for the full wire contract this plugin
implements against).

Covers:
  * bridge_client.is_configured() fail-closed behaviour (all three of
    TAX_AGENT_BRIDGE_URL / _SHARED_SECRET / _CHAT_ID required; URL must be
    https, or http only for localhost/127.0.0.1).
  * poller.render_text() covers every named event type without falling
    back to a raw "[EVENT_TYPE]" string, while still relaying
    DRAFT_READY/FINAL_PACKAGE_READY content verbatim.
  * delivery_receipts.py's atomic claim/release -- the core of the
    outbound duplicate-safety guarantee.
  * poller._poll_once() end to end against a mocked Tax Agent bridge,
    including the exact "ack failed, event reappears" scenario that would
    have caused a duplicate Telegram send before the fix.
  * real PluginManager.discover_and_load() auto-loads this plugin (kind:
    backend, bundled) with no plugins.enabled opt-in required, and
    registers all 11 tools.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "tax_content_bridge"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for key in (
        "TAX_AGENT_BRIDGE_URL", "TAX_AGENT_BRIDGE_SHARED_SECRET", "TAX_AGENT_BRIDGE_CHAT_ID",
        "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def _load_module(name: str):
    """Loads one module from plugins/tax_content_bridge/ directly by file
    path, registering it under the plugin's real relative-import package
    name so `from . import X` inside it resolves -- same pattern
    tests/plugins/test_disk_cleanup_plugin.py uses for its own plugin."""
    package_name = "hermes_plugins.tax_content_bridge"
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


# ---------------------------------------------------------------------------
# bridge_client.is_configured()
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_unconfigured_is_false(self):
        bc = _load_module("bridge_client")
        assert bc.is_configured() is False

    def test_url_and_secret_only_without_chat_id_is_false(self, monkeypatch):
        bc = _load_module("bridge_client")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "https://example.trycloudflare.com")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        assert bc.is_configured() is False

    def test_non_numeric_chat_id_is_false(self, monkeypatch):
        bc = _load_module("bridge_client")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "https://example.trycloudflare.com")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_CHAT_ID", "not_numeric")
        assert bc.is_configured() is False

    def test_all_three_valid_is_true(self, monkeypatch):
        bc = _load_module("bridge_client")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "https://example.trycloudflare.com")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_CHAT_ID", "111222333")
        assert bc.is_configured() is True

    def test_plain_http_to_a_real_host_is_rejected(self, monkeypatch):
        bc = _load_module("bridge_client")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "http://example.com")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_CHAT_ID", "111222333")
        assert bc.is_configured() is False

    def test_plain_http_to_localhost_is_accepted(self, monkeypatch):
        bc = _load_module("bridge_client")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "http://127.0.0.1:8766")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_CHAT_ID", "111222333")
        assert bc.is_configured() is True


# ---------------------------------------------------------------------------
# poller.render_text()
# ---------------------------------------------------------------------------

_NAMED_EVENT_TYPES = [
    "TOPIC_SHORTLIST_READY", "DRAFT_READY", "DRAFT_BLOCKED", "TEXT_REVISION_STARTED",
    "VISUAL_REVISION_STARTED", "COMBINED_REVISION_STARTED", "REVISION_BLOCKED",
    "VISUAL_GENERATION_STARTED", "FINAL_PACKAGE_READY", "FINAL_APPROVAL_CONFIRMED",
    "WORKFLOW_BLOCKED", "PUBLISH_SUCCESS", "PUBLISH_FAILURE",
]


class TestRenderText:
    @pytest.mark.parametrize("event_type", _NAMED_EVENT_TYPES)
    def test_named_event_types_never_render_raw(self, event_type):
        pl = _load_module("poller")
        text = pl.render_text({"event_type": event_type, "payload": {"text": "x", "hashtags": []}})
        assert text != f"[{event_type}]"

    def test_draft_ready_relays_exact_text_and_hashtags(self):
        pl = _load_module("poller")
        text = pl.render_text({"event_type": "DRAFT_READY", "payload": {"text": "APPROVED TEXT", "hashtags": ["#tax"]}})
        assert "APPROVED TEXT" in text
        assert "#tax" in text

    def test_genuinely_unknown_event_type_falls_back_to_bracket_tag(self):
        pl = _load_module("poller")
        assert pl.render_text({"event_type": "SOME_FUTURE_TYPE", "payload": {}}) == "[SOME_FUTURE_TYPE]"


# ---------------------------------------------------------------------------
# delivery_receipts: atomic claim/release
# ---------------------------------------------------------------------------

class TestDeliveryReceipts:
    def test_first_claim_succeeds_second_fails(self):
        dr = _load_module("delivery_receipts")
        assert dr.claim_for_delivery("evt-1") is True
        assert dr.claim_for_delivery("evt-1") is False

    def test_release_allows_reclaim(self):
        dr = _load_module("delivery_receipts")
        assert dr.claim_for_delivery("evt-2") is True
        dr.release_claim("evt-2")
        assert dr.claim_for_delivery("evt-2") is True

    def test_claims_survive_a_fresh_connection(self, monkeypatch, tmp_path):
        # Simulates a process restart: same HERMES_HOME, new module import.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        dr = _load_module("delivery_receipts")
        assert dr.claim_for_delivery("evt-restart") is True
        assert dr._db_path().exists()
        # A second "process" (fresh connection) sees the same claim.
        assert dr.claim_for_delivery("evt-restart") is False


# ---------------------------------------------------------------------------
# poller._poll_once() end to end, against a mocked Tax Agent bridge
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json_body


class TestPollOnceDuplicateSafety:
    @pytest.fixture(autouse=True)
    def _configure(self, monkeypatch):
        monkeypatch.setenv("TAX_AGENT_BRIDGE_URL", "https://example.trycloudflare.com")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_SHARED_SECRET", "s3cret")
        monkeypatch.setenv("TAX_AGENT_BRIDGE_CHAT_ID", "111222333")

    def test_ack_failure_does_not_cause_a_duplicate_send(self, monkeypatch):
        pl = _load_module("poller")
        sent = []

        def fake_send_message_tool(args, **kw):
            sent.append(args)
            return '{"success": true}'

        # tools.send_message_tool is a REAL module in this repo -- patch the
        # name poller.py actually imported, not a stub.
        monkeypatch.setattr(pl, "send_message_tool", fake_send_message_tool)

        pending = [{"event_id": "evt-A", "event_type": "VISUAL_GENERATION_STARTED", "payload": {}}]
        acked = []

        def fake_get(url, headers, timeout):
            assert url.endswith("/hermes/pending-events")
            return _FakeResponse(200, {"events": list(pending)})

        def fake_post(url, json, headers, timeout):
            assert url.endswith("/hermes/ack-event")
            acked.append(json["event_id"])
            return _FakeResponse(200, {"status": "ACKED"})

        monkeypatch.setattr(pl.requests, "get", fake_get)
        monkeypatch.setattr(pl.requests, "post", fake_post)

        pl._poll_once()
        assert len(sent) == 1
        assert acked.count("evt-A") == 1

        # Tax Agent never saw the ack (e.g. network blip) -- same event
        # comes back on the next poll. This must NOT resend to Telegram.
        pl._poll_once()
        assert len(sent) == 1, "duplicate Telegram send after a failed ack"
        assert acked.count("evt-A") == 2, "ack should still be retried"

    def test_send_failure_is_retried_not_dropped(self, monkeypatch):
        pl = _load_module("poller")
        sent = []
        fail_once = {"value": True}

        def flaky_send(args, **kw):
            if fail_once["value"]:
                fail_once["value"] = False
                raise RuntimeError("simulated Telegram send failure")
            sent.append(args)
            return '{"success": true}'

        monkeypatch.setattr(pl, "send_message_tool", flaky_send)

        pending = [{"event_id": "evt-B", "event_type": "VISUAL_GENERATION_STARTED", "payload": {}}]
        monkeypatch.setattr(pl.requests, "get", lambda url, headers, timeout: _FakeResponse(200, {"events": pending}))
        monkeypatch.setattr(pl.requests, "post", lambda url, json, headers, timeout: _FakeResponse(200, {}))

        pl._poll_once()
        assert sent == [], "should not have recorded a send on failure"

        pl._poll_once()
        assert len(sent) == 1, "a transient send failure must be retried, not dropped"


# ---------------------------------------------------------------------------
# Regression test for the production incident: PluginManager.discover_and_
# load() caches per process (self._discovered), so setting
# HERMES_GATEWAY_PROCESS *after* the first real discovery pass is too late
# -- register() is never invoked again to see it. This is exactly what the
# first attempt at this fix did (the flag was set deep inside
# GatewayRunner's own startup code), while hermes_cli/main.py's
# _prepare_agent_startup() -- called unconditionally, earlier, for every
# `hermes` invocation including `gateway run` -- had already triggered the
# one and only real discovery pass with the flag still unset. Production
# evidence: zero /hermes/pending-events requests reached Tax Agent after
# that deploy, versus every prior deployment showing traffic within
# seconds.
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.registered: list[str] = []

    def register_tool(self, *, name, toolset, schema, handler, check_fn, emoji):
        self.registered.append(name)


class TestPollerOwnershipRealOrdering:
    def test_flag_set_after_first_discovery_never_starts_the_poller(self, monkeypatch, _isolate_env):
        # Reproduces the actual bug: the process's FIRST real discovery
        # pass happens with the role flag still unset (main.py's own
        # earlier, unconditional discovery for `gateway run`) --
        # register() correctly withholds the poller. The flag then arrives
        # (as it did in the broken fix, deep inside GatewayRunner, only
        # after that first pass), and a second discover_and_load() call
        # runs (gateway/run.py's own redundant call) -- but PluginManager
        # is already discovered, so register() is never called again and
        # the poller never starts. THIS is the production bug, reproduced.
        from hermes_cli import plugins as pmod

        _load_module("bridge_client")  # imports the package once, cheaply
        pkg = sys.modules["hermes_plugins.tax_content_bridge"]
        started = []
        monkeypatch.setattr(pkg, "start_poller", lambda: started.append(True))

        mgr = pmod.PluginManager()
        monkeypatch.delenv("HERMES_GATEWAY_PROCESS", raising=False)
        mgr.discover_and_load()  # first real pass: flag not set yet (the bug)
        assert started == [], "sanity: correctly withheld with the flag unset"

        monkeypatch.setenv("HERMES_GATEWAY_PROCESS", "1")  # arrives too late
        mgr.discover_and_load()  # redundant call, no-op: already discovered

        loaded = mgr._plugins["tax-content-bridge"]
        assert loaded.enabled is True, "tools must still register regardless"
        assert started == [], (
            "BUG REPRODUCED: the poller never starts once discovery has "
            "already cached this process as discovered -- setting the flag "
            "afterward cannot recover it"
        )

    def test_flag_set_before_first_discovery_starts_the_poller(self, monkeypatch, _isolate_env):
        # The corrected ordering: the role flag is set BEFORE the process's
        # only real discover_and_load() call (as hermes_cli/main.py now
        # does, ahead of _prepare_agent_startup()), so register() sees it
        # on the one pass that matters.
        _load_module("bridge_client")
        pkg = sys.modules["hermes_plugins.tax_content_bridge"]
        started = []
        monkeypatch.setattr(pkg, "start_poller", lambda: started.append(True))
        monkeypatch.setenv("HERMES_GATEWAY_PROCESS", "1")  # set BEFORE discovery

        pkg.register(_FakeCtx())

        assert started == [True], "flag set before discovery must start the poller"

    def test_main_sets_the_flag_before_the_first_discovery_call(self):
        # Literal ordering guard on hermes_cli/main.py itself: the line
        # setting HERMES_GATEWAY_PROCESS for the gateway-run case must
        # appear BEFORE the _prepare_agent_startup(args) call that
        # actually triggers the first (and only) real discovery pass for
        # that process. Source-level, not behavioral, but directly guards
        # against the exact regression (the flag ending up written after
        # discovery has already run) recurring via a future reordering.
        import inspect

        from hermes_cli import main as main_mod

        source = inspect.getsource(main_mod)
        flag_pos = source.index('os.environ["HERMES_GATEWAY_PROCESS"] = "1"')
        # _prepare_agent_startup(args) is called from several call sites
        # (chat-launch fast paths, oneshot, ...) earlier in the file --
        # the one that matters here is the unconditional call in the main
        # dispatch flow, which is the NEXT occurrence after the flag line
        # (the flag is placed immediately above it).
        discovery_pos = source.index("_prepare_agent_startup(args)", flag_pos)
        assert flag_pos < discovery_pos, (
            "HERMES_GATEWAY_PROCESS must be set before _prepare_agent_startup() "
            "runs -- setting it after discovery has already run is the exact "
            "bug this test guards against"
        )


# ---------------------------------------------------------------------------
# Real PluginManager discovery -- kind: backend must auto-load, no opt-in
# ---------------------------------------------------------------------------

class TestBundledDiscovery:
    def test_auto_loads_with_no_config_and_registers_all_tools(self, _isolate_env, monkeypatch):
        # Real plugin, real loader -- only the outbound network calls this
        # module might eventually make are irrelevant here; discovery/
        # registration touches no network.
        from hermes_cli import plugins as pmod
        from tools.registry import registry

        mgr = pmod.PluginManager()
        mgr.discover_and_load()

        assert "tax-content-bridge" in mgr._plugins
        loaded = mgr._plugins["tax-content-bridge"]
        assert loaded.manifest.source == "bundled"
        assert loaded.manifest.kind == "backend"
        assert loaded.enabled is True, loaded.error

        expected_tools = {
            "tax_agent_status", "tax_agent_get_workflow_status", "tax_agent_list_topics",
            "tax_agent_select_topic", "tax_agent_research_more", "tax_agent_ask_about_case",
            "tax_agent_accept_draft", "tax_agent_request_text_revision",
            "tax_agent_request_visual_revision", "tax_agent_request_combined_revision",
            "tax_agent_approve_final_package",
        }
        registered = {name for name in expected_tools if registry.get_entry(name) is not None}
        assert registered == expected_tools
