"""search_market_context — OpenClaw skill execution module.

Contract: docs/04_skills_contracts.md §2

Uses the Brave Search API to retrieve real-time news and context for a market query.
Requires the BRAVE_API_KEY environment variable.
"""
from __future__ import annotations

import os

import requests
from pydantic import BaseModel, ConfigDict

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_RESULT_COUNT = 5
_FRESHNESS = "pw"  # past week
_TIMEOUT_SEC = 15


class SearchMarketContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class SearchMarketContextOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    error: str | None


def search_market_context(
    query: str,
) -> SearchMarketContextOutput:
    try:
        api_key = os.environ.get("BRAVE_API_KEY", "")
        if not api_key:
            return SearchMarketContextOutput(
                summary="",
                error="BRAVE_API_KEY environment variable is not set.",
            )

        response = requests.get(
            _BRAVE_SEARCH_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            params={
                "q": query,
                "count": _RESULT_COUNT,
                "freshness": _FRESHNESS,
            },
            timeout=_TIMEOUT_SEC,
        )
        response.raise_for_status()

        results = response.json().get("web", {}).get("results", [])
        if not results:
            return SearchMarketContextOutput(
                summary="No results found.",
                error=None,
            )

        parts = [
            f"{r.get('title', '').strip()}: {r.get('description', '').strip()}"
            for r in results
            if r.get("title") or r.get("description")
        ]
        summary = " | ".join(parts) if parts else "No results found."

        return SearchMarketContextOutput(summary=summary, error=None)

    except Exception as exc:
        return SearchMarketContextOutput(summary="", error=str(exc))
