"""Category playbooks: small directive modules keyed by market category (stub, none shipped).

A playbook is a short block of extra instructions (for example how to weigh a category's typical evidence) that the
architect and the judges receive as ``playbook`` in their payload when the market's category has one. The registry
is intentionally empty: add an entry only with evidence (ledger / Brier by category) that a category needs one.

    PLAYBOOKS["politics"] = "Short, price-blind directive text ..."

Keys are lowercase category names as in the market's ``category`` field. Playbooks must stay price-blind and
advisory: code still decides gating, sizing and skips.
"""

from __future__ import annotations

PLAYBOOKS: dict[str, str] = {}


def playbook_for(category: str | None) -> str | None:
    """The playbook text for ``category`` (case-insensitive), or ``None`` when there is none."""
    key = str(category or "").strip().lower()
    return PLAYBOOKS.get(key) or None


__all__ = ["PLAYBOOKS", "playbook_for"]
