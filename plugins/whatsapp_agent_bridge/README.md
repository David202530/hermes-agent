# whatsapp-agent-bridge

Bundled plugin connecting this Hermes instance to David's own, locally-run
**WhatsApp Agent** (a separate repo: `D:\AI Agent\Whatsapp AI`). Full
contract, card catalogue, and opaque-token rules live in that repo:
`docs/whatsapp-agent/PHASE_5.md`.

## What this plugin is

Ten `register_tool` tools (list pending, get current, draft, regenerate,
approve, send now, cancel send, remind later, dismiss, resolve unclear
send) plus one small inbound HTTP listener that receives WhatsApp Agent's
pushed reminder notifications and relays them to Telegram as plain text.

## What this plugin is NOT

- Not a WhatsApp client -- never imports whatsapp-web.js, never touches
  LocalAuth/session data, never calls `sendMessage`.
- Not a drafting, classification, or approval engine -- every tool forwards
  an Agent-issued opaque `action_token`; WhatsApp Agent's own
  `HumanSendService` is the sole authority on whether anything is valid or
  safe to send.
- Not a second copy of the Agent's conversation database -- this plugin
  stores only the current card per thread and its action tokens (see
  `store.py`'s module docstring for the exact schema and what is
  deliberately never stored: phone numbers, WhatsApp internal IDs, message
  history, LocalAuth data).
- Not related to Hermes's own native WhatsApp (Cloud API) integration --
  entirely separate code path, untouched by this plugin.

## Why this plugin needs an inbound listener, unlike tax_content_bridge

`plugins/tax_content_bridge` never needs Hermes to receive anything: it
polls the Tax Agent's own `GET /hermes/pending-events`. WhatsApp Agent has
no equivalent pull endpoint -- its `HermesEventSink` only ever **pushes** a
`WHATSAPP_REMINDER_DUE` notification to a configured HTTPS URL. Hermes's
plugin framework has no lightweight "receive an HTTP POST" extension point
(see `ingress.py`'s docstring for the full survey of `hermes_cli/plugins.py`
and why `register_platform`/`BasePlatformAdapter` and the generic
`gateway/platforms/webhook.py` adapter both don't fit), so this plugin owns
a minimal `http.server.ThreadingHTTPServer` instead, gated by
`HERMES_GATEWAY_PROCESS` exactly like that plugin's poller.

All *other* cards (draft, final confirmation, send result) are never
pushed separately -- they arrive as the direct HTTP response to whichever
tool call this plugin made (see `docs/whatsapp-agent/PHASE_4.md`'s
`HermesActionResult` in the Agent repo). So the ingress listener only ever
needs to understand one event type.

## Configuration

All required (Railway variables, or a local `.env` for self-hosted testing)
or the bridge stays fully inert -- every tool registered but refusing to
dispatch, and the ingress listener never binds a port:

- `WHATSAPP_AGENT_BRIDGE_ENABLED` -- must be `true`. Default `false`.
- `WHATSAPP_AGENT_SHARED_SECRET` -- must match the Agent's own
  `HERMES_BRIDGE_SHARED_SECRET`, at least 32 characters.
- `WHATSAPP_AGENT_CALLBACK_URL` -- a bare HTTPS origin (no path/query),
  e.g. `https://<cloudflare-tunnel-hostname>`. Plain `http` is only
  accepted for `localhost`/`127.0.0.1` (same-machine dev).
- `WHATSAPP_AGENT_AUTHORIZED_USER_ID` -- numeric Telegram chat id, the
  target for inbound-push notifications. This is **not** the value sent as
  `hermes_user_id` on outbound calls -- see `callback_client.py`.
- `WHATSAPP_AGENT_CF_ACCESS_CLIENT_ID` / `WHATSAPP_AGENT_CF_ACCESS_CLIENT_SECRET`
  -- optional, both-or-neither. When both are set, every outbound callback
  request carries `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers in
  addition to WhatsApp Agent's own Bearer auth (defense in depth: Cloudflare
  Access validates first, then the Agent's own application auth). No
  credentials are created by this plugin; these only add header support.
- `WHATSAPP_AGENT_BRIDGE_HOST` / `WHATSAPP_AGENT_BRIDGE_PORT` -- ingress
  listener bind host/port. Default `0.0.0.0` / `8878`. Reaching this port
  from the public internet requires a Railway domain/target-port mapping
  that does not exist yet -- a later, separate deployment step.

Never commit real values for any of these.

## Authentication

- **Agent → Hermes** (the ingress): `Authorization: Bearer <shared secret>`,
  checked with `hmac.compare_digest` (constant-time). There is no separate
  actor/user field on this leg in WhatsApp Agent's own wire contract --
  the push carries only `{event_id, event_type, card}` -- so the shared
  secret is the only inbound identity check that exists to make; the
  configured `WHATSAPP_AGENT_AUTHORIZED_USER_ID` is used solely as the
  Telegram notification target, not as an inbound authorization check.
- **Hermes → Agent** (the callback): the same Bearer shared secret, plus
  `hermes_user_id` read from the real, current Telegram session context
  (never a tool argument or static config -- see `callback_client.py`),
  which the Agent itself validates against its one allowlisted actor id.
  Optional Cloudflare Access headers, added only when both are configured.

## Opaque token handling

Tokens are stored and relayed as opaque strings only -- this plugin never
decodes, validates, or fabricates one. Each tool looks up the token bound
to its specific action id on the current thread; if none is found, the
tool fails closed with a plain message. `whatsapp_agent_send_now` has no
path to a token except one WhatsApp Agent itself issued on a fresh final
confirmation card, returned only after `whatsapp_agent_approve_draft`
succeeded -- there is no code path in this plugin that can construct,
reuse, or guess a send authorization.

## Local simulation

Tests use fake in-process HTTP fixtures standing in for WhatsApp Agent --
no real network call, no tunnel, no WhatsApp message. See
`tests/plugins/test_whatsapp_agent_bridge_plugin.py`.
