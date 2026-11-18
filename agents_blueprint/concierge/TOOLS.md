# Tools

Declared in `agent.yaml` (`tools.read` / `tools.write`). Documentation for the OpenClaw runtime config; the Hub does not enforce it.

- Read: `Vault/06_Concierge/outbox/`, `Vault/00_System/stats.md`, `Vault/02_Active_Research/`, the ledger in `Vault/05_Ledger/`.
- Write: ONLY `Vault/06_Concierge/inbox/` (`vote-*.json`) and the posted marker `Vault/06_Concierge/outbox/_posted.json`.
- Discord: send and receive messages in the configured channel.
- No shell, no network beyond Discord, no trading tools.
