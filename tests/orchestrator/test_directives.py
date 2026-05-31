"""Filter directives extraction from active_directives.md."""

from __future__ import annotations

from config.trading_constants import FILTERS
from obsidian_utils import ObsidianManager
from orchestrator.directives import extract_filter_directives


def test_extract_filter_directives_from_seed(tmp_path):
    vault = ObsidianManager(vault_base=tmp_path)
    vault.cold_start_protocol()
    directives = vault.read_directives()
    parsed = extract_filter_directives(directives)
    assert parsed["breakout_pct_shift"] == FILTERS["breakout_pct_shift"]
    assert parsed["volume_shock_ma_multiplier"] == FILTERS["volume_shock_ma_multiplier"]


def test_extract_filter_directives_fallback_on_empty():
    parsed = extract_filter_directives("")
    assert parsed == FILTERS


def test_extract_filter_directives_custom_yaml():
    md = """---
version: "0.2"
---

## Filter Weightings

```yaml
filters:
  breakout_pct_shift: 0.15
  volume_shock_ma_multiplier: 4.0
```

## Risk Constraints

x
"""
    parsed = extract_filter_directives(md)
    assert parsed["breakout_pct_shift"] == 0.15
    assert parsed["volume_shock_ma_multiplier"] == 4.0
    assert parsed["arbitrage_max_combined_ask"] == FILTERS["arbitrage_max_combined_ask"]
