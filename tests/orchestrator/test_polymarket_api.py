"""Polymarket public-API client: candidates, row mapping, CLOB quotes, resolution, retries.

HTTP is faked with a session double serving trimmed real responses from
``tests/orchestrator/fixtures/polymarket/`` (captured read-only from the live Gamma/CLOB APIs).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

from orchestrator import polymarket_api as api
from orchestrator import scraper

FIX = Path(__file__).parent / "fixtures" / "polymarket"
CID = "0xd5d9fc47718bd553592d126b1fa5e87183d27f3936975b0c04cc0f2dec1f1bb4"
YES_TOKEN = "83247781037352156539108067944461291821683755894607244160607042790356561625563"
NO_TOKEN = "33156410999665902694791064431724433042010245771106314074312009703157423879038"
NOW = datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc)

# The module-level fakes in conftest replace executable_quote for phase tests; keep the real one.
real_executable_quote = api.executable_quote
real_fetch_holder_concentration = api.fetch_holder_concentration


@pytest.fixture(autouse=True)
def _real_holders(monkeypatch):
    """conftest stubs holder fetching; this module tests the real one over a fake session."""
    monkeypatch.setattr(api, "fetch_holder_concentration", real_fetch_holder_concentration)


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is ValueError:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    """Routes ``get(url, params)`` to a handler; records every call."""

    def __init__(self, handler: Callable[[str, dict[str, Any]], Any]):
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):
        self.calls.append((url, dict(params or {})))
        out = self.handler(url, dict(params or {}))
        return out if isinstance(out, FakeResponse) else FakeResponse(200, out)


@pytest.fixture(autouse=True)
def _http(monkeypatch):
    monkeypatch.setattr(api, "_sleep", lambda s: None)
    monkeypatch.delenv("GAMMA_BASE_URL", raising=False)
    monkeypatch.delenv("CLOB_BASE_URL", raising=False)
    api.set_session(None)
    yield
    api.set_session(None)


def install(handler) -> FakeSession:
    sess = FakeSession(handler)
    api.set_session(sess)
    return sess


def gamma_market(**over) -> dict[str, Any]:
    m = copy.deepcopy(load("market_binary_open.json"))
    m.update(over)
    return m


# --- field mapping ---------------------------------------------------------


def test_fetch_market_maps_string_encoded_fields():
    sess = install(lambda url, p: [gamma_market()])
    row = api.fetch_market(CID)
    assert sess.calls[0][1]["condition_ids"] == CID
    assert row["market_id"] == CID and row["gamma_id"] == "562802"
    assert row["yes_token_id"] == YES_TOKEN and row["no_token_id"] == NO_TOKEN
    assert (row["yes_bid"], row["yes_ask"]) == (0.92, 0.93)
    assert row["yes_price"] == pytest.approx(0.925)  # mid of bid/ask
    assert row["no_bid"] == pytest.approx(0.07) and row["no_ask"] == pytest.approx(0.08)
    assert row["spread"] == pytest.approx(0.01)
    assert row["liquidity"] == pytest.approx(4109869.9095) and row["volume"] > 1e7
    assert row["event_id"] == "32225"
    assert row["end_date"].startswith("2026-11-04T04:59:00") and row["days_to_resolution"] >= 1
    assert row["question"] == row["title"] and row["description"]


def test_yes_price_falls_back_to_outcome_prices_without_a_book():
    m = gamma_market(outcomePrices='["0.30", "0.70"]')
    m.pop("bestBid"), m.pop("bestAsk")
    row = api.market_row_from_gamma(m)
    assert row["yes_price"] == 0.30 and row["no_price"] == 0.70 and "yes_ask" not in row


def test_outcome_order_no_first_maps_tokens_and_prices_by_label():
    m = gamma_market(
        outcomes='["No", "Yes"]',
        outcomePrices='["0.075", "0.925"]',
        clobTokenIds=json.dumps([NO_TOKEN, YES_TOKEN]),
    )
    m.pop("bestBid"), m.pop("bestAsk")
    row = api.market_row_from_gamma(m)
    assert row["yes_token_id"] == YES_TOKEN and row["no_token_id"] == NO_TOKEN
    assert row["yes_price"] == 0.925


def test_lists_may_arrive_as_real_lists():
    m = gamma_market(outcomes=["Yes", "No"], clobTokenIds=[YES_TOKEN, NO_TOKEN], outcomePrices=["0.4", "0.6"])
    assert api.market_row_from_gamma(m)["yes_token_id"] == YES_TOKEN


@pytest.mark.parametrize("outcomes", ['["Up", "Down"]', '["Yes", "No", "Maybe"]', '["Over", "Under"]', ""])
def test_non_yes_no_markets_are_skipped(outcomes):
    assert api.market_row_from_gamma(gamma_market(outcomes=outcomes)) is None
    sess = install(lambda url, p: [gamma_market(outcomes=outcomes)])
    assert api.fetch_market(CID) is None and sess.calls


def test_fetch_market_falls_back_to_closed_lookup_and_numeric_id():
    closed = load("market_closed_resolved.json")

    def handler(url, p):
        if url.endswith("/markets") and p.get("closed") == "true":
            return [closed]
        return []

    sess = install(handler)
    row = api.fetch_market(closed["conditionId"])
    assert row["closed"] is True and len(sess.calls) == 2

    sess = install(lambda url, p: closed)
    assert api.fetch_market("5950578")["market_id"] == closed["conditionId"]
    assert sess.calls[0][0].endswith("/markets/5950578")
    assert api.fetch_market("not an id!") is None


# --- candidates -------------------------------------------------------------


def _candidates_handler(pages: list[dict[str, Any]]):
    it = iter(pages)
    return lambda url, p: next(it)


def test_list_candidates_filters_window_liquidity_and_non_binary_and_pages():
    far = gamma_market(conditionId="0xfar", endDate="2027-06-01T00:00:00Z", endDateIso="2027-06-01")
    soon = gamma_market(conditionId="0xsoon", endDate="2026-10-24T00:00:00Z", endDateIso="2026-10-24")
    thin = gamma_market(conditionId="0xthin", liquidityNum=50.0, liquidity="50")
    updown = gamma_market(conditionId="0xud", outcomes='["Up", "Down"]')
    noorder = gamma_market(conditionId="0xno", acceptingOrders=False)
    good2 = gamma_market(conditionId="0xgood2", endDate="2026-12-01T00:00:00Z", endDateIso="2026-12-01")
    sess = install(
        _candidates_handler(
            [
                {"markets": [far, soon, thin, updown, noorder, gamma_market()], "next_cursor": "c1"},
                {"markets": [good2], "next_cursor": None},
            ]
        )
    )
    rows = api.list_candidates(10, now=NOW)
    assert [r["market_id"] for r in rows] == [CID, "0xgood2"]
    first, second = sess.calls
    assert first[0].endswith("/markets/keyset") and "after_cursor" not in first[1]
    assert second[1]["after_cursor"] == "c1"
    p = first[1]
    assert (p["active"], p["closed"], p["order"], p["ascending"]) == ("true", "false", "liquidityNum", "false")
    assert p["liquidity_num_min"] == api.MIN_LIQUIDITY_USD == 100.0
    assert p["end_date_min"] == "2026-10-29T13:00:00Z" and p["end_date_max"] == "2026-12-21T13:00:00Z"


def test_list_candidates_respects_limit_and_directive_knobs():
    pages = [{"markets": [gamma_market(conditionId=f"0x{i}") for i in range(5)], "next_cursor": "more"}]
    sess = install(_candidates_handler(pages))
    rows = api.list_candidates(2, {"min_days_to_resolution": 3, "max_days_to_resolution": 90}, now=NOW)
    assert len(rows) == 2 and len(sess.calls) == 1
    assert sess.calls[0][1]["end_date_min"] == "2026-10-25T13:00:00Z"
    assert sess.calls[0][1]["end_date_max"] == "2027-01-20T13:00:00Z"


def test_scraper_fetch_target_markets_wraps_rows_and_swallows_api_errors(monkeypatch):
    real_list = api.list_candidates  # fixture end dates are fixed: pin "now" so the window is stable
    monkeypatch.setattr(api, "list_candidates", lambda limit, knobs=None, **kw: real_list(limit, knobs, now=NOW))
    install(lambda url, p: load("keyset_page.json"))
    rows = scraper.fetch_target_markets()
    assert [r.market_id for r in rows] == [CID]
    assert rows[0].market_data["yes_ask"] == 0.93 and rows[0].market_data["yes_token_id"] == YES_TOKEN
    assert scraper.select_q(rows[0].market_data) == pytest.approx(0.925)

    install(lambda url, p: FakeResponse(400, {}))
    assert scraper.fetch_target_markets() == []
    assert scraper.fetch_market_row("../etc") is None


# --- executable quote -------------------------------------------------------


def _book_handler(book: dict[str, Any]):
    def handler(url, p):
        if url.endswith("/book"):
            return book
        return [gamma_market()]

    return handler


def test_executable_quote_sorts_asks_and_sums_depth_within_2c():
    sess = install(_book_handler(load("clob_book_yes.json")))
    q = real_executable_quote(CID, "YES")
    # real book: asks 0.99..0.93 (listed worst-first), bids up to 0.92
    assert q["ask"] == 0.93 and q["token_id"] == YES_TOKEN
    assert q["spread"] == pytest.approx(0.01)
    book = load("clob_book_yes.json")
    expect = sum(float(a["price"]) * float(a["size"]) for a in book["asks"] if float(a["price"]) <= 0.95 + 1e-9)
    assert q["depth_usd_within_2c"] == pytest.approx(expect, rel=1e-6) and expect > 0
    assert sess.calls[-1][1] == {"token_id": YES_TOKEN}


def test_executable_quote_depth_math_and_no_side_token():
    book = {
        "bids": [{"price": "0.38", "size": "100"}],
        "asks": [
            {"price": "0.50", "size": "100"},
            {"price": "0.41", "size": "200"},  # unsorted on purpose
            {"price": "0.43", "size": "100"},
            {"price": "0.44", "size": "1000"},  # > best_ask + 0.02
        ],
    }
    sess = install(_book_handler(book))
    q = real_executable_quote(CID, "NO")
    assert q["ask"] == 0.41 and q["token_id"] == NO_TOKEN
    assert q["depth_usd_within_2c"] == pytest.approx(0.41 * 200 + 0.43 * 100)
    assert q["spread"] == pytest.approx(0.03)
    assert sess.calls[-1][1] == {"token_id": NO_TOKEN}


def test_executable_quote_none_without_asks_and_never_cached():
    sess = install(_book_handler({"bids": [{"price": "0.4", "size": "1"}], "asks": []}))
    assert real_executable_quote(CID, "YES") is None
    real_executable_quote(CID, "YES")
    assert sum(1 for url, _ in sess.calls if url.endswith("/book")) == 2
    with pytest.raises(ValueError):
        real_executable_quote(CID, "MAYBE")


# --- resolution -------------------------------------------------------------


@pytest.mark.parametrize(
    ("closed", "prices", "uma", "want_yes", "want_status"),
    [
        (True, '["1", "0"]', "resolved", 1, "resolved"),
        (True, '["0", "1"]', "resolved", 0, "resolved"),
        (True, '["0.5", "0.5"]', "resolved", None, "void"),
        (True, '["0.0015", "0.9985"]', "disputed", None, "disputed"),
        (False, '["0.6", "0.4"]', None, "UNRESOLVED", None),
    ],
)
def test_resolution_mapping(closed, prices, uma, want_yes, want_status):
    m = load("market_closed_resolved.json") | {"closed": closed, "outcomePrices": prices, "umaResolutionStatus": uma}
    install(lambda url, p: [m])
    res = api.resolution(m["conditionId"])
    if want_yes == "UNRESOLVED":
        assert res is None
        return
    assert res["outcome_yes"] == want_yes and res["status"] == want_status
    assert res["resolved_at"].startswith("2026-10-20T00:11:51")


def test_resolution_maps_by_label_when_no_comes_first():
    m = load("market_closed_resolved.json") | {"outcomes": '["No", "Yes"]', "outcomePrices": '["1", "0"]'}
    install(lambda url, p: [m])
    assert api.resolution(m["conditionId"])["outcome_yes"] == 0


def test_scraper_fetch_resolution_shapes_and_waits_on_disputes():
    m = load("market_closed_resolved.json")
    install(lambda url, p: [m])
    res = scraper.fetch_resolution(m["conditionId"])
    assert res["outcome"] == 1 and res["status"] == "resolved"
    from orchestrator import ledger

    assert ledger.outcome_yes_from_resolution(res) == 1

    disputed = m | {"umaResolutionStatus": "disputed"}
    install(lambda url, p: [disputed])
    assert scraper.fetch_resolution(m["conditionId"]) is None
    install(lambda url, p: [m | {"closed": False}])
    assert scraper.fetch_resolution(m["conditionId"]) is None


# --- transport ---------------------------------------------------------------


def test_retries_429_and_5xx_with_backoff_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(api, "_sleep", sleeps.append)
    seq = iter([FakeResponse(429, {}, {"Retry-After": "2"}), FakeResponse(503, {}), FakeResponse(200, [gamma_market()])])
    sess = install(lambda url, p: next(seq))
    assert api.fetch_market(CID)["market_id"] == CID
    assert len(sess.calls) == 3 and sleeps == [2.0, 1.0]


def test_retries_transport_errors_and_gives_up():
    def boom(url, p):
        raise requests.ConnectionError("down")

    sess = install(boom)
    with pytest.raises(api.PolymarketAPIError):
        api.fetch_market(CID)
    assert len(sess.calls) == api.MAX_ATTEMPTS


def test_non_retryable_4xx_fails_fast_and_responses_are_cached():
    sess = install(lambda url, p: FakeResponse(404, {}))
    with pytest.raises(api.PolymarketAPIError):
        api.fetch_market(CID)
    assert len(sess.calls) == 1

    sess = install(lambda url, p: [gamma_market()])
    api.fetch_market(CID), api.fetch_market(CID)
    assert len(sess.calls) == 1  # TTL cache


def test_base_urls_are_overridable(monkeypatch):
    monkeypatch.setenv("GAMMA_BASE_URL", "http://gamma.test/")
    monkeypatch.setenv("CLOB_BASE_URL", "http://clob.test")
    sess = install(_book_handler(load("clob_book_yes.json")))
    real_executable_quote(CID, "YES")
    assert sess.calls[0][0] == "http://gamma.test/markets"
    assert sess.calls[-1][0] == "http://clob.test/book"


def test_resolution_queries_closed_true_first_and_ignores_stale_open_record():
    closed = load("market_closed_resolved.json")
    stale = closed | {"closed": False, "umaResolutionStatus": "proposed", "outcomePrices": '["0.9995", "0.0005"]'}
    sess = install(lambda url, p: [closed] if p.get("closed") == "true" else [stale])
    res = api.resolution(closed["conditionId"])
    assert res is not None and res["status"] == "resolved"
    assert sess.calls[0][1].get("closed") == "true"


def test_resolution_falls_back_to_open_lookup_and_stale_proposed_is_unresolved():
    stale = load("market_closed_resolved.json") | {"closed": False, "umaResolutionStatus": "proposed"}
    sess = install(lambda url, p: [] if p.get("closed") == "true" else [stale])
    assert api.resolution(stale["conditionId"]) is None
    assert [c[1].get("closed") for c in sess.calls] == ["true", None]


def test_fetch_market_still_queries_open_first():
    sess = install(lambda url, p: [gamma_market()])
    api.fetch_market(CID)
    assert "closed" not in sess.calls[0][1]


def test_row_carries_volume_24h_and_day_price_change():
    row = api.market_row_from_gamma(load("market_binary_open.json"), NOW)
    assert row["volume_24h"] == pytest.approx(584939.550041)
    assert row["day_price_change"] == pytest.approx(0.01)
    m = copy.deepcopy(load("market_binary_open.json"))
    m.pop("volume24hr")
    m.pop("oneDayPriceChange")
    row = api.market_row_from_gamma(m, NOW)
    assert "volume_24h" not in row and "day_price_change" not in row


def _holders(amounts_by_side):
    return {"data": [{"token_id": str(i), "holders": [{"amount": a} for a in amts]} for i, amts in enumerate(amounts_by_side)]}


def test_fetch_holder_concentration_parses_worst_side(monkeypatch):
    seen = {}

    def fake(url, params=None, **kw):
        seen.update(url=url, params=params, kw=kw)
        return _holders([[50, 30, 10, 10], [10, 10, 10, 10, 10, 10, 10, 10, 10, 10]])

    monkeypatch.setattr(api, "_get_json", fake)
    out = api.fetch_holder_concentration(CID)
    assert out == {"top1": pytest.approx(0.5), "top3": pytest.approx(0.9)}
    assert seen["url"].endswith("/holders") and seen["params"]["condition_id"] == CID
    assert seen["kw"]["timeout"] == api.MANIP["holders_timeout_sec"]
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: _holders([[5, 5]])["data"])  # bare list
    assert api.fetch_holder_concentration(CID)["top1"] == pytest.approx(0.5)


def test_fetch_holder_concentration_failures_return_none(monkeypatch):
    def boom(*a, **k):
        raise api.PolymarketAPIError("down")

    monkeypatch.setattr(api, "_get_json", boom)
    assert api.fetch_holder_concentration(CID) is None
    for payload in ({"error": "x"}, "junk", [{"holders": "nope"}], _holders([[0, 0]]), _holders([])):
        monkeypatch.setattr(api, "_get_json", lambda *a, _p=payload, **k: _p)
        assert api.fetch_holder_concentration(CID) is None
