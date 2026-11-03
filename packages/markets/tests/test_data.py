from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from conftest import NOW, FakeTicker
from markets._data import (
    DataSourceError,
    UnknownTicker,
    YahooProvider,
    get_provider,
    resolve_spot,
)

LATER = NOW + timedelta(hours=2)
EARLIER = NOW - timedelta(hours=2)


def test_resolve_spot_prefers_argument_then_post_then_pre(quote_factory):
    q = quote_factory(last=338.32, post_price=328.81, post_time=LATER)
    assert resolve_spot(q, 330.0) == (330.0, "argument")
    assert resolve_spot(q) == (328.81, "post_market")
    pre = quote_factory(last=338.32, pre_price=331.0, pre_time=LATER)
    assert resolve_spot(pre) == (331.0, "pre_market")
    stale = quote_factory(last=338.32, post_price=320.0, post_time=EARLIER)
    assert resolve_spot(stale) == (338.32, "last")


def test_resolve_spot_newest_extended_hours_price_wins(quote_factory):
    newest = NOW + timedelta(hours=14)
    # Pre-market morning: Yahoo still carries last evening's post price.
    morning = quote_factory(
        pre_price=331.0, pre_time=newest, post_price=329.0, post_time=LATER
    )
    assert resolve_spot(morning) == (331.0, "pre_market")
    evening = quote_factory(
        pre_price=331.0, pre_time=LATER, post_price=329.0, post_time=newest
    )
    assert resolve_spot(evening) == (329.0, "post_market")
    stale = quote_factory(
        last=338.32,
        pre_price=331.0,
        pre_time=EARLIER,
        post_price=329.0,
        post_time=EARLIER,
    )
    assert resolve_spot(stale) == (338.32, "last")
    assert resolve_spot(morning, 330.0) == (330.0, "argument")


def test_earnings_timing_and_split():
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2099-01-30 16:00", tz="America/New_York"),
            pd.Timestamp("2025-01-30 08:30", tz="America/New_York"),
            pd.Timestamp("2025-05-01 16:00", tz="America/New_York"),
        ],
        name="Earnings Date",
    )
    FakeTicker.earnings_data = pd.DataFrame(
        {
            "EPS Estimate": [2.1, 1.5, 1.6],
            "Reported EPS": [np.nan, 1.6, 1.7],
            "Surprise(%)": [np.nan, 6.67, 6.25],
        },
        index=idx,
    )
    events, nxt = YahooProvider().earnings("AAPL")
    assert [e.timing for e in events] == ["before_open", "after_close"]
    assert events[0].date < events[1].date
    assert events[0].date == date(2025, 1, 30)
    assert events[0].eps_reported == 1.6
    assert events[1].surprise_pct == 6.25
    assert nxt is not None
    assert nxt.eps_reported is None
    assert nxt.date == date(2099, 1, 30)
    assert nxt.eps_estimate == 2.1
    assert nxt.surprise_pct is None


def test_earnings_empty():
    assert YahooProvider().earnings("AAPL") == ([], None)


def test_http_429_maps_to_data_source_error():
    FakeTicker.history_error = Exception("429 Too Many Requests")
    with pytest.raises(DataSourceError, match="retry later") as exc:
        YahooProvider().history("AAPL", date(2025, 1, 1))
    assert "429 Too Many Requests" in str(exc.value)


def test_empty_history_and_no_quote_is_unknown_ticker():
    with pytest.raises(UnknownTicker):
        YahooProvider().history("ZZZZQ", date(2025, 1, 1))


def test_quote_without_price_is_unknown_ticker():
    FakeTicker.info_data = {"quoteType": "NONE"}
    with pytest.raises(UnknownTicker):
        YahooProvider().quote("ZZZZQ")


def test_history_strips_tz_and_normalizes_daily_index():
    idx = pd.DatetimeIndex(
        ["2025-01-02 00:00", "2025-01-03 00:00"], tz="America/New_York"
    )
    FakeTicker.history_data = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [1.5, 2.5],
            "Low": [0.5, 1.5],
            "Close": [1.2, 2.2],
            "Volume": [10, 20],
            "Dividends": [0.0, 0.0],
        },
        index=idx,
    )
    df = YahooProvider().history("AAPL", date(2025, 1, 1))
    assert df.index.tz is None
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert list(df.index) == [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")]
    assert FakeTicker.last_history_kwargs["auto_adjust"] is True


def test_quote_parses_info():
    FakeTicker.info_data = {
        "regularMarketPrice": 338.32,
        "regularMarketPreviousClose": 335.0,
        "regularMarketTime": int(NOW.timestamp()),
        "postMarketPrice": 328.81,
        "postMarketTime": int(LATER.timestamp()),
        "currency": "USD",
        "dividendYield": 0.5,
    }
    q = YahooProvider().quote("AAPL")
    assert q.last == 338.32
    assert q.previous_close == 335.0
    assert q.regular_time == NOW
    assert q.regular_time.tzinfo is not None
    assert (q.post_price, q.post_time) == (328.81, LATER)
    assert q.pre_price is None
    assert q.pre_time is None
    assert q.dividend_yield == pytest.approx(0.005)


def test_option_chain_and_expiries():
    FakeTicker.expiries = ("2025-06-20", "2025-07-18")
    calls = pd.DataFrame(
        {
            "contractSymbol": ["x"],
            "strike": [100.0],
            "bid": [1.0],
            "ask": [1.1],
            "lastPrice": [1.05],
            "impliedVolatility": [0.3],
            "openInterest": [5],
            "volume": [2],
        }
    )
    FakeTicker.chain = SimpleNamespace(calls=calls, puts=calls)
    p = YahooProvider()
    assert p.option_expiries("AAPL") == [date(2025, 6, 20), date(2025, 7, 18)]
    c, _ = p.option_chain("AAPL", date(2025, 6, 20))
    assert list(c.columns) == [
        "strike",
        "bid",
        "ask",
        "lastPrice",
        "impliedVolatility",
        "openInterest",
        "volume",
    ]


def test_rate_is_irx_close_over_100():
    FakeTicker.history_data = pd.DataFrame(
        {
            "Open": [4.2, 4.3],
            "High": [4.2, 4.3],
            "Low": [4.2, 4.3],
            "Close": [4.2, 4.3],
            "Volume": [0, 0],
        },
        index=pd.DatetimeIndex(["2025-06-02", "2025-06-03"]),
    )
    assert YahooProvider().rate() == pytest.approx(0.043)


def test_cache_hits_within_ttl_and_expires():
    FakeTicker.info_data = {
        "regularMarketPrice": 100.0,
        "regularMarketPreviousClose": 99.0,
        "regularMarketTime": int(NOW.timestamp()),
    }
    now = [1000.0]
    p = YahooProvider(clock=lambda: now[0])
    p.quote("AAPL")
    p.quote("AAPL")
    assert FakeTicker.calls["info"] == 1
    now[0] += 31
    p.quote("AAPL")
    assert FakeTicker.calls["info"] == 2


def test_get_provider_is_a_singleton(monkeypatch):
    monkeypatch.setattr("markets._data._provider", None)
    assert get_provider() is get_provider()
    assert isinstance(get_provider(), YahooProvider)
