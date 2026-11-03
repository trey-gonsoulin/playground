from datetime import date

import numpy as np
import pandas as pd
import pytest
from fixture_data import fixture_chains, fixture_close, fixture_history, fixture_meta
from markets._data import DataSourceError, EarningsEvent, Quote
from markets._vol import (
    atm,
    bs_price,
    default_sigma,
    forward_vol,
    garch_forecast,
    implied_vol,
    pending_event,
    realized_vol,
    term_structure,
)

TODAY = date(2026, 11, 2)


def test_bs_iv_round_trip():
    p = bs_price(100, 105, 0.25, 0.04, 0.005, 0.3, call=True)
    assert implied_vol(p, 100, 105, 0.25, 0.04, 0.005, call=True) == pytest.approx(
        0.3, abs=1e-6
    )


def test_implied_vol_none_below_intrinsic():
    assert implied_vol(1.0, 100, 80, 0.25, 0.04, 0.0, call=True) is None


def test_forward_vol_hand_calc():
    assert forward_vol(0.59, 4 / 365, 0.295, 46 / 365) == pytest.approx(
        0.2493, abs=5e-4
    )
    assert forward_vol(0.2, 0.1, 0.1, 0.11) is None
    assert forward_vol(0.2, 0.2, 0.2, 0.1) is None


def _term_structure(event, chains=None):
    meta = fixture_meta()
    return term_structure(
        chains or fixture_chains(),
        meta["spot"],
        TODAY,
        meta["rate"],
        meta["dividend_yield"],
        event,
    )


def test_ex_event_vol_skips_event_week_expiry():
    ts = _term_structure(date(2026, 11, 2))
    assert [e["days"] for e in ts["expiries"]] == [2, 4, 7, 9, 11, 18]
    assert all(f["from"] > date(2026, 11, 2) for f in ts["forward"])
    assert 0.15 < ts["ex_event_vol"] < ts["expiries"][0]["atm_iv"]


def test_term_structure_without_event_starts_at_first_expiry():
    ts = _term_structure(None)
    assert ts["forward"][0]["from"] == ts["expiries"][0]["expiry"]
    first = ts["expiries"][0]
    assert first["expected_move_pct"] == pytest.approx(
        first["atm_iv"] * np.sqrt(first["days"] / 365) * 100
    )


def test_term_structure_single_expiry_has_no_forward():
    ts = _term_structure(None, fixture_chains()[:1])
    assert ts["forward"] == []
    assert ts["ex_event_vol"] is None


def test_pending_event_after_close_today_wins():
    ev = [EarningsEvent(date(2026, 11, 2), "after_close", 1.99, 2.10, 5.75)]
    nxt = EarningsEvent(date(2027, 1, 28), "after_close", None, None, None)
    assert pending_event(ev, nxt, date(2026, 11, 2)) == date(2026, 11, 2)


def test_pending_event_falls_back_to_next():
    old = [EarningsEvent(date(2026, 8, 1), "after_close", 1.0, 1.1, 10.0)]
    nxt = EarningsEvent(date(2027, 1, 28), "after_close", None, None, None)
    assert pending_event(old, nxt, date(2026, 11, 2)) == date(2027, 1, 28)
    assert pending_event(old, None, date(2026, 11, 2)) is None


def test_pending_event_before_open_today_is_already_priced_in():
    ev = [EarningsEvent(date(2026, 11, 2), "before_open", 1.0, 1.1, 10.0)]
    assert pending_event(ev, None, date(2026, 11, 2)) is None


def _chain(bid, ask, iv, last=0.0):
    return pd.DataFrame(
        {
            "strike": [100.0],
            "bid": [bid],
            "ask": [ask],
            "lastPrice": [last],
            "impliedVolatility": [iv],
            "openInterest": [1],
            "volume": [1],
        }
    )


