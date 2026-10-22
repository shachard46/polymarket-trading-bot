# Graph Report - .  (2026-10-22)

## Corpus Check
- 121 files · ~71,928 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1902 nodes · 4800 edges · 99 communities (80 shown, 19 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 421 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Gamma Price Correction|Gamma Price Correction]]
- [[_COMMUNITY_A-IQ Research Bundle|A-IQ Research Bundle]]
- [[_COMMUNITY_Skill Contracts Doc|Skill Contracts Doc]]
- [[_COMMUNITY_Vault Paths & Trade Records|Vault Paths & Trade Records]]
- [[_COMMUNITY_Scraper DB Pruning|Scraper DB Pruning]]
- [[_COMMUNITY_Market Scraper Client|Market Scraper Client]]
- [[_COMMUNITY_Constants & Inactive State|Constants & Inactive State]]
- [[_COMMUNITY_Obsidian Vault Manager|Obsidian Vault Manager]]
- [[_COMMUNITY_Scraper DB Cleanup Scripts|Scraper DB Cleanup Scripts]]
- [[_COMMUNITY_Ledger Stats & Calibration|Ledger Stats & Calibration]]
- [[_COMMUNITY_Research-Edge Gate|Research-Edge Gate]]
- [[_COMMUNITY_Signal Bundle Fixture|Signal Bundle Fixture]]
- [[_COMMUNITY_Atomic Vault Writes|Atomic Vault Writes]]
- [[_COMMUNITY_OpenClaw CLI Config|OpenClaw CLI Config]]
- [[_COMMUNITY_Learning Loop Tests|Learning Loop Tests]]
- [[_COMMUNITY_Scraper Query Tools|Scraper Query Tools]]
- [[_COMMUNITY_Payload Models & Errors|Payload Models & Errors]]
- [[_COMMUNITY_Phase 3 Research Engine|Phase 3 Research Engine]]
- [[_COMMUNITY_Scraper ORM & Scanner|Scraper ORM & Scanner]]
- [[_COMMUNITY_Deep Researcher Parsing|Deep Researcher Parsing]]
- [[_COMMUNITY_Live Prompt Builder|Live Prompt Builder]]
- [[_COMMUNITY_Phase 3 Output Models|Phase 3 Output Models]]
- [[_COMMUNITY_Signal Bundle Test Fixtures|Signal Bundle Test Fixtures]]
- [[_COMMUNITY_Pipeline Hardening Tests|Pipeline Hardening Tests]]
- [[_COMMUNITY_Agent Blueprint Loader|Agent Blueprint Loader]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 90|Community 90]]
- [[_COMMUNITY_Community 92|Community 92]]

## God Nodes (most connected - your core abstractions)
1. `ObsidianManager` - 176 edges
2. `MarketRow` - 87 edges
3. `str` - 67 edges
4. `DeepResearcherComplete` - 67 edges
5. `Any` - 62 edges
6. `ObsidianManager` - 55 edges
7. `str` - 47 edges
8. `VaultWriteError` - 42 edges
9. `parse_deep_researcher()` - 41 edges
10. `AgentSchemaError` - 40 edges

## Surprising Connections (you probably didn't know these)
- `ObsidianManager` --uses--> `ObsidianManager`  [INFERRED]
  orchestrator/stats.py → obsidian_utils.py
- `Any` --uses--> `ObsidianManager`  [INFERRED]
  tests/orchestrator/test_phase5_archive.py → obsidian_utils.py
- `calculate_trade_allocation skill` --inherits--> `BaseModel`  [EXTRACTED]
  skills/calculate-trade-allocation/SKILL.md → orchestrator/schema_validation.py
- `evaluate_market_metrics skill` --inherits--> `BaseModel`  [EXTRACTED]
  skills/evaluate-market-metrics/SKILL.md → orchestrator/schema_validation.py
- `execute_polymarket_trade skill` --inherits--> `BaseModel`  [EXTRACTED]
  skills/execute-polymarket-trade/SKILL.md → orchestrator/schema_validation.py

## Import Cycles
- 1-file cycle: `orchestrator/gate.py -> orchestrator/gate.py`
- 1-file cycle: `orchestrator/phases.py -> orchestrator/phases.py`
- 1-file cycle: `orchestrator/scraper.py -> orchestrator/scraper.py`
- 1-file cycle: `skills/market-scraper/polymarket_tools/api.py -> skills/market-scraper/polymarket_tools/api.py`
- 1-file cycle: `skills/market-scraper/polymarket_tools/migrations/prune_market_changes.py -> skills/market-scraper/polymarket_tools/migrations/prune_market_changes.py`

