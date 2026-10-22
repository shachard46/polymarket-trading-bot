# Graph Report - polymarket-trading-bot  (2026-10-22)

## Corpus Check
- 114 files · ~71,938 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1896 nodes · 4781 edges · 94 communities (77 shown, 17 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 414 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `2f670fd9`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

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
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
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

## Communities (94 total, 17 thin omitted)

### Community 0 - "Gamma Price Correction"
Cohesion: 0.05
Nodes (75): GammaBinaryMarket, classify_token_alignment(), _complement_is_sane(), _count_suspicious_ltp_after_token_swap(), _gamma_prices_are_sane(), _gamma_token_count(), GammaBinaryMarket, load_gamma_binary_markets() (+67 more)

### Community 1 - "A-IQ Research Bundle"
Cohesion: 0.07
Nodes (59): _ensure_repo_on_path(), fetch_research_bundle(), _Flight, _fresh(), _load_execute_aiq_query(), normalize_query(), Any, bool (+51 more)

### Community 2 - "Skill Contracts Doc"
Cohesion: 0.06
Nodes (49): datetime, 1. Skill: evaluate_market_metrics, 2. Skill: calculate_trade_allocation, 3. Skill: execute_polymarket_trade, 4. Skill: execute_aiq_query, Hard veto (arbitrage / insufficient data), OpenClaw Skill Contracts, _build_latest_snapshot() (+41 more)

### Community 3 - "Vault Paths & Trade Records"
Cohesion: 0.07
Nodes (59): Path, str, Vault workspace path resolution (env + defaults)., Resolve vault workspace root: explicit arg, then env, then project default., resolve_vault_base(), Hub-built trade record (allocation + execute_polymarket_trade result) — written, Trade Executioner output — written to 03_Trades/., TradeLogPayload (+51 more)

### Community 4 - "Scraper DB Pruning"
Cohesion: 0.09
Nodes (37): Namespace, NoReturn, Polymarket CLOB scanner and query tools for OpenClaw., log(), Timestamped logging for scraper operations., Print message to stderr with UTC timestamp prefix., _cmd_get_all_markets(), _cmd_get_category_markets() (+29 more)

### Community 5 - "Market Scraper Client"
Cohesion: 0.08
Nodes (50): _days_to_resolution(), _end_date_from_scraper_row(), _extra_info_dict(), fetch_resolution(), fetch_target_markets(), get_market_trends(), _ingest_limit(), _is_positive_number() (+42 more)

### Community 6 - "Constants & Inactive State"
Cohesion: 0.06
Nodes (60): _error(), calculate_trade_allocation — OpenClaw skill execution module.  Contract: docs/04, ``below_edge_threshold`` is ``True`` iff ``S <= S_0`` (edge gate); ``None`` on t, ``below_edge_threshold`` is ``True`` iff best edge < MIN_EDGE; ``None`` on error, ``q`` is accepted as an alias for ``q_yes_ask``., Configuration constants for the Polymarket Trading Pipeline., execute_polymarket_trade — OpenClaw skill execution module.  Contract: docs/04_s, _active_frontmatter() (+52 more)

### Community 7 - "Obsidian Vault Manager"
Cohesion: 0.10
Nodes (24): ArgumentParser, ObsidianManager, ObsidianManager — Pydantic-gated file system layer for the Obsidian Vault.  The, Single point of truth for all Vault I/O.      Parameters     ----------     vaul, Create all vault subdirectories if they do not already exist., _build_parser(), main(), int (+16 more)

### Community 8 - "Scraper DB Cleanup Scripts"
Cohesion: 0.19
Nodes (17): Connection, _drop_columns_via_recreate(), main(), _market_change_has_column(), _markets_has_column(), Migration: Move market state columns (volume, liquidity, last_trade_price) from, CLI entry: run migration and exit., Return (major, minor, patch) of SQLite version. (+9 more)

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
Cohesion: 0.09
Nodes (22): _atomic_write_text(), _check_market_id(), _dump_frontmatter(), Path, Serialise ``data`` as YAML frontmatter followed by ``body``.      Produces the c, Write raw content to any path inside the Vault.          Parent directories are, Validate an Evaluator payload and write it to ``01_Filters/``.          The file, Validate a Deep Researcher payload and write to ``02_Active_Research/``. (+14 more)

### Community 13 - "OpenClaw CLI Config"
Cohesion: 0.05
Nodes (76): main(), OpenClaw Orchestrator entry point.  Lifecycle: Orchestrator builds JSON input →, auto_replay(), full_research_budget(), gate_budget(), _int_env(), max_edge_research_refreshes(), openclaw_agent_max_attempts() (+68 more)

### Community 14 - "Learning Loop Tests"
Cohesion: 0.12
Nodes (32): add_resolutions(), doc(), fm_of(), knobs_for(), Overseer, Wave 4: the learning loop (failure classes, gated/bounded/versioned Overseer, ro, A complete directives doc with every knob at its default unless overridden., _scored_rows() (+24 more)

### Community 15 - "Scraper Query Tools"
Cohesion: 0.11
Nodes (38): _build_market_dict_with_change(), _fetch_markets_with_change(), _fetch_single_market_with_change(), get_all_markets(), get_category_markets(), get_closed_markets(), get_market(), get_market_trends() (+30 more)

### Community 16 - "Payload Models & Errors"
Cohesion: 0.10
Nodes (45): DeepResearcherComplete, DeepResearcherNeedsMore, Exception, MarketRow, FilterLogPayload, Raised when an agent payload fails Pydantic validation.      The Orchestrator mu, Phase 2 gate/triage state for one market — written to 01_Filters/.      ``passed, VaultWriteError (+37 more)

### Community 17 - "Phase 3 Research Engine"
Cohesion: 0.08
Nodes (90): AgentRunner, aggregate_samples(), _aiq_cache_path(), _auto_revert_if_worse(), _bounded_post_mortems(), _bounds_payload(), _check_complete(), compute_allocation() (+82 more)

### Community 18 - "Scraper ORM & Scanner"
Cohesion: 0.11
Nodes (39): DeclarativeBase, Base, Market, MarketChange, Declarative base for ORM models., ORM model for the `markets` table.      Reflects the current state of open/close, ORM model for the `market_change` table.      Log of price/volume/liquidity/midp, BinaryMarket (+31 more)

### Community 19 - "Deep Researcher Parsing"
Cohesion: 0.11
Nodes (29): DeepResearcherOutput, ValidationError, Any, str, ValidationError, Pydantic models for Phase 3 agent JSON contracts., Validate a Deep Researcher state-machine payload by ``status``., _optional_prob() (+21 more)

### Community 20 - "Live Prompt Builder"
Cohesion: 0.10
Nodes (31): build_live_response_hint(), Any, str, Schema-driven live prompts for OpenClaw agent invocations., UTC date as ``YYYY-MM-DD`` (the ``today`` field of price-blind agent payloads)., Build response instructions from ``agent.yaml`` ``output_schema``., today_date(), today_line() (+23 more)

### Community 21 - "Phase 3 Output Models"
Cohesion: 0.17
Nodes (13): _has_usable_research_data(), _merge_research_bundle_queries(), Any, bool, Append ``new_entries``, skipping duplicate ``query`` strings., Replace a prior bundle row when a refetch succeeds after failure., Append ``new_entries``; refresh duplicate ``query`` rows when the new fetch is b, True when the market artifact exists and frontmatter has ``status: inactive``. (+5 more)

### Community 22 - "Signal Bundle Test Fixtures"
Cohesion: 0.11
Nodes (26): Shared orchestrator test fixtures., Avoid live DB access when phase tests merge quantitative ``signal_bundle``., _stub_evaluator_signal_bundle_fetch(), _stub_signal_bundle_fetch(), attach_signal_bundle(), fetch_signal_bundle(), _load_evaluate_market_metrics(), Any (+18 more)

### Community 23 - "Pipeline Hardening Tests"
Cohesion: 0.07
Nodes (35): _fake_trade(), _p4row(), Core pipeline hardening: loop resilience, infra circuit breaker, idempotent phas, Spy around the real (paper) ``execute_polymarket_trade`` skill., _resolved(), test_content_error_still_flags(), test_flag_inactive_corrupt_file_logs_loudly(), test_flag_inactive_phase2_without_file_writes_minimal_record() (+27 more)

### Community 24 - "Agent Blueprint Loader"
Cohesion: 0.12
Nodes (24): Per-role OpenClaw-style agent workspaces (see ``agents/<role>/``)., load_agents_from_dir(), Any, Path, str, Discover per-role agent workspaces under ``agents/<role>/`` and build ``AGENTS``, Load all agent workspaces; return orchestrator ``AGENTS`` dict keyed by role., build_model() (+16 more)

### Community 25 - "Community 25"
Cohesion: 0.19
Nodes (26): _append(), current_directive_version(), _from_prices(), _from_scalar(), iter_rows(), latest_decision_by_market(), ledger_path(), outcome_yes_from_resolution() (+18 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (18): OpenClaw orchestrator package — Hub-and-Spoke Polymarket trading pipeline.  The, ObsidianManager, Top-level scheduling loop., Run a single phase 1 → 5 sweep over the scraper queue., run_pipeline_tick(), phase1_data_ingestion(), phase3_qualitative_pipeline(), phase5_resolution_and_post_mortem() (+10 more)

### Community 27 - "Community 27"
Cohesion: 0.08
Nodes (38): Overseer output — used to overwrite 00_System/active_directives.md.      The ``n, bounds_table_text(), changed_knobs(), _coerce_override(), directive_version(), effective_knobs(), extract_filter_directives(), _frontmatter() (+30 more)

### Community 28 - "Community 28"
Cohesion: 0.22
Nodes (7): float, YAML frontmatter of an active research report — written to 02_Active_Research/., YAML frontmatter block from the Deep Researcher — written to 02_Active_Research/, ResearchFrontmatter, DirectiveBoundsError, The proposed directives violate the Overseer guard-rails (BOUNDS, step, count, c, float

### Community 29 - "Community 29"
Cohesion: 0.30
Nodes (13): _format_count(), _log_progress(), main(), _progress_bar(), Prune old market_change rows, keeping the last 4 days of history.  Rules:   - Ke, Log when crossing ~10% thresholds to avoid spam., Prune stale market_change rows.      Args:         days: History window to keep, run_prune() (+5 more)

### Community 30 - "Community 30"
Cohesion: 0.23
Nodes (12): _get_db_path(), _get_engine(), _get_engine_url(), _migrate_add_enriched_columns(), Database schema and connection management using SQLAlchemy.  Defines Market and, Add enriched columns to markets table if they do not exist (SQLite migration)., Create the database file and tables if they do not exist.     Migrates existing, Return the database file path from env or default. (+4 more)

### Community 31 - "Community 31"
Cohesion: 0.12
Nodes (27): agent_error_reason(), coerce_deep_researcher_markdown(), extract_fenced_block(), _find_embedded_json_object(), _legacy_markdown_to_complete(), _parse_body_candidates(), preprocess_agent_text(), Any (+19 more)

### Community 32 - "Community 32"
Cohesion: 0.18
Nodes (20): test_phase2_uses_directive_knobs(), mk(), Phase 2 end to end: gate -> rank -> triage, budgets, caching, ledger., stages(), test_arbitrage_ledgered_not_researched(), test_budgets_respected_and_ranked(), test_event_reject_applies_to_siblings(), test_gate_reject_ledgered_once_with_backoff() (+12 more)

### Community 33 - "Community 33"
Cohesion: 0.18
Nodes (9): Architecture, Commands, graphify, Knowledge graph (from `.cursor/rules/graphify.mdc`), Testing notes, Root requirements.txt, Market scraper README, Market scraper requirements.txt (+1 more)

### Community 34 - "Community 34"
Cohesion: 0.19
Nodes (8): int, Rewrite a legacy non-integer ``version`` (e.g. ``"0.1-seed"``) as integer 1., Highest version number seen in the live file or the history archive., Atomically replace the live file (``.bak`` of the old one) and archive ``v{versi, Return the path where the post-mortem report for ``market_id`` lives., Return the raw contents of ``active_directives.md``.          Returns         --, Write a seed ``active_directives.md`` if the file is missing or empty., test_directive_version()

### Community 35 - "Community 35"
Cohesion: 0.11
Nodes (18): parse_agent_json_or_yaml(), Parse an agent response into a dict.      Accepts an already-parsed mapping (pas, OpenClaw sometimes returns the JSON object as a JSON-encoded string., test_parse_double_encoded_json_string(), test_parse_empty_output_raises(), test_parse_padded_evaluator_output_with_fenced_json(), test_parse_raw_json_object(), F7 regression: stub runner returns schema-valid responses; stub_error drives ina (+10 more)

### Community 36 - "Community 36"
Cohesion: 0.21
Nodes (20): _briefer_stub(), _deep_researcher_stub(), _evaluator_stub(), _executioner_stub(), _live_session_key(), _overseer_stub(), _post_mortem_stub(), Any (+12 more)

### Community 37 - "Community 37"
Cohesion: 0.12
Nodes (8): _big(), Unit tests for calculate_trade_allocation (docs/03_strategy_math.md)., test_below_min_order_zero(), test_cap_event(), test_cap_f_max(), test_cap_liquidity(), test_cap_total(), test_exposure_exhausted_zero()

### Community 38 - "Community 38"
Cohesion: 0.12
Nodes (4): _patch_openclaw_run(), test_run_agent_uses_session_id_without_agent_on_legacy_cli(), test_run_agent_uses_session_key_when_cli_supports_it(), str

### Community 39 - "Community 39"
Cohesion: 0.29
Nodes (6): 1. Core Infrastructure, 2. File System Memory & The Pydantic Gatekeeper, 3. Obsidian Vault Directory Schemas, 4. The Cold Start Protocol, Polymarket Hub-and-Spoke Trading Architecture, Cold Start Protocol

### Community 40 - "Community 40"
Cohesion: 0.21
Nodes (16): coerce_deep_researcher_state_machine(), normalize_structured_output(), Backfill known output fields from the orchestrator payload when missing., Normalize Deep Researcher output to the JSON state-machine shape.      Accepts l, _stub_deep_researcher_markdown(), _legacy_markdown(), Regression tests for agent response parsing and normalization., Frontmatter carrying the full contract (the Hub no longer invents missing fields (+8 more)

### Community 41 - "Community 41"
Cohesion: 0.18
Nodes (17): PostMortemPayload, Post-Mortem Analyst output — appended under ## Post-Mortem in 04_Post_Mortems/., _as_float(), _attempt_cooldown_active(), build_phase3_queue(), _days_left(), float, _qualitative_rank_key() (+9 more)

### Community 42 - "Community 42"
Cohesion: 0.47
Nodes (5): main(), Delete market_change rows where liquidity IS NULL.  These rows are created when, Delete market_change rows where liquidity IS NULL.     Set markets.change_id = N, run_cleanup(), int

### Community 43 - "Community 43"
Cohesion: 0.13
Nodes (24): MarketRow, Hub-validated row carrying every field downstream spokes need.      Constructed, F5 regression: a Deep Researcher market_id mismatch flags the market inactive., test_matching_market_id_proceeds(), test_mismatched_market_id_flags_inactive(), test_phase3_passes_real_title_and_description_to_briefer(), Phase 2: skip markets with an open bet; edge-refresh handoff via pending_edge_re, Phase 3 must not poll trade logs for edge refresh (no pending flag). (+16 more)

### Community 44 - "Community 44"
Cohesion: 0.15
Nodes (8): _get_token_ids(), Extract explicit YES and NO token IDs from a binary market., _build_tokens must always place the YES token at index 0., Gamma returns No first — _build_tokens must sort YES to index 0., Price must stay attached to its token after the sort., Winner flag must stay with the resolved token., TestBuildTokensYesFirst, TestGetTokenIds

### Community 45 - "Community 45"
Cohesion: 0.24
Nodes (10): _bundle_has_entries(), _bundle_row_usable(), _open_trade_shows_bet_not_edge_dq(), bool, True when the last run hit the edge gate (no allocation: best edge below ``MIN_E, True only when at least one row carries data and no error (not a poisoned bundle, True when an open trade log reflects a non-zero allocation path (bet placed)., True when the last run hit the edge gate (no allocation due to score vs ``S_0``) (+2 more)

### Community 46 - "Community 46"
Cohesion: 0.20
Nodes (9): _pnl_rows(), Decision ledger, resolution mapping, stats math, stats.md tick, live prompt date, _row(), test_brier_and_calibration(), test_false_negative_proxy(), test_fit_lambda_recovers_known_value(), test_pnl_annual_offset_and_counterfactual(), test_pnl_annual_offset_taxes_net_per_calendar_year() (+1 more)

### Community 47 - "Community 47"
Cohesion: 0.47
Nodes (5): main(), Migration: Set status = 'active' for markets with outcome IS NULL.  Markets with, Set status = 'active' for markets where outcome IS NULL.     Unresolved markets, run_migration(), int

### Community 48 - "Community 48"
Cohesion: 0.40
Nodes (6): get_session(), get_session_factory(), Return the session factory (sessionmaker)., Yield a SQLAlchemy session. Commits on success, rolls back on error.      Usage:, Session, sessionmaker

### Community 49 - "Community 49"
Cohesion: 0.14
Nodes (4): FakeAiqOutput, Unit tests for Hub-side parallel A-IQ bundle fetching., Isolate dynamic import cache between tests., _reset_aiq_cache()

### Community 50 - "Community 50"
Cohesion: 0.18
Nodes (12): _can_import_skill_module(), CLI regression tests for execute-aiq-query/run.py., Prefer OpenClaw gateway layout; fall back to repo config for local CI., run.py must import config from hardcoded OpenClaw root without PYTHONPATH., Smoke: CLI prints ExecuteAiqQueryOutput JSON when A-IQ is mocked., _run_py_env(), test_run_py_imports_without_pythonpath(), test_run_py_invalid_json_exits_1() (+4 more)

### Community 51 - "Community 51"
Cohesion: 0.16
Nodes (14): Calibrated Probability Range (estimated_p, p_low, p_high), Forensic Fact Verifier (deep_researcher), Full vs Quick Mode, needs_more_data follow-up round, Bull/Bear Thesis Markdown Format, overseer agent.yaml, active_directives.md, Filter Knobs (+6 more)

### Community 52 - "Community 52"
Cohesion: 0.10
Nodes (20): Return the YAML frontmatter from ``01_Filters/{market_id}.md`` if present., Split ``---`` YAML frontmatter from the following markdown body.      Generic he, split_yaml_frontmatter_markdown(), _fake_cli(), Regression tests for the vault / parsing / CLI hardening pass., test_corrupt_bundle_backed_up(), test_deep_researcher_error_and_unexpected_status(), test_deep_researcher_non_frontmatter_garbage_raises() (+12 more)

### Community 53 - "Community 53"
Cohesion: 0.24
Nodes (7): choose_yes_price(), Choose the canonical YES price while preserving raw book summary columns.      y, _book(), Regression tests for YES/NO token resolution and YES-implied midpoint., Mirror _persist_market: NO book mid implies YES mid = 1 - mid_no., TestChooseYesPrice, TestYesMidFromNoBook

### Community 54 - "Community 54"
Cohesion: 0.36
Nodes (10): _fast(), _install_get(), Poll-loop behaviour tests for execute_aiq_query (requests mocked)., _resp(), test_4xx_fails(), test_empty_report_is_error(), test_lowercase_success(), test_not_found_is_terminal() (+2 more)

### Community 55 - "Community 55"
Cohesion: 0.33
Nodes (5): main(), Set SQLite journal mode to WAL for better concurrent read/write performance.  Re, Set journal_mode to WAL for the Polymarket database.     Returns True on success, bool, int

### Community 57 - "Community 57"
Cohesion: 0.18
Nodes (10): Analytical Mission (Guarding the Sunk Cost), EXECUTION FLOW, OUTPUT SCHEMA, OUTPUT SCHEMA (Critical Infrastructure), Re-Evaluator — operating instructions, `review_kind: "edge_research_refresh"`, `review_kind: "quantitative"`, SHARED RULES (All `review_kind` values) (+2 more)

### Community 58 - "Community 58"
Cohesion: 0.18
Nodes (10): 1. Data Ingestion, 2. Quantitative Routing, 3. Qualitative Pipeline (Decoupled), 4. Execution, 5. Market Resolution & Post-Mortem (Async/Scheduled), 6. Macro-Learning Loop (Every 24-48 Hours), 7. Error Handling (In-Place State Machine), Orchestrator Pipeline (Strict State Flow) (+2 more)

### Community 59 - "Community 59"
Cohesion: 0.33
Nodes (7): ExecuteAiqQueryInput, ExecuteAiqQueryOutput, execute_aiq_query — OpenClaw skill execution module.  Contract: docs/04_skills_c, _resolve_config(), float, str, execute_aiq_query skill

### Community 61 - "Community 61"
Cohesion: 0.29
Nodes (7): Analytical Mission (Hunting the Edge), Context Briefer — operating instructions, EXECUTION FLOW, Query Engineering Rules, Query Planner (briefer) — operating instructions, Turn 1 — Data gathering, Turn 2 — Summary

### Community 62 - "Community 62"
Cohesion: 0.25
Nodes (8): 1. The Evaluator & Re-Evaluator, 2. The Context Briefer, 2. The Query Planner (briefer), 3. The Deep Researcher, 4. The Trade Executioner, 5. The Post-Mortem Analyst, 6. The Overseer (Strategy Optimizer), Agent Personas and I/O Schemas

### Community 63 - "Community 63"
Cohesion: 0.21
Nodes (11): briefer agent.yaml, A-IQ Fetch (execute_aiq_query), Forensic Pipeline, Four Fixed-Slot Query Plan, Price-Blind Principle, Surgical Query Planner (briefer), deep_researcher agent.yaml, triage agent.yaml (+3 more)

### Community 64 - "Community 64"
Cohesion: 0.25
Nodes (7): Analytical Mission (The First Filter), Evaluator — operating instructions, EXECUTION FLOW, OPERATIONAL RULES, OUTPUT FORMAT (Critical Infrastructure), Turn 1 — Data Gathering, Turn 2 — Evaluation

### Community 65 - "Community 65"
Cohesion: 0.20
Nodes (9): Kelly and exposure caps, Shrink toward market, Step 1: Expected Return (After-Tax & Fees), Step 2: Time Adjustment, Step 3: Rarity Bonus, Step 4: Execution Penalty, Step 5: Final Score, Step 6: Bankroll Allocation (+1 more)

### Community 66 - "Community 66"
Cohesion: 0.15
Nodes (8): _archive_timestamp(), Return a filesystem-safe UTC timestamp suffix for archived trade filenames., Return sorted files under a vault directory matching ``suffix``., Directory for Hub-persisted A-IQ query results (``research_bundles/``)., Return the ``queries`` list from a saved bundle, or ``None`` if missing., Write or merge A-IQ results under ``research_bundles/{market_id}.json``., Move ``03_Trades/{market_id}.json`` to ``03_Trades/_resolved/``.          Called, Delete the saved bundle so the next write starts from fresh data.

### Community 68 - "Community 68"
Cohesion: 0.29
Nodes (6): Analytical Mission (Evolution & Error Eradication), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Overseer — operating instructions, Overseer (Strategy Optimizer) — operating instructions

### Community 69 - "Community 69"
Cohesion: 0.33
Nodes (5): Execution Mission (Weaponizing the Edge), OPERATIONAL RULES, OUTPUT SCHEMA (Critical Infrastructure), RISK PARAMETER MAPPING (Apply Exactly; Do Not Improvise), Trade Executioner — operating instructions

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (5): Analytical Mission (The Autopsy), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Post-Mortem Analyst — operating instructions

### Community 72 - "Community 72"
Cohesion: 0.12
Nodes (11): _build_seed_directives(), str, True when an execution intent record exists for ``market_id``., Persist an intent record before the trade skill is called (kept out of ``iter_op, Remove the intent record (no-op if absent)., Read any file inside the Vault by path relative to ``vault_base``.          Para, Return the path where active research for ``market_id`` lives., Return full markdown for ``02_Active_Research/{market_id}.md`` if present. (+3 more)

### Community 73 - "Community 73"
Cohesion: 0.67
Nodes (4): _runner(), _seed_traded_market(), test_post_mortem_losing_bet_pnl_and_invalid_failure_class(), test_post_mortem_payload_fields_and_persistence()

## Knowledge Gaps
- **148 isolated node(s):** `Commands`, `Architecture`, `Testing notes`, `Knowledge graph (from `.cursor/rules/graphify.mdc`)`, `graphify` (+143 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **17 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `datetime` connect `Skill Contracts Doc` to `Community 32`, `A-IQ Research Bundle`, `Scraper DB Pruning`, `Constants & Inactive State`, `Obsidian Vault Manager`, `Ledger Stats & Calibration`, `Research-Edge Gate`, `Scraper Query Tools`, `Deep Researcher Parsing`, `Live Prompt Builder`, `Community 25`, `Community 30`?**
  _High betweenness centrality (0.208) - this node is a cross-community bridge._
- **Why does `ObsidianManager` connect `Obsidian Vault Manager` to `A-IQ Research Bundle`, `Vault Paths & Trade Records`, `Market Scraper Client`, `Constants & Inactive State`, `Ledger Stats & Calibration`, `Atomic Vault Writes`, `OpenClaw CLI Config`, `Learning Loop Tests`, `Payload Models & Errors`, `Phase 3 Research Engine`, `Phase 3 Output Models`, `Pipeline Hardening Tests`, `Community 25`, `Community 26`, `Community 27`, `Community 28`, `Community 32`, `Community 34`, `Community 41`, `Community 43`, `Community 45`, `Community 46`, `Community 52`, `Community 60`, `Community 66`, `Community 72`?**
  _High betweenness centrality (0.176) - this node is a cross-community bridge._
- **Why does `MarketRow` connect `Community 43` to `Community 32`, `A-IQ Research Bundle`, `Gamma Price Correction`, `Vault Paths & Trade Records`, `Market Scraper Client`, `Constants & Inactive State`, `Obsidian Vault Manager`, `Community 41`, `Community 45`, `Learning Loop Tests`, `Payload Models & Errors`, `Phase 3 Research Engine`, `Live Prompt Builder`, `Pipeline Hardening Tests`, `Community 28`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Are the 78 inferred relationships involving `ObsidianManager` (e.g. with `AgentRunner` and `ArgumentParser`) actually correct?**
  _`ObsidianManager` has 78 INFERRED edges - model-reasoned connections that need verification._
- **Are the 44 inferred relationships involving `MarketRow` (e.g. with `AgentRunner` and `DeepResearcherComplete`) actually correct?**
  _`MarketRow` has 44 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `str` (e.g. with `DeepResearcherComplete` and `DeepResearcherNeedsMore`) actually correct?**
  _`str` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `DeepResearcherComplete` (e.g. with `AgentRunner` and `DeepResearcherNeedsMore`) actually correct?**
  _`DeepResearcherComplete` has 28 INFERRED edges - model-reasoned connections that need verification._