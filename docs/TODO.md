# TODO (not urgent)

- [ ] **Evidence readiness line in stats.md**: resolved count vs what each question needs (Brier ~30, λ fit ~50, A/B ~30 per arm) + weekly resolution rate.
- [ ] **Freeze the clock in tests**: autouse fixture pinning "now"; audit fixtures for hard-coded dates (one test already broke when its end date passed).
- [ ] **Manipulation thresholds as Overseer knobs**: only after the code-computed comparison (Brier of q on gate-flagged vs passed markets) has enough resolved markets.
- [ ] **OpenClaw sync** when live: `./scripts/sync_openclaw_agent_workspace.sh triage` (schema now requires manipulation_risk).
- [ ] **Fit INTEGRITY thresholds + concentrated top_n basis**: once resolved data exists, fit `config.trading_constants.INTEGRITY` hard-reject and alarm levels against Brier by `integ_score` band; decide whether `top_n` concentration should ever reject (currently record-only).
- [ ] **/v2/oi vs holder sums**: investigate why `/v2/oi?condition_id=` supply totals are inconsistent with `/v2/holders` sums for many markets (drives `basis: supply` fallbacks to `top_n`).
