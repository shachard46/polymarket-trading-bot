# Concierge (Discord bot)

The concierge is the user-facing side of the bot: the built-in Discord bot (`orchestrator/discord_bot.py`), the only front-end. Its model is the `concierge` role in `config/models.yaml`. It relays what the Hub wrote and records yes/no votes. It never decides, trades, filters or tunes knobs, and it keeps no user preferences. The Hub writes, the concierge reads, code decides.

Spec: `agents_blueprint/concierge/` (`AGENTS.md` Q&A prompt, `agent.yaml`, `hub_spawned: false`). Bot: `orchestrator/discord_bot.py`. Hub side: `orchestrator/notify.py`, `p4_approval.process_inbox`, `p6_overseer.process_directive_votes`.

## 1. Setup

The bot reads and writes `Vault/06_Concierge/` directly, so it must run on the machine that holds the vault (same `PMBOT_VAULT_PATH`, or repo root).

1. Create the bot and set its env: section 5.
2. Hub environment: set the flags you want (section 3). `DISCORD_WEBHOOK_URL` is optional. It pushes offers and the digest to a Discord channel by webhook, in addition to the bot; failures are logged only.
3. Run the bot: `python3 -m orchestrator.discord_bot`, on the vault machine.
4. Smoke check: run the Hub with `CONCIERGE_APPROVAL=1`. Confirm an `outbox/offer-*.json` appears, the bot posts it with Yes and No buttons, and its name appears in `outbox/_posted.json`. Press Yes. Confirm the vote file moves to `inbox/_done/` and the ledger has `user_vote` and `offer_approved` or `offer_stale`.

## 2. Files

```
Vault/06_Concierge/
  outbox/                       Hub -> user
    offer-<market_id>-<tick>.json
    digest-<YYYY-MM-DD>.json
    directive-v<N>.md           only when CONCIERGE_APPROVE_DIRECTIVES=1
    _pending/                   directive payloads (Hub only)
    _done/                      processed or expired files
    _posted.json                posted file names (bot-owned)
  inbox/                        user -> Hub
    vote-<offer_id>.json
    vote-directive-v<N>.json
    _done/
```

### Offer: `outbox/offer-<market_id>-<tick>.json`

`offer_id` = `<market_id>-<tick>`; `tick` = UTC `YYYYMMDDTHHMMSS`.

- `offer_id`, `market_id`, `question`, `url`
- `mode`: `approval` (vote needed) or `informational` (already placed). `status`: `pending` or `informational`.
- `side`, `p`, `p_low`, `p_high`, `q_entry`, `edge`, `allocation_usd`, `shares`, `price`, `ask`, `depth_usd`
- `evidence_summary` (first 500 characters of the research body), `manipulation_risk`, `integ_score`, `integ_flags`
- `created_at`, `expires_at` (`CONCIERGE_OFFER_TTL_SEC` after creation)
- `resume`: approval offers only. Hub-internal; the bot ignores it.

### Digest: `outbox/digest-<date>.json`

One per UTC day, written by the first tick of that day.

- `date`, `generated_at`, `realised_pnl_after_tax`, `counterfactual_pnl_after_tax`, `brier_p`, `brier_q`, `n_resolved`
- `resolved_this_week`, `new_trades`, `new_offers` (last 24 h), `integrity_rejects` (reason to count, last 24 h)
- `pending_directive_proposal`: file name or `null`

### Directive proposal: `outbox/directive-v<N>.md`

Written only when `CONCIERGE_APPROVE_DIRECTIVES=1` and the Overseer proposes a change. Frontmatter: `proposal_id` (`directive-v<N>`), `base_version`, `new_version`, `created_at`, `expires_at` (`CONCIERGE_DIRECTIVE_TTL_SEC`). Body: knob changes, reason, stats excerpt, then `Reply yes or no. Vote file: inbox/vote-directive-v<N>.json`.

### Votes

Offer vote, `inbox/vote-<offer_id>.json`:
```json
{"offer_id": "<market_id>-<tick>", "vote": "yes"}
```

Directive vote, `inbox/vote-directive-v<N>.json`:
```json
{"proposal_id": "directive-v<N>", "vote": "yes"}
```

- `vote` is `yes` or `no`. The bot writes a vote only after an explicit yes or no from you (button press). Ambiguous input writes nothing.
- Offer votes: the Hub reads `offer_id` from the body. Directive votes: the Hub reads the id from the file name, so keep `vote-directive-v<N>.json`.
- Invalid votes (bad value, unknown offer, offer not `mode: approval`) are ignored and moved to `_done/`.

## 3. Flags and behaviour

