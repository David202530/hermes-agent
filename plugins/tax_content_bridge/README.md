# tax-content-bridge

Bundled plugin connecting this Hermes instance to the **Tax Content
Intelligence Agent** — David's own research/drafting/approval pipeline for
Georgian tax-dispute LinkedIn content. Full contract, architecture, and
tunnel-setup instructions live in that repo:
`docs/HERMES_INTEGRATION.md` (`tax-content-intelligence-agent`).

## What this plugin is

Eleven `register_tool` tools (topic discovery, topic selection, research,
questions, draft acceptance, text/visual/combined revision, final-package
approval, status) plus one background thread that polls the Tax Agent
bridge for outbound events and delivers them via this Hermes instance's own
Telegram sending path.

## What this plugin is NOT

- Not a tax-reasoning, Writer, evidence-checking, or approval engine — every
  tool forwards to the Tax Agent's own bridge, which is the sole authority.
- Not a LinkedIn publisher — `PUBLISH_LINKEDIN` is not exposed as a tool.
- Not a raw-text hook — no `pre_gateway_dispatch`, no message interception.
  Every action is an explicit, schema-validated tool call the agent
  chooses to make.

## Configuration

Set these three (Railway variables, or your local `.env` for a self-hosted
instance) or every tool call fails closed with a clear error and the poller
idles quietly:

- `TAX_AGENT_BRIDGE_URL` — the Tax Agent's tunnel HTTPS URL.
- `TAX_AGENT_BRIDGE_SHARED_SECRET` — must match the Tax Agent repo's
  `HERMES_BRIDGE_SHARED_SECRET`.
- `TAX_AGENT_BRIDGE_CHAT_ID` — the numeric Telegram chat id outbound
  messages are delivered to (must match the Tax Agent repo's
  `HERMES_AUTHORIZED_TELEGRAM_USER_ID`).

Never commit real values for any of these.