def test_atm_uses_mid_when_quotes_are_good():
    sigma = 0.3
    call = bs_price(100, 100, 0.25, 0.04, 0.0, sigma, call=True)
    put = bs_price(100, 100, 0.25, 0.04, 0.0, sigma, call=False)
    res = atm(
        _chain(call - 0.05, call + 0.05, 0.9),
        _chain(put - 0.05, put + 0.05, 0.9),
        100,
        0.25,
        0.04,
        0.0,
    )
    assert res["strike"] == 100.0
    assert res["call_iv"] == pytest.approx(sigma, abs=1e-4)
    assert res["iv"] == pytest.approx(sigma, abs=1e-4)
    assert res["straddle_mid"] == pytest.approx(call + put)


def test_atm_reports_mid_source():
    call = bs_price(100, 100, 0.25, 0.04, 0.0, 0.3, call=True)
    res = atm(
        _chain(call - 0.05, call + 0.05, 0.9),
        _chain(call - 0.05, call + 0.05, 0.9),
        100,
        0.25,
        0.04,
        0.0,
    )
    assert res["iv_source"] == "mid"


def test_atm_falls_back_on_crossed_quote_to_last_price():
    call_last = bs_price(100, 100, 0.25, 0.04, 0.0, 0.35, call=True)
    put_last = bs_price(100, 100, 0.25, 0.04, 0.0, 0.37, call=False)
    res = atm(
        _chain(0, 0, 0.9, last=call_last),
        _chain(1.0, 0.5, 0.9, last=put_last),
        100,
        0.25,
        0.04,
        0.0,
    )
    assert res["call_iv"] == pytest.approx(
        implied_vol(call_last, 100, 100, 0.25, 0.04, 0.0, call=True)
    )
    assert res["call_iv"] == pytest.approx(0.35, abs=1e-6)
    assert res["put_iv"] == pytest.approx(0.37, abs=1e-6)
    assert res["iv_source"] == "last"
    assert res["straddle_mid"] == pytest.approx(call_last + put_last)


def test_atm_uses_sane_yahoo_iv_as_last_resort():
    res = atm(_chain(0, 0, 0.41), _chain(0, 0, 0.43), 100, 0.25, 0.04, 0.0)
    assert res["iv"] == pytest.approx(0.42)
    assert res["iv_source"] == "yahoo"
    assert res["straddle_mid"] is None


def test_atm_drops_side_with_junk_yahoo_iv():
    put_last = bs_price(100, 100, 0.25, 0.04, 0.0, 0.37, call=False)
    res = atm(
        _chain(0, 0, 1e-5),
        _chain(0, 0, 1e-5, last=put_last),
        100,
        0.25,
        0.04,
        0.0,
    )
    assert res["call_iv"] is None
    assert res["iv"] == pytest.approx(0.37, abs=1e-6)


def test_atm_tries_next_nearest_strike_when_both_sides_unusable():
    dead = _chain(0, 0, 1e-5)
    good = _chain(0, 0, 0.4)
    good["strike"] = 110.0
    both = pd.concat([dead, good], ignore_index=True)
    res = atm(both, both, 100, 0.25, 0.04, 0.0)
    assert res["strike"] == 110.0
    assert res["iv"] == pytest.approx(0.4)


def test_atm_none_when_nothing_usable():
    res = atm(_chain(0, 0, 1e-5), _chain(0, 0, 1e-5), 100, 0.25, 0.04, 0.0)
    assert res["iv"] is None
    assert res["iv_source"] is None


def test_term_structure_skips_expiry_without_usable_iv():
    chains = fixture_chains()
    junk = _chain(0, 0, 1e-5)
    skipped = [(chains[0][0], junk, junk), *chains[1:]]
    ts = _term_structure(None, skipped)
    assert [e["expiry"] for e in ts["expiries"]] == [c[0] for c in chains[1:]]
    nonoverlap = [(chains[0][0], junk, _chain(0, 0, 1e-5).assign(strike=105.0))]
    assert _term_structure(None, nonoverlap)["expiries"] == []


