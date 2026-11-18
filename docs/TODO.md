# TODO (not urgent)

- [ ] **Evidence readiness line in stats.md**: resolved count vs what each question needs (Brier ~30, λ fit ~50, A/B ~30 per arm) + weekly resolution rate.
- [ ] **Freeze the clock in tests**: autouse fixture pinning "now"; audit fixtures for hard-coded dates (one test already broke when its end date passed).
- [ ] **Manipulation thresholds as Overseer knobs**: only after the code-computed comparison (Brier of q on gate-flagged vs passed markets) has enough resolved markets.
- [ ] **Fit INTEGRITY thresholds + concentrated top_n basis**: once resolved data exists, fit `config.trading_constants.INTEGRITY` hard-reject and alarm levels against Brier by `integ_score` band; decide whether `top_n` concentration should ever reject (currently record-only).
- [ ] **/v2/oi vs holder sums**: investigate why `/v2/oi?condition_id=` supply totals are inconsistent with `/v2/holders` sums for many markets (drives `basis: supply` fallbacks to `top_n`).
- [ ] **Batch API for triage and shadow runs**: triage and shadow calls are not latency-critical; use the Anthropic Batch API in `live` mode to cut cost.
- [x] **Refactor `obsidian_utils.py`**: split into schemas (Pydantic payload models) and manager (vault I/O and the write gatekeeper).
- [x] **Refactor `stats.py`**: split into per-section modules (Brier, shadows, integrity, offers, P&L, lambda fit).
- [ ] **Stop/promote rule per measure-only experiment**: for each shadow, independent-sample and triage-shadow check, write the sample size and the Brier or P&L result that promotes it to a filter or stops it.
- [x] **Dead-code audit**: `per_trade` tax mode removed; `BANKROLL_USD` kept as live (not legacy).
- [ ] **Dead-code audit: `state.py` replay code**: still read by `orchestrator/replay.py` and `loop.py` auto-replay; remove only if those go.
- [ ] **deploy/ folder for Lightsail**: systemd units for the Hub and the Discord bot, a setup script, and a daily off-box Vault backup.
- [ ] **Token usage and cost per call**: record input and output tokens and cost on ledger rows; add a cost/day line to `stats.md` with Brier per dollar.
- [ ] **Cap web-fetch size**: limit bytes per `web_fetch` result to cut Web Searcher cost.
- [x] **Rename the env vars to the neutral `PMBOT_` prefix** in one pass (config, tests, docs, the server env files), together with the `llm_*` helpers in `orchestrator/config.py`.
- [x] **Verify Gemini request shape (done 2026-11-18, docs only)**: generateContent at /v1beta/models/{m}:generateContent + `x-goog-api-key` confirmed; REST ref names the tool `googleSearch` (now used; snake_case `google_search` is Interactions-API style); JSON mode + google_search combine only on Gemini 3/4 series (400 fallback kept); `toolUsePromptTokenCount` now counted as input. Old note:: check the `google_search` tool name (vs `googleSearch`) and the `x-goog-api-key` header against the current AI Studio docs. `GeminiLLM` was written without a live call.