## Hyperedges (group relationships)
- **Forensic Pipeline Agent Flow** — triage_agents_researchability_triage, briefer_agents_surgical_query_planner, deep_researcher_agents_forensic_fact_verifier, post_mortem_analyst_agents_lead_forensic_auditor, overseer_agents_strategy_optimizer [INFERRED 0.85]
- **Price-Blind Agents** — triage_agents_researchability_triage, briefer_agents_surgical_query_planner, deep_researcher_agents_forensic_fact_verifier [EXTRACTED 0.95]
- **Price-blind research pipeline** — docs_05_agent_schemas_briefer, skills_execute_aiq_query_skill, docs_05_agent_schemas_deep_researcher [EXTRACTED 1.00]
- **Deterministic execution flow** — skills_calculate_trade_allocation_skill, skills_execute_polymarket_trade_skill, claude_orchestrator_hub [EXTRACTED 1.00]

## Communities (99 total, 19 thin omitted)

### Community 0 - "Gamma Price Correction"
Cohesion: 0.05
Nodes (75): GammaBinaryMarket, classify_token_alignment(), _complement_is_sane(), _count_suspicious_ltp_after_token_swap(), _gamma_prices_are_sane(), _gamma_token_count(), GammaBinaryMarket, load_gamma_binary_markets() (+67 more)

### Community 1 - "A-IQ Research Bundle"
Cohesion: 0.07
Nodes (57): _ensure_repo_on_path(), fetch_research_bundle(), _Flight, _fresh(), _load_execute_aiq_query(), normalize_query(), Any, bool (+49 more)

### Community 2 - "Skill Contracts Doc"
Cohesion: 0.06
Nodes (49): datetime, 1. Skill: evaluate_market_metrics, 2. Skill: calculate_trade_allocation, 3. Skill: execute_polymarket_trade, 4. Skill: execute_aiq_query, Hard veto (arbitrage / insufficient data), OpenClaw Skill Contracts, _build_latest_snapshot() (+41 more)

### Community 3 - "Vault Paths & Trade Records"
Cohesion: 0.07
Nodes (59): Path, str, Vault workspace path resolution (env + defaults)., Resolve vault workspace root: explicit arg, then env, then project default., resolve_vault_base(), Hub-built trade record (allocation + execute_polymarket_trade result) — written, Trade Executioner output — written to 03_Trades/., TradeLogPayload (+51 more)

### Community 4 - "Scraper DB Pruning"
Cohesion: 0.07
Nodes (54): _format_count(), _log_progress(), main(), _progress_bar(), Prune old market_change rows, keeping the last 4 days of history.  Rules:   - Ke, Log when crossing ~10% thresholds to avoid spam., Prune stale market_change rows.      Args:         days: History window to keep, run_prune() (+46 more)

### Community 5 - "Market Scraper Client"
Cohesion: 0.08
Nodes (50): _days_to_resolution(), _end_date_from_scraper_row(), _extra_info_dict(), fetch_resolution(), fetch_target_markets(), get_market_trends(), _ingest_limit(), _is_positive_number() (+42 more)

### Community 6 - "Constants & Inactive State"
Cohesion: 0.10
Nodes (45): Configuration constants for the Polymarket Trading Pipeline., _active_frontmatter(), _build_error_log(), build_inactive_filter_payload(), _cascade_phase3_filter_inactive(), clear_inactive(), _filter_confidence(), _flag_payload() (+37 more)

### Community 7 - "Obsidian Vault Manager"
Cohesion: 0.06
Nodes (36): ArgumentParser, ObsidianManager, Single point of truth for all Vault I/O.      Parameters     ----------     vaul, Create all vault subdirectories if they do not already exist., Read any file inside the Vault by path relative to ``vault_base``.          Para, Return every post-mortem markdown file in ``04_Post_Mortems/``., Return trade JSON files that have not yet been archived.          Used by phase, _build_parser() (+28 more)

### Community 8 - "Scraper DB Cleanup Scripts"
Cohesion: 0.07
Nodes (45): Connection, main(), Delete market_change rows where liquidity IS NULL.  These rows are created when, Delete market_change rows where liquidity IS NULL.     Set markets.change_id = N, run_cleanup(), main(), Migration: Set status = 'active' for markets with outcome IS NULL.  Markets with, Set status = 'active' for markets where outcome IS NULL.     Unresolved markets (+37 more)

