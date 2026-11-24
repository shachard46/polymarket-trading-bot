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

from config.trading_constants import LIQ_FRAC, MIN_ORDER_USD
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


real_fetch_price_history = api.fetch_price_history
real_fetch_trades = api.fetch_trades
real_fetch_book = api.fetch_book


@pytest.fixture(autouse=True)
def _real_integrity_fetchers(monkeypatch):
    """conftest stubs the integrity fetchers; this module tests the real ones."""
    monkeypatch.setattr(api, "fetch_price_history", real_fetch_price_history)
    monkeypatch.setattr(api, "fetch_trades", real_fetch_trades)
    monkeypatch.setattr(api, "fetch_book", real_fetch_book)


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
    assert p["liquidity_num_min"] == api.MIN_LIQUIDITY_USD == MIN_ORDER_USD / LIQ_FRAC
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
    expect = sum(float(a["price"]) * float(a["size"]) for a in book["asks"] if float(a["price"]) <= q["ask"] + api.DEPTH_BAND + 1e-9)
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
        if url.endswith("/holders"):
            seen.update(url=url, params=params, kw=kw)
        return _holders([[50, 30, 10, 10], [10, 10, 10, 10, 10, 10, 10, 10, 10, 10]])

    monkeypatch.setattr(api, "_get_json", fake)
    out = api.fetch_holder_concentration(CID)  # /oi gets holders-shaped junk -> top_n fallback
    assert out == {"top1": pytest.approx(0.5), "top3": pytest.approx(0.9), "basis": "top_n"}
    assert seen["url"].endswith("/holders") and seen["params"]["condition_id"] == CID
    assert seen["kw"]["timeout"] == api.MANIP["holders_timeout_sec"]
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: _holders([[5, 5]])["data"])  # bare list
    assert api.fetch_holder_concentration(CID)["top1"] == pytest.approx(0.5)


def _routed(oi):
    def fake(url, params=None, **kw):
        if url.endswith("/oi"):
            if isinstance(oi, Exception):
                raise oi
            return oi
        return _holders([[50, 30, 10, 10], [20, 10]])

    return fake


def test_fetch_holder_concentration_supply_basis(monkeypatch):
    monkeypatch.setattr(api, "_get_json", _routed({"data": [{"condition_id": CID, "value": 1000.0}]}))
    out = api.fetch_holder_concentration(CID)
    assert out == {"top1": pytest.approx(0.05), "top3": pytest.approx(0.09), "basis": "supply"}


def test_fetch_holder_concentration_oi_unusable_falls_back(monkeypatch):
    for oi in (api.PolymarketAPIError("down"), {"data": []}, {"data": [{"value": 0}]}, {"data": [{"value": "x"}]},
               {"data": [{"value": 80.0}]}):  # 80 < top-N sum 100: inconsistent
        monkeypatch.setattr(api, "_get_json", _routed(oi))
        out = api.fetch_holder_concentration(CID)
        assert out["basis"] == "top_n" and out["top1"] == pytest.approx(2 / 3)


def test_fetch_holder_concentration_failures_return_none(monkeypatch):
    def boom(*a, **k):
        raise api.PolymarketAPIError("down")

    monkeypatch.setattr(api, "_get_json", boom)
    assert api.fetch_holder_concentration(CID) is None
    for payload in ({"error": "x"}, "junk", [{"holders": "nope"}], _holders([[0, 0]]), _holders([])):
        monkeypatch.setattr(api, "_get_json", lambda *a, _p=payload, **k: _p)
        assert api.fetch_holder_concentration(CID) is None


def test_keyset_real_page_signals_survive_list_candidates_and_scraper_snapshot():
    """Real trimmed /markets/keyset page: volume24hr/oneDayPriceChange present on one market, omitted on the other."""
    page = load("markets_keyset_page.json")
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    install(_candidates_handler([page]))
    rows = api.list_candidates(10, now=now)
    assert len(rows) == 2
    full, sparse = rows
    assert full["volume_24h"] > 0 and isinstance(full["day_price_change"], float)
    assert sparse["volume_24h"] > 0 and "day_price_change" not in sparse  # omitted by Gamma => absent, never 0
    snap = scraper._market_snapshot_from_scraper_row(full)
    assert snap["volume_24h"] == full["volume_24h"] and snap["day_price_change"] == full["day_price_change"]
    assert "day_price_change" not in scraper._market_snapshot_from_scraper_row(sparse)


# --- integrity fetchers ----------------------------------------------------

LIQ_CID = "0xfe28d93c0729d1e099a0359caaf1277e4e6560034c2e3900d65921515f467282"
LIQ_TOKEN = "30917853047681848974124763406524368436482316036542704956055886396093348090575"


def test_fetch_price_history_parses_pairs_and_is_uncached():
    sess = install(lambda url, p: load("prices_history.json"))
    out = api.fetch_price_history(LIQ_TOKEN)
    assert out and all(isinstance(t, int) and isinstance(p, float) for t, p in out)
    assert out[0] == (1794441622, 0.135)
    url, params = sess.calls[0]
    assert url.endswith("/prices-history")
    assert params == {"market": LIQ_TOKEN, "interval": "1m", "fidelity": 1440}
    api.fetch_price_history(LIQ_TOKEN)
    assert len(sess.calls) == 2  # cache=False: fresh each call


