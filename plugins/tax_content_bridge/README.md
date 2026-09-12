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

Set all three (Railway variables, or your local `.env` for a self-hosted
instance) or `is_configured()` fails closed: every tool is registered but
refuses to dispatch, and the poller idles quietly instead of crashing:

- `TAX_AGENT_BRIDGE_URL` — the Tax Agent's tunnel HTTPS URL. Must be
  `https://` for anything but `localhost`/`127.0.0.1` — a plain `http` URL
  to a real tunnel is refused, since it would leak the shared secret and
  every action payload in transit.
- `TAX_AGENT_BRIDGE_SHARED_SECRET` — must match the Tax Agent repo's
  `HERMES_BRIDGE_SHARED_SECRET`.
- `TAX_AGENT_BRIDGE_CHAT_ID` — numeric only. The Telegram chat id outbound
  messages are delivered to (must match the Tax Agent repo's
  `HERMES_AUTHORIZED_TELEGRAM_USER_ID`). Required even though the inbound
  tools don't read it directly: without it the poller can never deliver
  anything, so a partial config (URL + secret only) would let David
  approve actions that silently never produce a visible reply.

Never commit real values for any of these.

## Outbound delivery: duplicate-safety

`delivery_receipts.py` claims each Tax Agent `event_id` (a SQLite `INSERT`
under a `UNIQUE` constraint, atomic) *before* sending it to Telegram, and
only releases the claim if the send itself fails. So a successful send is
never repeated even if the following ack fails and the same event
reappears on the next poll — the poller just retries the ack in that case.
See that module's docstring for the exact guarantee and its one
assumption: this Hermes service currently runs exactly one replica/region
(verified against Railway at the time this was written); the SQLite store
also protects a future multi-replica deployment *if* replicas share its
volume, but not otherwise.

## Event rendering

`DRAFT_READY`/`FINAL_PACKAGE_READY` relay Tax Agent's exact text + hashtags
verbatim — never paraphrased. Every other event type the Tax Agent spec
names gets a fixed, concise Georgian status line (see `poller.py`'s
`_STATUS_TEXT`); only a genuinely unrecognized future event type falls
back to a bracketed tag. `PUBLISH_SUCCESS`/`PUBLISH_FAILURE` are rendered
for forward-compatibility — real publication stays disabled via Tax
Agent's own gate regardless.
