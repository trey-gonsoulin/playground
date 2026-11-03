"""Earnings event study: how a stock moves after reports, from daily bars. Pure computation."""

from datetime import date

import numpy as np
import pandas as pd

from markets._data import EarningsEvent

DAY_MARKS = (1, 2, 3, 5, 10, 15, 20)
DEFAULT_LEVELS_PCT = [2, 3, 4, 5, 7, 10]
RECENT_EVENT_ROWS = 12


def day_zero(index: pd.DatetimeIndex, event: EarningsEvent) -> int | None:
    """Position of the last session whose close is known before the reaction.

    after_close: the last session on or before the report date.
    before_open: the last session strictly before it.
    """
    side = "right" if event.timing == "after_close" else "left"
    pos = int(index.searchsorted(pd.Timestamp(event.date), side=side)) - 1
    return pos if pos >= 0 else None


def _r(x: float) -> float:
    return round(float(x), 2)


def _pct(ratio: float) -> float:
    return _r((ratio - 1) * 100)


def _quartiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p25": None, "median": None, "p75": None}
    p25, p50, p75 = np.percentile(values, [25, 50, 75])
    return {"p25": _r(p25), "median": _r(p50), "p75": _r(p75)}


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "pct_up": None}
    arr = np.asarray(values)
    return {
        "mean": _r(arr.mean()),
        "median": _r(np.median(arr)),
        "pct_up": _r((arr > 0).mean() * 100),
    }


def _event_row(
    prices: pd.DataFrame,
    event: EarningsEvent,
    d0: int,
    anchor_day: int,
    horizon_days: int,
) -> dict:
    close = prices["Close"].to_numpy()
    n = len(close)
    a = d0 + anchor_day
    window = slice(a + 1, a + horizon_days + 1)
    ret = {
        k: _pct(close[d0 + k] / close[d0]) if d0 + k < n else None for k in DAY_MARKS
    }
    return {
        "date": event.date.isoformat(),
        "timing": event.timing,
        "surprise_pct": None if event.surprise_pct is None else _r(event.surprise_pct),
        "gap_open": _pct(prices["Open"].iloc[d0 + 1] / close[d0]),
        "r1": ret[1],
        "r2": ret[2],
        "r5": ret[5],
        "r10": ret[10],
        "r20": ret[20],
        "max_after": _pct(prices["High"].iloc[window].max() / close[a]),
        "min_after": _pct(prices["Low"].iloc[window].min() / close[a]),
        "end_after": _pct(close[a + horizon_days] / close[a]),
        "best_day": anchor_day + 1 + int(np.argmax(close[window])),
        "_returns": ret,
    }


def _in_range(value: float | None, low: float | None, high: float | None) -> bool:
    if low is None and high is None:
        return True
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def event_study(
    prices: pd.DataFrame,
    events: list[EarningsEvent],
    *,
    horizon_days: int = 20,
    anchor_day: int = 2,
    day1_min: float | None = None,
    day1_max: float | None = None,
    surprise_min: float | None = None,
    surprise_max: float | None = None,
    since: date | None = None,
    levels_pct: list[float] | None = None,
    include_all_events: bool = False,
) -> dict:
    """Study price moves after earnings. Returns, filters and level hit rates are in percent.

    Events whose day 0 is unknown or whose anchor + horizon window runs past the data are dropped
    (counted in `dropped_events`); the filters then narrow the rest.
    """
    levels = DEFAULT_LEVELS_PCT if levels_pct is None else levels_pct
    rows, first_d0, dropped = [], None, 0
    for event in sorted(events, key=lambda e: e.date):
        d0 = day_zero(prices.index, event)
        if d0 is None or d0 + anchor_day + horizon_days >= len(prices):
            dropped += 1
            continue
        if since is not None and event.date < since:
            continue
        if not _in_range(event.surprise_pct, surprise_min, surprise_max):
            continue
        row = _event_row(prices, event, d0, anchor_day, horizon_days)
        if not _in_range(row["r1"], day1_min, day1_max):
            continue
        rows.append(row)
        first_d0 = d0 if first_d0 is None else min(first_d0, d0)

    marks = [k for k in DAY_MARKS if k <= horizon_days]
    days = {k: _summary([r["_returns"][k] for r in rows]) for k in marks}
    maxes = [r["max_after"] for r in rows]
    mins = [r["min_after"] for r in rows]
    hit_rates = [
        {
            "pct": level,
            "up": _r(np.mean([m >= level for m in maxes]) * 100) if rows else None,
            "down": _r(np.mean([m <= -level for m in mins]) * 100) if rows else None,
        }
        for level in levels
    ]

    close = prices["Close"].iloc[first_d0 or 0 :]
    baseline = _summary(list(((close.shift(-horizon_days) / close - 1) * 100).dropna()))

    shown = rows if include_all_events else rows[-RECENT_EVENT_ROWS:]
    return {
        "n_events": len(rows),
        "dropped_events": dropped,
        "days": days,
        "after_anchor": {
            "max": _quartiles(maxes),
            "min": _quartiles(mins),
            "end": _quartiles([r["end_after"] for r in rows]),
        },
        "hit_rates": hit_rates,
        "baseline": baseline,
        "events": [{k: v for k, v in r.items() if k != "_returns"} for r in shown],
    }
