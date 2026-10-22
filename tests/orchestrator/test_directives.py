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
    assert parsed["gate_min_q"] == FILTERS["gate_min_q"]
    assert parsed["research_max_quick_width"] == FILTERS["research_max_quick_width"]


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
  gate_min_q: 0.07
  research_max_quick_width: 0.2
```

## Risk Constraints

x
"""
    parsed = extract_filter_directives(md)
    assert parsed["gate_min_q"] == 0.07
    assert parsed["research_max_quick_width"] == 0.2
    assert parsed["arbitrage_max_combined_ask"] == FILTERS["arbitrage_max_combined_ask"]
