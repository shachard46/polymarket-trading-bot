# Graph Report - polymarket-trading-bot  (2026-06-01)

## Corpus Check
- 84 files · ~31,342 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1070 nodes · 2187 edges · 79 communities (67 shown, 12 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 138 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ddf12122`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
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

## God Nodes (most connected - your core abstractions)
1. `ObsidianManager` - 92 edges
2. `MarketRow` - 43 edges
3. `str` - 35 edges
4. `AgentOutputParseError` - 32 edges
5. `str` - 31 edges
6. `parse_agent_json_or_yaml()` - 30 edges
7. `spawn_agent()` - 30 edges
8. `ObsidianManager` - 29 edges
9. `VaultWriteError` - 28 edges
10. `Any` - 26 edges

## Surprising Connections (you probably didn't know these)
- `ArgumentParser` --uses--> `ObsidianManager`  [INFERRED]
  orchestrator/replay.py → obsidian_utils.py
- `int` --uses--> `ObsidianManager`  [INFERRED]
  orchestrator/replay.py → obsidian_utils.py
- `str` --uses--> `ObsidianManager`  [INFERRED]
  orchestrator/replay.py → obsidian_utils.py
- `Any` --uses--> `ObsidianManager`  [INFERRED]
  tests/orchestrator/test_phase5_archive.py → obsidian_utils.py
- `Any` --uses--> `VaultWriteError`  [INFERRED]
  orchestrator/phases.py → obsidian_utils.py

## Import Cycles
- 1-file cycle: `skills/evaluate-market-metrics/evaluate_market_metrics.py -> skills/evaluate-market-metrics/evaluate_market_metrics.py`

## Communities (79 total, 12 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.10
Nodes (35): AgentRunner, DeepResearcherComplete, DeepResearcherNeedsMore, Exception, MarketRow, Raised when an agent payload fails Pydantic validation.      The Orchestrator mu, Raised when an agent payload fails Pydantic validation.      The Orchestrator mu, VaultWriteError (+27 more)

### Community 1 - "Community 1"
Cohesion: 0.25
Nodes (8): coerce_deep_researcher_markdown(), Normalize a Deep Researcher response into a markdown string.      Live OpenClaw, Normalize a Deep Researcher response into a markdown string.      Live OpenClaw, Normalize a Deep Researcher response into a markdown string.      Live OpenClaw, Validate the agent response against the local ``output_schema``.      Empty resp, Validate the agent response against the local ``output_schema``.      Empty resp, Validate the agent response against the local ``output_schema``.      Empty resp, _validate_response()

### Community 2 - "Community 2"
Cohesion: 0.10
Nodes (40): datetime, _build_latest_snapshot(), _check_arbitrage_hard_veto(), _compute_info_drift_metrics(), compute_signal_bundle_from_series(), _compute_signals_from_series(), _days_since_creation(), _empty_output() (+32 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (67): fetch_market_row(), fetch_resolution(), fetch_target_markets(), get_market_trends(), _ingest_limit(), _market_row_from_scraper(), MarketRow, _poly_scan_bin() (+59 more)

### Community 4 - "Community 4"
Cohesion: 0.10
Nodes (29): extract_fenced_block(), _find_embedded_json_object(), _legacy_markdown_to_complete(), _parse_body_candidates(), preprocess_agent_text(), Any, int, str (+21 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (50): ArgumentParser, build_phase3_queue(), Scan ``01_Filters/`` for initial research or ``pending_edge_refresh`` work., Scan ``01_Filters/`` for initial research or ``pending_edge_refresh`` work., Scan ``01_Filters/`` for initial research or ``pending_edge_refresh`` work., _build_parser(), main(), int (+42 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (60): main(), OpenClaw Orchestrator entry point.  Lifecycle: Orchestrator builds JSON input →, auto_replay(), max_edge_research_refreshes(), openclaw_agent_max_attempts(), openclaw_agent_retry_backoff(), openclaw_agent_timeout(), openclaw_bin() (+52 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (23): parse_deep_researcher(), parse_deep_researcher_frontmatter(), _parse_estimated_p(), parse_estimated_p_from_deep_researcher_frontmatter(), ParsedResearch, Any, float, str (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.14
Nodes (15): _has_usable_research_data(), _merge_research_bundle_queries(), Any, bool, ObsidianManager — Pydantic-gated file system layer for the Obsidian Vault.  The, Append ``new_entries``, skipping duplicate ``query`` strings., Replace a prior bundle row when a refetch succeeds after failure., Append ``new_entries``; refresh duplicate ``query`` rows when the new fetch is b (+7 more)

### Community 9 - "Community 9"
Cohesion: 0.12
Nodes (4): _patch_openclaw_run(), test_run_agent_uses_session_id_without_agent_on_legacy_cli(), test_run_agent_uses_session_key_when_cli_supports_it(), str

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (16): Path, Write raw content to any path inside the Vault.          Parent directories are, Write raw content to any path inside the Vault.          Parent directories are, Write raw content to any path inside the Vault.          Parent directories are, Validate a Trade Executioner payload and write to ``03_Trades/``.          The f, Validate a Trade Executioner payload and write to ``03_Trades/``.          The f, Validate a Post-Mortem Analyst payload and append it to the report.          The, Validate a Trade Executioner payload and write to ``03_Trades/``.          The f (+8 more)

### Community 11 - "Community 11"
Cohesion: 0.16
Nodes (11): ObsidianManager, Single point of truth for all Vault I/O.      Parameters     ----------     vaul, Single point of truth for all Vault I/O.      Parameters     ----------     vaul, True when the market artifact exists and frontmatter has ``status: inactive``., Validate an Overseer payload and overwrite ``active_directives.md``.          Th, Validate an Overseer payload and overwrite ``active_directives.md``.          Th, Validate an Overseer payload and overwrite ``active_directives.md``.          Th, Read parsed frontmatter or JSON root for a market artifact. (+3 more)

### Community 12 - "Community 12"
Cohesion: 0.10
Nodes (21): Configuration constants for the Polymarket Trading Pipeline., Orchestrator-level configuration: env flags + re-exports of trading constants., agent_error_reason(), parse_agent_json_or_yaml(), Return the error string from an agent payload, if any.      Treats missing key,, Return the error string from an agent payload, if any.      Treats missing key,, Return the error string from an agent payload, if any.      Treats missing key,, Parse an agent response into a dict.      Accepts an already-parsed mapping (pas (+13 more)

### Community 13 - "Community 13"
Cohesion: 0.18
Nodes (12): _can_import_skill_module(), CLI regression tests for execute-aiq-query/run.py., Prefer OpenClaw gateway layout; fall back to repo config for local CI., run.py must import config from hardcoded OpenClaw root without PYTHONPATH., Smoke: CLI prints ExecuteAiqQueryOutput JSON when A-IQ is mocked., _run_py_env(), test_run_py_imports_without_pythonpath(), test_run_py_invalid_json_exits_1() (+4 more)

### Community 14 - "Community 14"
Cohesion: 0.25
Nodes (9): extract_filter_directives(), Any, str, Parse filter threshold directives from active_directives.md., Return filter thresholds from the Filter Weightings YAML block.      Falls back, Filter directives extraction from active_directives.md., test_extract_filter_directives_custom_yaml(), test_extract_filter_directives_fallback_on_empty() (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.18
Nodes (9): _build_seed_directives(), Move a market's file between Vault directories.          Resolves the source by, Move a market's file between Vault directories.          Resolves the source by, Write a seed ``active_directives.md`` if the file is missing or empty., Move a market's file between Vault directories.          Resolves the source by, Write a seed ``active_directives.md`` if the file is missing or empty., Return the full text of a seed ``active_directives.md``.      Embeds live values, Write a seed ``active_directives.md`` if the file is missing or empty. (+1 more)

### Community 16 - "Community 16"
Cohesion: 0.24
Nodes (9): Path, str, Vault workspace path resolution (env + defaults)., Resolve vault workspace root: explicit arg, then env, then project default., resolve_vault_base(), OPENCLAW_VAULT_PATH overrides the default Obsidian vault workspace root., test_explicit_vault_base_overrides_env(), test_obsidian_manager_uses_env_when_vault_base_omitted() (+1 more)

### Community 17 - "Community 17"
Cohesion: 0.05
Nodes (38): calculate_trade_allocation(), CalculateTradeAllocationInput, CalculateTradeAllocationOutput, calculate_trade_allocation — OpenClaw skill execution module.  Contract: docs/04, ``below_edge_threshold`` is ``True`` iff ``S <= S_0`` (edge gate); ``None`` on t, EvaluateMarketMetricsInput, execute_aiq_query(), ExecuteAiqQueryInput (+30 more)

### Community 18 - "Community 18"
Cohesion: 0.23
Nodes (10): DirectivesPayload, Overseer output — used to overwrite 00_System/active_directives.md.      The ``n, Overseer output — used to overwrite 00_System/active_directives.md.      The ``n, F3 regression: Overseer output is structurally validated before overwriting dire, test_directives_payload_accepts_valid_doc(), test_directives_payload_rejects_missing_frontmatter(), test_directives_payload_rejects_missing_required_header(), test_phase6_quarantines_malformed_overseer_output() (+2 more)

### Community 19 - "Community 19"
Cohesion: 0.25
Nodes (9): F8 regression: trade JSON is archived after a successful post-mortem., _runner(), _seed_resolved_market(), test_post_mortem_success_archives_trade(), test_subsequent_tick_does_not_re_resolve_archived_trade(), vault(), Any, ObsidianManager (+1 more)

### Community 20 - "Community 20"
Cohesion: 0.20
Nodes (9): 1. Data Ingestion, 2. Quantitative Routing, 3. Qualitative Pipeline (Decoupled), 4. Execution, 5. Market Resolution & Post-Mortem (Async/Scheduled), 6. Macro-Learning Loop (Every 24-48 Hours), 7. Error Handling (In-Place State Machine), Orchestrator Pipeline (Strict State Flow) (+1 more)

### Community 21 - "Community 21"
Cohesion: 0.20
Nodes (10): latest_snapshot, datetime, days_since_creation, liquidity, midpoint, no_price, spread, total_volume (+2 more)

### Community 22 - "Community 22"
Cohesion: 0.15
Nodes (12): Analytical Mission (Guarding the Sunk Cost), EXECUTION FLOW, OUTPUT SCHEMA, OUTPUT SCHEMA (Critical Infrastructure), Re-Evaluator — operating instructions, `review_kind: "edge_research_refresh"`, `review_kind: "edge_research_refresh"` (The Revival), `review_kind: "quantitative"` (+4 more)

### Community 23 - "Community 23"
Cohesion: 0.11
Nodes (25): Per-role OpenClaw-style agent workspaces (see ``agents/<role>/``)., load_agents_from_dir(), Any, Path, str, Discover per-role agent workspaces under ``agents/<role>/`` and build ``AGENTS``, Load all agent workspaces; return orchestrator ``AGENTS`` dict keyed by role., Load all agent workspaces; return orchestrator ``AGENTS`` dict keyed by role. (+17 more)

### Community 24 - "Community 24"
Cohesion: 0.25
Nodes (7): Step 1: Expected Return (After-Tax & Fees), Step 2: Time Adjustment, Step 3: Rarity Bonus, Step 4: Execution Penalty, Step 5: Final Score, Step 6: Bankroll Allocation, Trade Execution Math Protocol

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (8): 1. The Evaluator & Re-Evaluator, 2. The Context Briefer, 2. The Query Planner (briefer), 3. The Deep Researcher, 4. The Trade Executioner, 5. The Post-Mortem Analyst, 6. The Overseer (Strategy Optimizer), Agent Personas and I/O Schemas

### Community 26 - "Community 26"
Cohesion: 0.25
Nodes (7): error, hard_veto, details, passed, trigger, market_id, thresholds_applied

### Community 27 - "Community 27"
Cohesion: 0.15
Nodes (21): Spawn the agent registered under ``role`` and return its raw response.      Rais, Spawn the agent registered under ``role`` and return its raw response.      Rais, Spawn the agent registered under ``role`` and return its raw response.      Rais, spawn_agent(), Hub merge of signal_bundle onto slim quantitative agent output., test_run_structured_agent_attaches_bundle_after_stub_spawn(), test_run_structured_agent_attaches_bundle_for_re_evaluator(), test_spawn_agent_returns_slim_evaluator_without_signal_bundle() (+13 more)

### Community 28 - "Community 28"
Cohesion: 0.16
Nodes (16): coerce_deep_researcher_state_machine(), normalize_structured_output(), Backfill known output fields from the orchestrator payload when missing., Backfill known output fields from the orchestrator payload when missing., Backfill known output fields from the orchestrator payload when missing., Normalize Deep Researcher output to the JSON state-machine shape.      Accepts l, Regression tests for agent response parsing and normalization., OpenClaw sometimes returns the JSON object as a JSON-encoded string. (+8 more)

### Community 29 - "Community 29"
Cohesion: 0.14
Nodes (29): _context_summary_from_active(), _edge_refresh_research(), _fetch_and_persist_bundle(), _finalize_research(), _forced_synthesis(), _plan_research_queries(), _planning_context_from_active(), ObsidianManager (+21 more)

### Community 30 - "Community 30"
Cohesion: 0.33
Nodes (5): 1. Core Infrastructure, 2. File System Memory & The Pydantic Gatekeeper, 3. Obsidian Vault Directory Schemas, 4. The Cold Start Protocol, Polymarket Hub-and-Spoke Trading Architecture

### Community 31 - "Community 31"
Cohesion: 0.33
Nodes (5): 1. Skill: evaluate_market_metrics, 2. Skill: calculate_trade_allocation, 3. Skill: execute_polymarket_trade, 4. Skill: execute_aiq_query, OpenClaw Skill Contracts

### Community 32 - "Community 32"
Cohesion: 0.33
Nodes (6): end_price, pct_move, start_price, threshold, window_hrs, breakout

### Community 33 - "Community 33"
Cohesion: 0.33
Nodes (6): direction, max_run, net_pct_change, proxy, threshold, info_drift

### Community 34 - "Community 34"
Cohesion: 0.33
Nodes (6): signals, volume_shock, baseline_median, current, ratio, threshold

### Community 35 - "Community 35"
Cohesion: 0.29
Nodes (7): Analytical Mission (Hunting the Edge), Context Briefer — operating instructions, EXECUTION FLOW, Query Engineering Rules, Query Planner (briefer) — operating instructions, Turn 1 — Data gathering, Turn 2 — Summary

### Community 36 - "Community 36"
Cohesion: 0.25
Nodes (7): Analytical Mission (The First Filter), Evaluator — operating instructions, EXECUTION FLOW, OPERATIONAL RULES, OUTPUT FORMAT (Critical Infrastructure), Turn 1 — Data Gathering, Turn 2 — Evaluation

### Community 37 - "Community 37"
Cohesion: 0.40
Nodes (5): data_quality, newest, oldest, snapshots_used, start_date_available

### Community 38 - "Community 38"
Cohesion: 0.40
Nodes (5): fired, liquidity, pct_move, threshold, low_liquidity_breakout

### Community 39 - "Community 39"
Cohesion: 0.40
Nodes (5): spread_anomaly, baseline_median, current, ratio, threshold

### Community 40 - "Community 40"
Cohesion: 0.13
Nodes (12): str, ValidationError, Create all vault subdirectories if they do not already exist., Create all vault subdirectories if they do not already exist., Create all vault subdirectories if they do not already exist., Return the ``queries`` list from a saved bundle, or ``None`` if missing., Read the open trade log JSON for ``market_id`` as a raw string., Write or merge A-IQ results under ``research_bundles/{market_id}.json``. (+4 more)

### Community 41 - "Community 41"
Cohesion: 0.22
Nodes (7): _archive_timestamp(), Return a filesystem-safe UTC timestamp suffix for archived trade filenames., Return a filesystem-safe UTC timestamp suffix for archived trade filenames., Directory for Hub-persisted A-IQ query results (``research_bundles/``)., Directory for Hub-persisted A-IQ query results (``research_bundles/``)., Move ``03_Trades/{market_id}.json`` to ``03_Trades/_resolved/``.          Called, Move ``03_Trades/{market_id}.json`` to ``03_Trades/_resolved/``.          Called

### Community 42 - "Community 42"
Cohesion: 0.25
Nodes (6): Return the path where active research for ``market_id`` lives., Return full markdown for ``02_Active_Research/{market_id}.md`` if present., Return the path where active research for ``market_id`` lives., Return full markdown for ``02_Active_Research/{market_id}.md`` if present., Return the path where active research for ``market_id`` lives., Return full markdown for ``02_Active_Research/{market_id}.md`` if present.

### Community 43 - "Community 43"
Cohesion: 0.10
Nodes (16): _ensure_repo_on_path(), fetch_research_bundle(), _load_execute_aiq_query(), Any, str, Hub-side parallel A-IQ fetch for Phase 3 research bundles.  The orchestrator cal, Clear the cached skill callable (for tests)., Load ``execute_aiq_query`` from the skill module (cached after first load). (+8 more)

### Community 44 - "Community 44"
Cohesion: 0.50
Nodes (3): CLI regression tests for evaluate-market-metrics/run.py., run.py must import config from hardcoded OpenClaw root without PYTHONPATH., test_run_py_imports_without_pythonpath()

### Community 52 - "Community 52"
Cohesion: 0.33
Nodes (5): Execution Mission (Weaponizing the Edge), OPERATIONAL RULES, OUTPUT SCHEMA (Critical Infrastructure), RISK PARAMETER MAPPING (Apply Exactly; Do Not Improvise), Trade Executioner — operating instructions

### Community 54 - "Community 54"
Cohesion: 0.29
Nodes (6): Analytical Mission (Evolution & Error Eradication), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Overseer — operating instructions, Overseer (Strategy Optimizer) — operating instructions

### Community 56 - "Community 56"
Cohesion: 0.33
Nodes (5): Analytical Mission (The Autopsy), INPUT MAPPING, OPERATIONAL RULES, OUTPUT SCHEMA, Post-Mortem Analyst — operating instructions

### Community 64 - "Community 64"
Cohesion: 0.09
Nodes (18): _dump_frontmatter(), Serialise ``data`` as YAML frontmatter followed by ``body``.      Produces the c, Serialise ``data`` as YAML frontmatter followed by ``body``.      Produces the c, Validate an Evaluator payload and write it to ``01_Filters/``.          The file, Validate an Evaluator payload and write it to ``01_Filters/``.          The file, Validate an Evaluator payload and write it to ``01_Filters/``.          The file, Validate a Deep Researcher payload and write to ``02_Active_Research/``., Validate a Deep Researcher payload and write to ``02_Active_Research/``. (+10 more)

### Community 66 - "Community 66"
Cohesion: 0.25
Nodes (6): Return the path where the post-mortem report for ``market_id`` lives., Read the post-mortem markdown for ``market_id``., Return the path where the post-mortem report for ``market_id`` lives., Read the post-mortem markdown for ``market_id``., Return the path where the post-mortem report for ``market_id`` lives., Read the post-mortem markdown for ``market_id``.

### Community 67 - "Community 67"
Cohesion: 0.10
Nodes (23): OpenClaw orchestrator package — Hub-and-Spoke Polymarket trading pipeline.  The, Top-level scheduling loop., Run a single phase 1 → 5 sweep over the scraper queue., run_pipeline_tick(), phase1_data_ingestion(), phase3_qualitative_pipeline(), phase4_execution(), phase5_resolution_and_post_mortem() (+15 more)

### Community 68 - "Community 68"
Cohesion: 0.18
Nodes (20): _bundle_has_entries(), _open_trade_shows_bet_not_edge_dq(), _persist_phase2_filter(), phase2_quantitative_routing(), Any, bool, str, True when an open trade log reflects a non-zero allocation path (bet placed). (+12 more)

### Community 69 - "Community 69"
Cohesion: 0.22
Nodes (18): _briefer_stub(), _deep_researcher_stub(), _evaluator_stub(), _executioner_stub(), _overseer_stub(), _post_mortem_stub(), Any, str (+10 more)

### Community 70 - "Community 70"
Cohesion: 0.20
Nodes (14): Shared orchestrator test fixtures., Avoid live DB access when phase tests merge quantitative ``signal_bundle``., _stub_evaluator_signal_bundle_fetch(), attach_signal_bundle(), fetch_signal_bundle(), _load_evaluate_market_metrics(), Any, str (+6 more)

### Community 71 - "Community 71"
Cohesion: 0.15
Nodes (12): build_live_response_hint(), Any, str, Schema-driven live prompts for OpenClaw agent invocations., Build response instructions from ``agent.yaml`` ``output_schema``., _build_live_prompt(), Serialize the orchestrator payload for the target OpenClaw workspace., Serialize the orchestrator payload for the target OpenClaw workspace. (+4 more)

### Community 72 - "Community 72"
Cohesion: 0.25
Nodes (8): phase6_macro_learning_loop(), Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``., Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``., Spawn a JSON/YAML agent. Returns ``(parsed, error_reason)``., Aggregate post-mortems → Overseer → overwrite ``active_directives.md``., Aggregate post-mortems → Overseer → overwrite ``active_directives.md``., Aggregate post-mortems → Overseer → overwrite ``active_directives.md``., _run_structured_agent()

### Community 73 - "Community 73"
Cohesion: 0.29
Nodes (7): DeepResearcherOutput, parse_deep_researcher_json(), Any, Validate a Deep Researcher state-machine payload by ``status``., Validate a Deep Researcher state-machine payload by ``status``., test_stub_deep_researcher_returns_parseable_complete_payload(), test_stub_error_deep_researcher_carries_error_in_frontmatter()

### Community 74 - "Community 74"
Cohesion: 0.40
Nodes (5): _live_session_key(), Scope OpenClaw transcript state to one market per agent., Scope OpenClaw transcript state to one market per agent., Scope OpenClaw transcript state to one market per agent., test_live_session_key_overseer_is_isolated()

### Community 75 - "Community 75"
Cohesion: 0.50
Nodes (3): Return sorted files under a vault directory matching ``suffix``., Return sorted files under a vault directory matching ``suffix``., Return sorted files under a vault directory matching ``suffix``.

### Community 76 - "Community 76"
Cohesion: 0.50
Nodes (3): Merge ``updates`` into a JSON trade log root object., Merge ``updates`` into a JSON trade log root object., Merge ``updates`` into a JSON trade log root object.

### Community 77 - "Community 77"
Cohesion: 0.50
Nodes (3): Return the raw contents of ``active_directives.md``.          Returns         --, Return the raw contents of ``active_directives.md``.          Returns         --, Return the raw contents of ``active_directives.md``.          Returns         --

### Community 78 - "Community 78"
Cohesion: 0.50
Nodes (3): Read any file inside the Vault by path relative to ``vault_base``.          Para, Read any file inside the Vault by path relative to ``vault_base``.          Para, Read any file inside the Vault by path relative to ``vault_base``.          Para

## Knowledge Gaps
- **145 isolated node(s):** `Path`, `str`, `Any`, `str`, `Path` (+140 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ObsidianManager` connect `Community 11` to `Community 0`, `Community 3`, `Community 5`, `Community 6`, `Community 8`, `Community 10`, `Community 14`, `Community 15`, `Community 16`, `Community 18`, `Community 19`, `Community 29`, `Community 40`, `Community 41`, `Community 42`, `Community 64`, `Community 65`, `Community 66`, `Community 67`, `Community 68`, `Community 75`, `Community 76`, `Community 77`, `Community 78`?**
  _High betweenness centrality (0.204) - this node is a cross-community bridge._
- **Why does `parse_agent_json_or_yaml()` connect `Community 12` to `Community 0`, `Community 1`, `Community 4`, `Community 69`, `Community 6`, `Community 72`, `Community 23`, `Community 28`, `Community 29`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._
- **Why does `MarketRow` connect `Community 3` to `Community 0`, `Community 68`, `Community 5`, `Community 17`, `Community 29`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._
- **Are the 25 inferred relationships involving `ObsidianManager` (e.g. with `AgentRunner` and `ArgumentParser`) actually correct?**
  _`ObsidianManager` has 25 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `MarketRow` (e.g. with `AgentRunner` and `DeepResearcherComplete`) actually correct?**
  _`MarketRow` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `AgentOutputParseError` (e.g. with `AgentRunner` and `DeepResearcherComplete`) actually correct?**
  _`AgentOutputParseError` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `str` (e.g. with `ObsidianManager` and `VaultWriteError`) actually correct?**
  _`str` has 9 INFERRED edges - model-reasoned connections that need verification._