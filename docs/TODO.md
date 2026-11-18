# TODO (not urgent)

- [ ] **Evidence readiness line in stats.md**: resolved count vs what each question needs (Brier ~30, λ fit ~50, A/B ~30 per arm) + weekly resolution rate.
- [ ] **Freeze the clock in tests**: autouse fixture pinning "now"; audit fixtures for hard-coded dates (one test already broke when its end date passed).
- [ ] **Manipulation thresholds as Overseer knobs**: only after the code-computed comparison (Brier of q on gate-flagged vs passed markets) has enough resolved markets.
- [ ] **OpenClaw sync** when live: `./scripts/sync_openclaw_agent_workspace.sh triage` (schema now requires manipulation_risk).
