import json
from datetime import date

import pandas as pd
import pytest
from fixture_data import fixture_history
from markets._data import EarningsEvent
from markets._events import day_zero, event_study


def _ev(idx, i, timing="after_close", surprise=None):
    return EarningsEvent(idx[i].date(), timing, None, None, surprise)


def _frame(n=300):
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame(
        {c: [100.0] * n for c in ("Open", "High", "Low", "Close")}
        | {"Volume": [1] * n},
        index=idx,
    )


def _set(df, i, **bars):
    for col, v in bars.items():
        df.iloc[i, df.columns.get_loc(col.capitalize())] = v


@pytest.fixture
def prices():
    df = _frame()
    # event A after close at 100: -5%, -6%, then a high of 101.05
    _set(df, 101, open=98.0, close=95.0, low=95.0)
    _set(df, 102, close=94.0, low=94.0)
    _set(df, 104, high=101.05, close=101.0)
    # event B after close at 150: +4% on day 1
    _set(df, 151, open=102.0, close=104.0, high=104.0)
    return df


def test_day_zero_alignment():
    idx = pd.bdate_range("2026-01-01", periods=10)
    assert (
        day_zero(idx, EarningsEvent(idx[4].date(), "after_close", None, None, None))
        == 4
    )
    assert (
        day_zero(idx, EarningsEvent(idx[4].date(), "before_open", None, None, None))
        == 3
    )


def test_day_zero_weekend_and_out_of_range():
    idx = pd.bdate_range("2026-01-01", periods=10)
    sat = date(2026, 1, 3)
    assert day_zero(idx, EarningsEvent(sat, "after_close", None, None, None)) == 1
    assert (
        day_zero(idx, EarningsEvent(date(2025, 12, 1), "after_close", None, None, None))
        is None
    )
    assert (
        day_zero(idx, EarningsEvent(idx[0].date(), "before_open", None, None, None))
        is None
    )


def test_returns_and_after_anchor_exact(prices):
    ev = _ev(prices.index, 100)
    res = event_study(prices, [ev], horizon_days=5, anchor_day=2)
    row = res["events"][0]
    assert row["date"] == prices.index[100].date().isoformat()
    assert row["r1"] == -5.0 and row["r2"] == -6.0 and row["max_after"] == 7.5
    assert row["gap_open"] == -2.0
    assert row["min_after"] == 6.38  # 100/94 - 1, lows after the anchor stay at 100
    assert row["end_after"] == 6.38
    assert row["best_day"] == 4  # close of 101 on session day0+4
    assert row["r5"] == 0.0
    assert row["r10"] == 0.0
    assert res["n_events"] == 1 and res["dropped_events"] == 0
    assert res["after_anchor"]["max"] == {"p25": 7.5, "median": 7.5, "p75": 7.5}
    assert res["days"][1] == {"mean": -5.0, "median": -5.0, "pct_up": 0.0}
    assert set(res["days"]) == {1, 2, 3, 5}


def test_filters_and_dropped(prices):
    ev_down = _ev(prices.index, 100)
    ev_up = _ev(prices.index, 150)
    ev_last_night = _ev(prices.index, 298)
    res = event_study(
        prices, [ev_down, ev_up, ev_last_night], horizon_days=5, day1_max=-2
    )
    assert res["n_events"] == 1 and res["dropped_events"] == 1
    assert res["events"][0]["r1"] == -5.0
    res = event_study(prices, [ev_down, ev_up], horizon_days=5, day1_min=2)
    assert [r["r1"] for r in res["events"]] == [4.0]


def test_surprise_filter_excludes_missing(prices):
    evs = [
        _ev(prices.index, 100, surprise=8.0),
        _ev(prices.index, 150, surprise=None),
        _ev(prices.index, 200, surprise=-3.0),
    ]
    res = event_study(prices, evs, horizon_days=5, surprise_min=0)
    assert res["n_events"] == 1 and res["events"][0]["surprise_pct"] == 8.0
    res = event_study(prices, evs, horizon_days=5, surprise_max=0)
    assert res["n_events"] == 1 and res["events"][0]["surprise_pct"] == -3.0


def test_since_filter(prices):
    evs = [_ev(prices.index, 100), _ev(prices.index, 150)]
    res = event_study(prices, evs, horizon_days=5, since=prices.index[120].date())
    assert res["n_events"] == 1


def test_hit_rates_default_levels(prices):
    res = event_study(prices, [_ev(prices.index, 100)])
    assert [h["pct"] for h in res["hit_rates"]] == [2, 3, 4, 5, 7, 10]


def test_hit_rates_values(prices):
    res = event_study(
        prices, [_ev(prices.index, 100)], horizon_days=5, levels_pct=[5, 7.5, 8]
    )
    assert res["hit_rates"] == [
        {"pct": 5, "up": 100.0, "down": 0.0},
        {"pct": 7.5, "up": 100.0, "down": 0.0},
        {"pct": 8, "up": 0.0, "down": 0.0},
    ]


def test_baseline_flat(prices):
    res = event_study(prices, [_ev(prices.index, 200)], horizon_days=5)
    assert res["baseline"] == {"mean": 0.0, "median": 0.0, "pct_up": 0.0}


def test_events_limited_to_last_12(prices):
    evs = [_ev(prices.index, i) for i in range(20, 260, 10)]
    assert len(event_study(prices, evs, horizon_days=5)["events"]) == 12
    res = event_study(prices, evs, horizon_days=5, include_all_events=True)
    assert len(res["events"]) == 24


def test_no_events_is_json_safe(prices):
    res = event_study(prices, [], horizon_days=5)
    assert res["n_events"] == 0 and res["events"] == []
    assert res["days"][1] == {"mean": None, "median": None, "pct_up": None}
    json.dumps(res)


def test_recorded_history_smoke():
    df = fixture_history()
    evs = [_ev(df.index, i) for i in range(30, len(df.index) - 40, 40)]
    res = event_study(df, evs)
    assert res["n_events"] == len(evs)
    json.dumps(res)
