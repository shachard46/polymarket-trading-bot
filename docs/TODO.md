# TODO (not urgent)

- [ ] **Evidence readiness line in stats.md**: resolved count vs what each question needs (Brier ~30, λ fit ~50, A/B ~30 per arm) + weekly resolution rate.
- [ ] **Freeze the clock in tests**: autouse fixture pinning "now"; audit fixtures for hard-coded dates (one test already broke when its end date passed).
- [ ] **Manipulation thresholds as Overseer knobs**: only after the code-computed comparison (Brier of q on gate-flagged vs passed markets) has enough resolved markets.
- [ ] **OpenClaw sync** when live: `./scripts/sync_openclaw_agent_workspace.sh triage` (schema now requires manipulation_risk).
- [ ] **Fit INTEGRITY thresholds + concentrated top_n basis**: once resolved data exists, fit `config.trading_constants.INTEGRITY` hard-reject and alarm levels against Brier by `integ_score` band; decide whether `top_n` concentration should ever reject (currently record-only).
- [ ] **/v2/oi vs holder sums**: investigate why `/v2/oi?condition_id=` supply totals are inconsistent with `/v2/holders` sums for many markets (drives `basis: supply` fallbacks to `top_n`).
- [ ] **Batch API for triage and shadow runs**: triage and shadow calls are not latency-critical; use the Anthropic Batch API in `direct` mode to cut cost.
- [ ] **Pass `role=` to `_ledger_market_fields`**: `common._ledger_market_fields(market_data, role)` supports `model`, but no caller passes `role`, so ledger rows lack the model in direct mode.
- [ ] **Retire the OpenClaw research path**: once the `direct` backend matches OpenClaw on the shadow comparison (`stats.md` runner-backend Brier), drop the `live` research path.
- [ ] **Refactor `obsidian_utils.py`**: split into schemas (Pydantic payload models) and manager (vault I/O and the write gatekeeper).
- [ ] **Refactor `stats.py`**: split into per-section modules (Brier, shadows, integrity, offers, P&L, lambda fit).
- [ ] **Stop/promote rule per measure-only experiment**: for each shadow, independent-sample and triage-shadow check, write the sample size and the Brier or P&L result that promotes it to a filter or stops it.
- [ ] **Dead-code audit**: remove the `per_trade` tax mode, the legacy `BANKROLL_USD` path and the `state.py` replay code if nothing reads them.