| Flag | Off (default) | On |
|---|---|---|
| `CONCIERGE_APPROVAL` | Phase 4 trades as before. An informational offer is written after each trade and the bot reports it as placed. Votes are not read. | Phase 4 writes `offer_pending` and does not trade. Nothing executes without a yes vote. Works the same in paper and live mode. |
| `CONCIERGE_APPROVE_DIRECTIVES` | Overseer changes are applied directly (bounds, cooldown, auto-revert). | Each change becomes `directive-v<N>.md` and waits for a vote. |
| `CONCIERGE_POLL_SEC` | Not used: the loop sleeps the full pipeline interval. | Between ticks the loop reads `inbox/` every N seconds. Only the flags that are on are polled. |
| `CONCIERGE_OFFER_TTL_SEC` | Written into `expires_at`; no effect. | Offers past it are closed `offer_expired` by the poll. Default 14400 (4 h). |
| `CONCIERGE_DIRECTIVE_TTL_SEC` | Written into `expires_at` of proposals; no effect. | Proposals past it are dropped. Default 172800 (48 h). |
| `DISCORD_WEBHOOK_URL` | No push. The bot still relays outbox files. | Each offer and each digest is also posted by webhook. Failures are logged, never raised. |

With `CONCIERGE_APPROVAL=1`, an offer resolves as:

- `yes`, edge still clears `effective_min_edge()` after re-quote and re-size: executes. Ledger `offer_approved`, trade log in `03_Trades/`.
- `yes`, edge gone: `offer_stale`. Trade log `below_edge`; a later research refresh may re-offer.
- `yes`, quote or market fetch failed (infra): vote kept, retried on each poll until expiry.
- `no`: `offer_declined`.
- No vote before `expires_at`: `offer_expired`.
- Each vote is ledgered once as `user_vote`. Declined and expired offers count in the counterfactual P&L.

With `CONCIERGE_APPROVE_DIRECTIVES=1`, a proposal resolves as:

- `yes`, directives still at `base_version`: re-validated by `write_directives` and applied as a new version.
- `yes`, directives moved since the proposal: rejected, dropped.
- `no`, or no vote before expiry: dropped. The Overseer's 24 h throttle still applies.

Turning `CONCIERGE_APPROVAL` off stops vote processing, so pending offers stay pending until it is turned back on. Their intents keep the market blocked meanwhile.

## 4. Safety

- The Hub acts only on vote files. It never reads Discord directly, and the bot never writes to the Vault except `inbox/` and `_posted.json`.
- Offers and proposals are relayed as the Hub wrote them. The bot adds no filter and no preference.
- Stats: `stats.md` section 'Offers & votes' shows approved, declined, expired and stale counts, plus Brier and counterfactual P&L per group once resolved.

## 5. Discord bot

`orchestrator/discord_bot.py`, a discord.py process. It is the only front-end and is never imported by the Hub. Run it on the machine that holds the vault. It reads and writes the same `outbox/` and `inbox/` as the Hub.

### Setup

1. Create an application in the Discord developer portal (https://discord.com/developers/applications). Under Bot, add a bot and copy its token. Never log or commit the token.
2. Under Bot, enable the Message Content Intent. The owner Q&A reads message text.
3. Under OAuth2, URL Generator, pick scope `bot` and permission Send Messages. Open the generated URL and invite the bot to your server.
4. In Discord, enable Developer Mode (User Settings, Advanced). Right-click the private channel and copy its ID. Right-click your own user and copy its ID.
5. Set the env:
   ```bash
   export DISCORD_BOT_TOKEN=...          # required
   export DISCORD_CHANNEL_ID=...         # required: the private channel
   export DISCORD_OWNER_ID=...           # required: only this user can vote or ask
   export CONCIERGE_TZ=UTC               # optional, default UTC
   export CONCIERGE_QUIET_HOURS=23-07    # optional: digests wait in this window
   export CONCIERGE_POLL_SEC=300         # optional
   ```
   Set the Hub flags you want (section 3). The relay and votes need no model. Owner Q&A calls the `concierge` role's model from `config/models.yaml` (default: anthropic, needs `ANTHROPIC_API_KEY`). To use another provider, set `LLM_ROLE_CONCIERGE=provider:model` and that provider's key.
6. Run `python3 -m orchestrator.discord_bot`. Smoke check as in section 1, step 4: the offer appears in the channel with Yes and No buttons, and the vote moves to `inbox/_done/`.

### Behaviour

- Relays `offer-*.json`, `digest-*.json` and `directive-*.md` from the outbox, once each. Offers and proposals go out even in quiet hours. Digests wait until the window ends.
- Only `DISCORD_OWNER_ID` can press buttons or ask questions. Other users are ignored.
- Button presses write `inbox/vote-<id>.json`. The Hub validates the vote as for any vote file.
- Questions in the channel or by DM get a read-only answer from `stats.md`, the last ledger rows and the research notes named in the question.