### Community 9 - "Ledger Stats & Calibration"
Cohesion: 0.15
Nodes (44): bet_pnl(), bootstrap_diff_ci(), brier(), brier_by_directive_version(), calibration_table(), compute_stats(), failure_class_counts(), false_negative_rate() (+36 more)

### Community 10 - "Research-Edge Gate"
Cohesion: 0.10
Nodes (39): arbitrage_combined(), backoff_delay(), _data(), eligibility(), is_due(), is_retired(), _knob(), _momentum_fired() (+31 more)

### Community 11 - "Signal Bundle Fixture"
Cohesion: 0.06
Nodes (41): end_price, pct_move, start_price, threshold, window_hrs, data_quality, newest, oldest (+33 more)

### Community 12 - "Atomic Vault Writes"
Cohesion: 0.10
Nodes (24): _atomic_write_text(), _check_market_id(), _dump_frontmatter(), Path, str, True when an execution intent record exists for ``market_id``., Persist an intent record before the trade skill is called (kept out of ``iter_op, Remove the intent record (no-op if absent). (+16 more)

### Community 13 - "OpenClaw CLI Config"
Cohesion: 0.10
Nodes (41): openclaw_agent_max_attempts(), openclaw_agent_retry_backoff(), openclaw_agent_timeout(), openclaw_bin(), float, Return OpenClaw CLI binary name/path., Return per-agent OpenClaw CLI timeout in seconds., Return max OpenClaw agent CLI attempts before surfacing failure. (+33 more)

### Community 14 - "Learning Loop Tests"
Cohesion: 0.13
Nodes (31): add_resolutions(), doc(), fm_of(), knobs_for(), Overseer, Wave 4: the learning loop (failure classes, gated/bounded/versioned Overseer, ro, A complete directives doc with every knob at its default unless overridden., _scored_rows() (+23 more)

### Community 15 - "Scraper Query Tools"
Cohesion: 0.11
Nodes (38): _build_market_dict_with_change(), _fetch_markets_with_change(), _fetch_single_market_with_change(), get_all_markets(), get_category_markets(), get_closed_markets(), get_market(), get_market_trends() (+30 more)

### Community 16 - "Payload Models & Errors"
Cohesion: 0.10
Nodes (35): Exception, MarketRow, FilterLogPayload, Raised when an agent payload fails Pydantic validation.      The Orchestrator mu, Evaluator / Re-Evaluator output — written to 01_Filters/., Phase 2 gate/triage state for one market — written to 01_Filters/.      ``passed, VaultWriteError, AgentOutputParseError (+27 more)

### Community 17 - "Phase 3 Research Engine"
Cohesion: 0.14
Nodes (37): AgentRunner, aggregate_samples(), _bundle_has_entries(), _bundle_row_usable(), _check_complete(), _dr_payload(), _ensure_as_of(), _extra_sample() (+29 more)

### Community 18 - "Scraper ORM & Scanner"
Cohesion: 0.12
Nodes (36): Market, MarketChange, ORM model for the `markets` table.      Reflects the current state of open/close, ORM model for the `market_change` table.      Log of price/volume/liquidity/midp, BinaryMarket, _derive_status(), _float_or_none(), _get_market_category() (+28 more)

### Community 19 - "Deep Researcher Parsing"
Cohesion: 0.10
Nodes (31): DeepResearcherOutput, ValidationError, Any, str, ValidationError, Validate a Deep Researcher state-machine payload by ``status``., _optional_prob(), parse_deep_researcher() (+23 more)

### Community 20 - "Live Prompt Builder"
Cohesion: 0.10
Nodes (30): build_live_response_hint(), Any, str, Schema-driven live prompts for OpenClaw agent invocations., UTC date as ``YYYY-MM-DD`` (the ``today`` field of price-blind agent payloads)., Build response instructions from ``agent.yaml`` ``output_schema``., today_date(), today_line() (+22 more)

### Community 21 - "Phase 3 Output Models"
Cohesion: 0.14
Nodes (25): DeepResearcherComplete, DeepResearcherNeedsMore, float, Pydantic models for Phase 3 agent JSON contracts., Complete Forensic Fact Verifier payload (price-blind: the model never sees q)., _row(), test_forced_synthesis_fetches_last_pending_queries(), test_poisoned_bundle_is_replanned_and_refetched() (+17 more)

### Community 22 - "Signal Bundle Test Fixtures"
Cohesion: 0.11
Nodes (26): Shared orchestrator test fixtures., Avoid live DB access when phase tests merge quantitative ``signal_bundle``., _stub_evaluator_signal_bundle_fetch(), _stub_signal_bundle_fetch(), attach_signal_bundle(), fetch_signal_bundle(), _load_evaluate_market_metrics(), Any (+18 more)

### Community 23 - "Pipeline Hardening Tests"
Cohesion: 0.10
Nodes (25): _fake_trade(), _p4row(), Core pipeline hardening: loop resilience, infra circuit breaker, idempotent phas, Spy around the real (paper) ``execute_polymarket_trade`` skill., _resolved(), test_content_error_still_flags(), test_flag_inactive_corrupt_file_logs_loudly(), test_flag_inactive_phase2_without_file_writes_minimal_record() (+17 more)

### Community 24 - "Agent Blueprint Loader"
Cohesion: 0.12
Nodes (24): Per-role OpenClaw-style agent workspaces (see ``agents/<role>/``)., load_agents_from_dir(), Any, Path, str, Discover per-role agent workspaces under ``agents/<role>/`` and build ``AGENTS``, Load all agent workspaces; return orchestrator ``AGENTS`` dict keyed by role., build_model() (+16 more)

### Community 25 - "Community 25"
Cohesion: 0.19
Nodes (26): _append(), current_directive_version(), _from_prices(), _from_scalar(), iter_rows(), latest_decision_by_market(), ledger_path(), outcome_yes_from_resolution() (+18 more)

### Community 26 - "Community 26"
Cohesion: 0.10
Nodes (24): main(), OpenClaw Orchestrator entry point.  Lifecycle: Orchestrator builds JSON input →, auto_replay(), bool, str, Return base seconds for exponential backoff between CLI retries., Return whether the orchestrator should clear inactive flags at startup., Return the active runner mode, defaulting to ``stub``.      Raises :class:`Value (+16 more)

### Community 27 - "Community 27"
Cohesion: 0.12
Nodes (25): Overseer output — used to overwrite 00_System/active_directives.md.      The ``n, bounds_table_text(), changed_knobs(), directive_version(), effective_knobs(), _frontmatter(), float, int (+17 more)

### Community 28 - "Community 28"
Cohesion: 0.11
Nodes (18): _has_usable_research_data(), _merge_research_bundle_queries(), Any, bool, float, ObsidianManager — Pydantic-gated file system layer for the Obsidian Vault.  The, YAML frontmatter of an active research report — written to 02_Active_Research/., Append ``new_entries``, skipping duplicate ``query`` strings. (+10 more)

### Community 29 - "Community 29"
Cohesion: 0.12
Nodes (26): _auto_revert_if_worse(), _bounded_post_mortems(), _bounds_payload(), compute_allocation(), _directives_frontmatter(), _ledger_market_fields(), _ledger_safe(), _load_calculate_trade_allocation() (+18 more)

### Community 30 - "Community 30"
Cohesion: 0.16
Nodes (23): full_research_budget(), gate_budget(), _int_env(), max_edge_research_refreshes(), overseer_min_new_resolved(), int, quick_research_budget(), Orchestrator-level configuration: env flags + re-exports of trading constants. (+15 more)

### Community 31 - "Community 31"
Cohesion: 0.16
Nodes (23): agent_error_reason(), coerce_deep_researcher_markdown(), coerce_deep_researcher_state_machine(), extract_fenced_block(), _find_embedded_json_object(), _legacy_markdown_to_complete(), _parse_body_candidates(), preprocess_agent_text() (+15 more)

### Community 32 - "Community 32"
Cohesion: 0.19
Nodes (19): mk(), Phase 2 end to end: gate -> rank -> triage, budgets, caching, ledger., stages(), test_arbitrage_ledgered_not_researched(), test_budgets_respected_and_ranked(), test_event_reject_applies_to_siblings(), test_gate_reject_ledgered_once_with_backoff(), test_retired_after_six_fails() (+11 more)

### Community 33 - "Community 33"
Cohesion: 0.14
Nodes (18): BrieferOutput, OpenClawCLIError, Raised when the OpenClaw CLI cannot run or returns unusable output., InfraUnavailable, BaseException, Gateway/network/disk is unavailable — abort the tick (circuit breaker).      Rai, _Stop, test_run_forever_survives_phase6_failure() (+10 more)

### Community 34 - "Community 34"
Cohesion: 0.14
Nodes (12): _build_seed_directives(), int, Rewrite a legacy non-integer ``version`` (e.g. ``"0.1-seed"``) as integer 1., Highest version number seen in the live file or the history archive., Atomically replace the live file (``.bak`` of the old one) and archive ``v{versi, Parse ``03_Trades/{market_id}.json`` if present; return ``None`` on missing/bad, Read the open trade log JSON for ``market_id`` as a raw string., Move a market's file between Vault directories.          Resolves the source by (+4 more)

### Community 35 - "Community 35"
Cohesion: 0.13
Nodes (16): parse_agent_json_or_yaml(), Parse an agent response into a dict.      Accepts an already-parsed mapping (pas, OpenClaw sometimes returns the JSON object as a JSON-encoded string., test_parse_double_encoded_json_string(), test_parse_empty_output_raises(), test_parse_padded_evaluator_output_with_fenced_json(), test_parse_raw_json_object(), F7 regression: stub runner returns schema-valid responses; stub_error drives ina (+8 more)

### Community 36 - "Community 36"
Cohesion: 0.22
Nodes (19): _briefer_stub(), _deep_researcher_stub(), _evaluator_stub(), _executioner_stub(), _live_session_key(), _overseer_stub(), _post_mortem_stub(), Any (+11 more)

### Community 37 - "Community 37"
Cohesion: 0.12
Nodes (8): _big(), Unit tests for calculate_trade_allocation (docs/03_strategy_math.md)., test_below_min_order_zero(), test_cap_event(), test_cap_f_max(), test_cap_liquidity(), test_cap_total(), test_exposure_exhausted_zero()

### Community 38 - "Community 38"
Cohesion: 0.12
Nodes (4): _patch_openclaw_run(), test_run_agent_uses_session_id_without_agent_on_legacy_cli(), test_run_agent_uses_session_key_when_cli_supports_it(), str

### Community 39 - "Community 39"
Cohesion: 0.12
Nodes (14): In-place error state machine, 1. Core Infrastructure, 2. File System Memory & The Pydantic Gatekeeper, 3. Obsidian Vault Directory Schemas, 4. The Cold Start Protocol, Polymarket Hub-and-Spoke Trading Architecture, Cold Start Protocol, Replay recovery (+6 more)

### Community 40 - "Community 40"
Cohesion: 0.18
Nodes (16): normalize_structured_output(), Backfill known output fields from the orchestrator payload when missing., _stub_deep_researcher_markdown(), _legacy_markdown(), Regression tests for agent response parsing and normalization., Frontmatter carrying the full contract (the Hub no longer invents missing fields, test_coerce_deep_researcher_state_machine_from_legacy_markdown(), test_coerce_deep_researcher_state_machine_markdown_dict() (+8 more)

### Community 41 - "Community 41"
Cohesion: 0.15
Nodes (17): _as_float(), _attempt_cooldown_active(), build_phase3_queue(), _days_left(), _error_free_active_fm(), Merge ``updates`` over the prior filter record and persist (keeps backoff/triage, Scan ``01_Filters/`` for initial research or ``pending_edge_refresh`` work., Refresh TTL: ``max(48h, days_to_resolution * 24h / 10)``. (+9 more)

### Community 42 - "Community 42"
Cohesion: 0.15
Nodes (17): PostMortemPayload, Post-Mortem Analyst output — appended under ## Post-Mortem in 04_Post_Mortems/., _aiq_cache_path(), _context_summary_from_active(), _edge_refresh_research(), _open_allocated_usd(), _planning_context_from_active(), _post_mortem_failed() (+9 more)

### Community 43 - "Community 43"
Cohesion: 0.17
Nodes (13): MarketRow, Hub-validated row carrying every field downstream spokes need.      Constructed, F5 regression: a Deep Researcher market_id mismatch flags the market inactive., test_matching_market_id_proceeds(), test_mismatched_market_id_flags_inactive(), Phase 2: skip markets with an open bet; edge-refresh handoff via pending_edge_re, Phase 3 must not poll trade logs for edge refresh (no pending flag)., test_phase2_edge_refresh_respects_cap() (+5 more)

### Community 44 - "Community 44"
Cohesion: 0.15
Nodes (8): _get_token_ids(), Extract explicit YES and NO token IDs from a binary market., _build_tokens must always place the YES token at index 0., Gamma returns No first — _build_tokens must sort YES to index 0., Price must stay attached to its token after the sort., Winner flag must stay with the resolved token., TestBuildTokensYesFirst, TestGetTokenIds

### Community 45 - "Community 45"
Cohesion: 0.14
Nodes (15): _event_triage_cache(), _open_trade_shows_bet_not_edge_dq(), _persist_phase2_filter(), phase2_quantitative_routing(), Persist quantitative agent output; stamp inactive when ``passed`` is false., True when the last run hit the edge gate (no allocation: best edge below ``MIN_E, Filter markets through the (re-)Evaluator; persist to ``01_Filters/``., ``event_id -> cached triage verdict`` from every filter record on disk. (+7 more)

### Community 46 - "Community 46"
Cohesion: 0.20
Nodes (9): _pnl_rows(), Decision ledger, resolution mapping, stats math, stats.md tick, live prompt date, _row(), test_brier_and_calibration(), test_false_negative_proxy(), test_fit_lambda_recovers_known_value(), test_pnl_annual_offset_and_counterfactual(), test_pnl_annual_offset_taxes_net_per_calendar_year() (+1 more)

### Community 47 - "Community 47"
Cohesion: 0.15
Nodes (13): _error(), calculate_trade_allocation — OpenClaw skill execution module.  Contract: docs/04, ``below_edge_threshold`` is ``True`` iff ``S <= S_0`` (edge gate); ``None`` on t, ``below_edge_threshold`` is ``True`` iff best edge < MIN_EDGE; ``None`` on error, ``q`` is accepted as an alias for ``q_yes_ask``., execute_polymarket_trade — OpenClaw skill execution module.  Contract: docs/04_s, float, int (+5 more)

### Community 48 - "Community 48"
Cohesion: 0.20
Nodes (13): briefer agent.yaml, Annual offset tax mode, Gate and triage funnel, ObsidianManager vault gatekeeper, Orchestrator Hub, Overseer learning loop with BOUNDS, Post-Mortem Analyst, Price-blind research (+5 more)

### Community 49 - "Community 49"
Cohesion: 0.14
Nodes (4): FakeAiqOutput, Unit tests for Hub-side parallel A-IQ bundle fetching., Isolate dynamic import cache between tests., _reset_aiq_cache()

### Community 50 - "Community 50"
Cohesion: 0.18
Nodes (12): _can_import_skill_module(), CLI regression tests for execute-aiq-query/run.py., Prefer OpenClaw gateway layout; fall back to repo config for local CI., run.py must import config from hardcoded OpenClaw root without PYTHONPATH., Smoke: CLI prints ExecuteAiqQueryOutput JSON when A-IQ is mocked., _run_py_env(), test_run_py_imports_without_pythonpath(), test_run_py_invalid_json_exits_1() (+4 more)

### Community 51 - "Community 51"
Cohesion: 0.18
Nodes (13): Calibrated Probability Range (estimated_p, p_low, p_high), Forensic Fact Verifier (deep_researcher), Full vs Quick Mode, needs_more_data follow-up round, Bull/Bear Thesis Markdown Format, active_directives.md, Filter Knobs, Overseer Hard Rules (max 2 knobs, small steps, locked) (+5 more)

### Community 52 - "Community 52"
Cohesion: 0.15
Nodes (9): True when the market artifact exists and frontmatter has ``status: inactive``., Return the YAML frontmatter from ``01_Filters/{market_id}.md`` if present., Validate an Overseer payload and overwrite ``active_directives.md``.          Th, Read parsed frontmatter or JSON root for a market artifact., parse_filters_block(), Return the ``filters:`` mapping from the Filter Weightings yaml block, else ``No, Split ``---`` YAML frontmatter from the following markdown body.      Generic he, split_yaml_frontmatter_markdown() (+1 more)

### Community 53 - "Community 53"
Cohesion: 0.24
Nodes (7): choose_yes_price(), Choose the canonical YES price while preserving raw book summary columns.      y, _book(), Regression tests for YES/NO token resolution and YES-implied midpoint., Mirror _persist_market: NO book mid implies YES mid = 1 - mid_no., TestChooseYesPrice, TestYesMidFromNoBook

### Community 54 - "Community 54"
Cohesion: 0.36
Nodes (10): _fast(), _install_get(), Poll-loop behaviour tests for execute_aiq_query (requests mocked)., _resp(), test_4xx_fails(), test_empty_report_is_error(), test_lowercase_success(), test_not_found_is_terminal() (+2 more)

### Community 55 - "Community 55"
Cohesion: 0.25
Nodes (9): F8 regression: trade JSON is archived after a successful post-mortem., _runner(), _seed_resolved_market(), test_post_mortem_success_archives_trade(), test_subsequent_tick_does_not_re_resolve_archived_trade(), vault(), Any, ObsidianManager (+1 more)

### Community 56 - "Community 56"
Cohesion: 0.25
Nodes (10): _coerce_override(), extract_filter_directives(), Any, Return filter thresholds from the Filter Weightings YAML block.      Falls back, Coerce ``value`` to the type of ``default``; raise ValueError if unusable., Filter directives extraction from active_directives.md., test_extract_filter_directives_custom_yaml(), test_extract_filter_directives_fallback_on_empty() (+2 more)

### Community 57 - "Community 57"
Cohesion: 0.18
Nodes (10): Analytical Mission (Guarding the Sunk Cost), EXECUTION FLOW, OUTPUT SCHEMA, OUTPUT SCHEMA (Critical Infrastructure), Re-Evaluator — operating instructions, `review_kind: "edge_research_refresh"`, `review_kind: "quantitative"`, SHARED RULES (All `review_kind` values) (+2 more)

### Community 58 - "Community 58"
Cohesion: 0.22
Nodes (9): 1. Data Ingestion, 2. Quantitative Routing, 3. Qualitative Pipeline (Decoupled), 4. Execution, 5. Market Resolution & Post-Mortem (Async/Scheduled), 6. Macro-Learning Loop (Every 24-48 Hours), 7. Error Handling (In-Place State Machine), Orchestrator Pipeline (Strict State Flow) (+1 more)

### Community 59 - "Community 59"
Cohesion: 0.25
Nodes (9): Agent blueprints (spokes), Hub-and-Spoke architecture, ExecuteAiqQueryInput, ExecuteAiqQueryOutput, execute_aiq_query — OpenClaw skill execution module.  Contract: docs/04_skills_c, _resolve_config(), float, str (+1 more)

### Community 61 - "Community 61"
Cohesion: 0.29
Nodes (7): Analytical Mission (Hunting the Edge), Context Briefer — operating instructions, EXECUTION FLOW, Query Engineering Rules, Query Planner (briefer) — operating instructions, Turn 1 — Data gathering, Turn 2 — Summary

### Community 62 - "Community 62"
Cohesion: 0.25
Nodes (8): 1. The Evaluator & Re-Evaluator, 2. The Context Briefer, 2. The Query Planner (briefer), 3. The Deep Researcher, 4. The Trade Executioner, 5. The Post-Mortem Analyst, 6. The Overseer (Strategy Optimizer), Agent Personas and I/O Schemas

### Community 63 - "Community 63"
Cohesion: 0.29
Nodes (8): A-IQ Fetch (execute_aiq_query), Forensic Pipeline, Four Fixed-Slot Query Plan, Price-Blind Principle, Surgical Query Planner (briefer), Hub / Orchestrator, Researchability Triage, Researchable Decision

### Community 64 - "Community 64"
Cohesion: 0.25
Nodes (7): Analytical Mission (The First Filter), Evaluator — operating instructions, EXECUTION FLOW, OPERATIONAL RULES, OUTPUT FORMAT (Critical Infrastructure), Turn 1 — Data Gathering, Turn 2 — Evaluation

### Community 65 - "Community 65"
Cohesion: 0.29
Nodes (7): Step 1: Expected Return (After-Tax & Fees), Step 2: Time Adjustment, Step 3: Rarity Bonus, Step 4: Execution Penalty, Step 5: Final Score, Step 6: Bankroll Allocation, Trade Execution Math Protocol

### Community 66 - "Community 66"
Cohesion: 0.29
Nodes (5): _archive_timestamp(), Return a filesystem-safe UTC timestamp suffix for archived trade filenames., Return sorted files under a vault directory matching ``suffix``., Directory for Hub-persisted A-IQ query results (``research_bundles/``)., Move ``03_Trades/{market_id}.json`` to ``03_Trades/_resolved/``.          Called

### Community 67 - "Community 67"
Cohesion: 0.33
Nodes (7): phase3_qualitative_pipeline(), Run FULL research for ``jobs``: events in parallel, same-event markets sequentia, QUICK stage -> Hub decision -> FULL stage; persist research to ``02_Active_Resea, Briefer → Deep Researcher; persist research to ``02_Active_Research/``., _run_full_stage(), market_quarantine(), Flag the market inactive if any unhandled exception escapes the block.

### Community 68 - "Community 68"
Cohesion: 0.29
Nodes (6): Analytical Mission (Evolution & Error Eradication), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Overseer — operating instructions, Overseer (Strategy Optimizer) — operating instructions

### Community 69 - "Community 69"
Cohesion: 0.33
Nodes (5): Execution Mission (Weaponizing the Edge), OPERATIONAL RULES, OUTPUT SCHEMA (Critical Infrastructure), RISK PARAMETER MAPPING (Apply Exactly; Do Not Improvise), Trade Executioner — operating instructions

### Community 70 - "Community 70"
Cohesion: 0.40
Nodes (5): Two full ticks in stub mode with a faked scraper / A-IQ must not crash., _row(), test_two_stub_ticks(), MarketRow, str

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (5): Analytical Mission (The Autopsy), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Post-Mortem Analyst — operating instructions

### Community 73 - "Community 73"
Cohesion: 0.67
Nodes (4): _runner(), _seed_traded_market(), test_post_mortem_losing_bet_pnl_and_invalid_failure_class(), test_post_mortem_payload_fields_and_persistence()

### Community 80 - "Community 80"
Cohesion: 0.67
Nodes (3): DeclarativeBase, Base, Declarative base for ORM models.

## Knowledge Gaps
- **144 isolated node(s):** `Path`, `str`, `Any`, `str`, `Path` (+139 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **19 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `datetime` connect `Skill Contracts Doc` to `Community 32`, `A-IQ Research Bundle`, `Scraper DB Pruning`, `Constants & Inactive State`, `Scraper DB Cleanup Scripts`, `Ledger Stats & Calibration`, `Research-Edge Gate`, `Scraper Query Tools`, `Live Prompt Builder`, `Phase 3 Output Models`, `Community 25`, `Community 28`?**
  _High betweenness centrality (0.203) - this node is a cross-community bridge._
- **Why does `ObsidianManager` connect `Obsidian Vault Manager` to `A-IQ Research Bundle`, `Vault Paths & Trade Records`, `Market Scraper Client`, `Constants & Inactive State`, `Ledger Stats & Calibration`, `Atomic Vault Writes`, `Learning Loop Tests`, `Payload Models & Errors`, `Phase 3 Research Engine`, `Phase 3 Output Models`, `Pipeline Hardening Tests`, `Community 25`, `Community 26`, `Community 27`, `Community 28`, `Community 29`, `Community 30`, `Community 32`, `Community 33`, `Community 34`, `Community 42`, `Community 43`, `Community 46`, `Community 52`, `Community 55`, `Community 56`, `Community 60`, `Community 66`, `Community 70`, `Community 72`?**
  _High betweenness centrality (0.155) - this node is a cross-community bridge._
- **Why does `MarketRow` connect `Community 43` to `Community 32`, `Community 33`, `A-IQ Research Bundle`, `Vault Paths & Trade Records`, `Gamma Price Correction`, `Market Scraper Client`, `Constants & Inactive State`, `Obsidian Vault Manager`, `Community 70`, `Community 42`, `Learning Loop Tests`, `Payload Models & Errors`, `Phase 3 Research Engine`, `Live Prompt Builder`, `Phase 3 Output Models`, `Pipeline Hardening Tests`, `Community 29`, `Community 30`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 78 inferred relationships involving `ObsidianManager` (e.g. with `AgentRunner` and `ArgumentParser`) actually correct?**
  _`ObsidianManager` has 78 INFERRED edges - model-reasoned connections that need verification._
- **Are the 44 inferred relationships involving `MarketRow` (e.g. with `AgentRunner` and `DeepResearcherComplete`) actually correct?**
  _`MarketRow` has 44 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `str` (e.g. with `DeepResearcherComplete` and `DeepResearcherNeedsMore`) actually correct?**
  _`str` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `DeepResearcherComplete` (e.g. with `AgentRunner` and `DeepResearcherNeedsMore`) actually correct?**
  _`DeepResearcherComplete` has 28 INFERRED edges - model-reasoned connections that need verification._