def test_fetch_trades_keeps_numeric_fields():
    sess = install(lambda url, p: load("data_trades.json"))
    out = api.fetch_trades(LIQ_CID)
    url, params = sess.calls[0]
    assert url.endswith("/v2/trades")
    assert params == {"condition": LIQ_CID, "limit": api.INTEGRITY["trades_limit"]}
    assert len(out) == 12
    t = out[0]
    assert set(t) == {"proxy_wallet", "side", "size", "price", "timestamp", "outcome_index"}
    assert t["proxy_wallet"].startswith("0x") and t["side"] in ("BUY", "SELL")
    assert isinstance(t["size"], float) and isinstance(t["price"], float)
    assert isinstance(t["timestamp"], int) and t["outcome_index"] in (0, 1)


def test_fetch_trades_accepts_bare_list():
    install(lambda url, p: load("data_trades.json")["data"])
    assert len(api.fetch_trades(LIQ_CID)) == 12


def test_fetch_book_floats_sorted_best_first():
    sess = install(lambda url, p: load("clob_book_liquid.json"))
    out = api.fetch_book(LIQ_TOKEN)
    assert sess.calls[0][0].endswith("/book") and sess.calls[0][1] == {"token_id": LIQ_TOKEN}
    assert out["bids"][0] == (0.117, 100.0)
    assert out["bids"] == sorted(out["bids"], reverse=True)
    assert out["asks"] == sorted(out["asks"])
    assert all(isinstance(p, float) and isinstance(s, float) for p, s in out["bids"] + out["asks"])


@pytest.mark.parametrize("fn,arg", [(api.fetch_price_history, "t"), (api.fetch_trades, "c"), (api.fetch_book, "t")])
@pytest.mark.parametrize(
    "payload",
    [FakeResponse(404, {}), FakeResponse(200, ValueError), "junk", {"history": "x", "data": "x", "bids": 1}, None],
)
def test_integrity_fetchers_none_on_error_or_bad_shape(fn, arg, payload):
    install(lambda url, p: payload)
    assert fn(arg) is None


def test_integrity_fetchers_none_on_malformed_rows():
    install(lambda url, p: {"history": [{"t": 1, "p": "x"}]})
    assert api.fetch_price_history("t") is None
    install(lambda url, p: {"data": [{"size": 1}]})
    assert api.fetch_trades("c") is None
    install(lambda url, p: {"bids": [{"price": "0.1"}], "asks": []})
    assert api.fetch_book("t") is None


def test_list_candidates_pre_gate_refills_past_rejected_rows():
    extreme = [gamma_market(conditionId=f"0xe{i}", bestBid=0.97, bestAsk=0.99) for i in range(3)]
    ok = [gamma_market(conditionId=f"0xok{i}") for i in range(2)]
    pages = [
        {"markets": extreme, "next_cursor": "c1"},
        {"markets": ok, "next_cursor": "c2"},
    ]
    sess = install(_candidates_handler(pages))
    from orchestrator import gate

    rejected: dict[str, int] = {}
    rows = api.list_candidates(2, now=NOW, accept=lambda r: gate.eligibility(r), rejected=rejected)
    assert [r["market_id"] for r in rows] == ["0xok0", "0xok1"]
    assert len(sess.calls) == 2  # kept paging past the rejected first page
    assert rejected == {"price_extreme": 3}


def test_fetch_target_markets_handled_rows_do_not_use_slots(monkeypatch, tmp_path):
    from datetime import timedelta

    from obsidian_utils import ObsidianManager
    from orchestrator import scraper

    vault = ObsidianManager(vault_base=tmp_path)
    vault.cold_start_protocol()
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for mid, fm in {
        "0xretired": {"status": "retired", "passed": False},
        "0xbackoff": {"next_check_at": future, "passed": False},
        "0xdue": {"next_check_at": past, "passed": False},  # due again: still ingested
        "0xbet": {"passed": True},  # researched / open-bet row: Phase 2 still sees it
    }.items():
        vault.write_filter_log(mid, {"market_id": mid, "confidence_multiplier": 0.0, "details": "t", **fm})
    vault.write_filter_log("0xinactive", {"market_id": "0xinactive", "passed": False, "confidence_multiplier": 0.0, "details": "t"})
    vault.patch_frontmatter("0xinactive", "filters", {"status": "inactive"})

    handled = [gamma_market(conditionId=m) for m in ("0xretired", "0xbackoff", "0xinactive")]
    keep = [gamma_market(conditionId=m) for m in ("0xdue", "0xbet", "0xnew1", "0xnew2")]
    pages = [
        {"markets": handled, "next_cursor": "c1"},
        {"markets": keep[:2], "next_cursor": "c2"},
        {"markets": keep[2:], "next_cursor": None},
    ]
    sess = install(_candidates_handler(pages))
    real = api.list_candidates
    monkeypatch.setattr(
        api, "list_candidates",
        lambda limit, knobs=None, accept=None, rejected=None: real(
            limit, knobs, now=NOW, accept=accept, rejected=rejected
        ),
    )
    monkeypatch.setenv("PMBOT_INGEST_LIMIT", "3")
    rows = scraper.fetch_target_markets(vault=vault)
    assert [r.market_id for r in rows] == ["0xdue", "0xbet", "0xnew1"]
    assert len(sess.calls) == 3  # paged past the handled first page