def test_realized_vol_matches_hand_calc():
    close = pd.Series([100.0, 101.0, 99.0, 102.0, 100.0])
    rets = np.log(close).diff().dropna()
    assert realized_vol(close, 3) == pytest.approx(
        rets.iloc[-3:].std(ddof=1) * np.sqrt(252)
    )


def test_garch_is_positive_and_reasonable():
    assert 0.05 < garch_forecast(fixture_close(), 20) < 1.0


class FakeProvider:
    def __init__(self, with_options=True, expiry_count=None, post_price=None):
        self.meta = fixture_meta()
        self.with_options = with_options
        self.expiry_count = expiry_count
        self.post_price = post_price

    def history(self, ticker, start, end=None, interval="1d", prepost=False):
        return fixture_history()

    def quote(self, ticker):
        stamp = pd.Timestamp("2026-11-02 21:00", tz="UTC").to_pydatetime()
        return Quote(
            ticker,
            "USD",
            self.meta["spot"],
            self.meta["spot"],
            stamp,
            None,
            None,
            self.post_price,
            None if self.post_price is None else stamp + pd.Timedelta(hours=1),
            self.meta["dividend_yield"],
        )

    def earnings(self, ticker, limit=80):
        return [EarningsEvent(date(2026, 11, 2), "after_close", 1.99, 2.10, 5.75)], None

    def option_expiries(self, ticker):
        if not self.with_options:
            return []
        return [c[0] for c in fixture_chains()][: self.expiry_count]

    def option_chain(self, ticker, expiry):
        return next((c[1], c[2]) for c in fixture_chains() if c[0] == expiry)

    def rate(self):
        return self.meta["rate"]


def test_default_sigma_prefers_ex_event_options():
    sigma, source = default_sigma(FakeProvider(), "AAPL", TODAY)
    assert source == "options_ex_event"
    assert 0.15 < sigma < 1.0


def test_default_sigma_falls_back_to_realized():
    sigma, source = default_sigma(FakeProvider(with_options=False), "AAPL", TODAY)
    assert source == "realized_20d"
    assert sigma == pytest.approx(realized_vol(fixture_close(), 20))


def test_default_sigma_inverts_at_regular_session_price_not_post_market():
    meta = fixture_meta()
    after_hours = default_sigma(
        FakeProvider(post_price=meta["spot_after_hours"]), "AAPL", TODAY
    )
    regular = default_sigma(FakeProvider(), "AAPL", TODAY)
    assert after_hours == regular
    ts = _term_structure(date(2026, 11, 2))
    assert regular[0] == pytest.approx(ts["ex_event_vol"])
    # At the regular price the front expiry's call and put IVs agree; at the
    # post-market price they would not.
    _, calls, puts = fixture_chains()[0]
    front = atm(
        calls, puts, meta["spot"], 2 / 365, meta["rate"], meta["dividend_yield"]
    )
    assert abs(front["call_iv"] - front["put_iv"]) < 0.15
    moved = atm(
        calls,
        puts,
        meta["spot_after_hours"],
        2 / 365,
        meta["rate"],
        meta["dividend_yield"],
    )
    assert abs(moved["call_iv"] - moved["put_iv"]) > 0.3


def test_default_sigma_single_expiry_uses_atm_iv():
    sigma, source = default_sigma(FakeProvider(expiry_count=1), "AAPL", TODAY)
    assert source == "options_atm"
    assert sigma == pytest.approx(_term_structure(None)["expiries"][0]["atm_iv"])


def test_default_sigma_propagates_data_source_error():
    class Broken(FakeProvider):
        def option_chain(self, ticker, expiry):
            raise DataSourceError("Yahoo refused")

    with pytest.raises(DataSourceError):
        default_sigma(Broken(), "AAPL", TODAY)
