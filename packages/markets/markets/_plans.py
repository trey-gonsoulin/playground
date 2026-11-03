"""Sell plans: tranches of market, limit, stop-limit and bracket orders, evaluated on simulated paths."""

import math
from dataclasses import dataclass, fields
from typing import Literal

import numpy as np

from markets._paths import Paths

NOTE = (
    "With zero drift, no plan changes expected proceeds much; plans trade off the spread of outcomes. "
    "Positive drift favors waiting, negative favors selling now."
)
FRACTION_TOLERANCE = 1e-6
PERCENTILES = (5, 25, 50, 75, 95)
REQUIRED = {
    "market": (),
    "limit": ("price",),
    "stop_limit": ("price", "limit_price"),
    "bracket": ("take_profit", "stop"),
}
# Price fields each type reads: the required ones plus bracket's optional limit_price.
ALLOWED = REQUIRED | {"bracket": ("take_profit", "stop", "limit_price")}


@dataclass(frozen=True)
class Tranche:
    """One order for `fraction` of the shares; see `parse_plans` for which prices each type needs."""

    fraction: float
    type: Literal["market", "limit", "stop_limit", "bracket"]
    price: float | None = None
    limit_price: float | None = None
    take_profit: float | None = None
    stop: float | None = None


@dataclass(frozen=True)
class Plan:
    name: str
    tranches: list[Tranche]


PRICE_FIELDS = ("price", "limit_price", "take_profit", "stop")
TRANCHE_FIELDS = {f.name for f in fields(Tranche)}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _parse_tranche(raw: dict) -> Tranche:
    unknown = set(raw) - TRANCHE_FIELDS
    if unknown:
        raise ValueError(f"unknown tranche fields: {', '.join(sorted(unknown))}")
    kind = raw.get("type")
    if kind not in REQUIRED:
        raise ValueError(
            f"tranche type must be one of {', '.join(REQUIRED)}, got {kind!r}"
        )
    for name in REQUIRED[kind]:
        if raw.get(name) is None:
            raise ValueError(f"{kind} tranche needs {name}")
    for name in PRICE_FIELDS:
        if raw.get(name) is not None and name not in ALLOWED[kind]:
            raise ValueError(f"{kind} tranche does not use {name}")
    for name in ("fraction", *PRICE_FIELDS):
        value = raw.get(name)
        if value is None and name != "fraction":
            continue
        if not _is_number(value) or not value > 0:
            raise ValueError(f"{name} must be a positive number, got {value!r}")
    fraction = raw["fraction"]
    t = Tranche(**{**raw, "fraction": float(fraction)})
    if kind == "stop_limit" and t.limit_price > t.price:
        raise ValueError("stop_limit limit_price must be at most price (the stop)")
    if kind == "bracket":
        if t.stop >= t.take_profit:
            raise ValueError("bracket take_profit must be above stop")
        if t.limit_price is not None and t.limit_price > t.stop:
            raise ValueError("bracket limit_price must be at most stop")
    return t


def parse_plans(raw: list[dict]) -> list[Plan]:
    """Validate `[{"name", "tranches": [{"fraction", "type", ...}]}]` into plans.

    `limit` needs `price`; `stop_limit` needs `price` (the stop) and `limit_price` ≤ stop; `bracket` needs
    `take_profit` > `stop`, with an optional `limit_price` ≤ stop (without it, the stop leg is a stop-market).
    `market` takes no price, and a price field the type doesn't use is rejected.
    """
    plans = []
    for p in raw:
        name = p.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("each plan needs a name")
        tranches = [_parse_tranche(t) for t in p.get("tranches") or []]
        if not tranches:
            raise ValueError(f"plan {name!r} needs at least one tranche")
        total = sum(t.fraction for t in tranches)
        if abs(total - 1) > FRACTION_TOLERANCE:
            raise ValueError(
                f"plan {name!r} tranche fractions must sum to 1, got {total:g}"
            )
        plans.append(Plan(name=name, tranches=tranches))
    return plans


def _fills(t: Tranche, paths: Paths) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per path: (sale price, filled, 1-based session of the fill, 0 = now). Unfilled paths sell at the last close."""
    n_paths, days = paths.open.shape
    spot = paths.spot
    # Every order type as: sell above `up`, trigger below `down`, then rest as a limit at `rest`.
    up = {"market": -math.inf, "limit": t.price, "bracket": t.take_profit}.get(
        t.type
    ) or math.inf
    down = {"stop_limit": t.price, "bracket": t.stop}.get(t.type) or -math.inf
    rest = t.limit_price if t.limit_price is not None else -math.inf

    # Marketable at submission: fills at spot on day 0, or a triggered stop rests from session 1.
    if spot >= up or (spot <= down and spot >= rest):
        return np.full(n_paths, spot), np.ones(n_paths, dtype=bool), np.zeros(n_paths)
    price = paths.close[:, -1].copy()
    filled = np.zeros(n_paths, dtype=bool)
    day = np.zeros(n_paths)
    resting = np.full(n_paths, spot <= down)
    for d in range(days):
        o, h, lo = paths.open[:, d], paths.high[:, d], paths.low[:, d]
        waiting = ~filled & ~resting
        # A resting order is a plain limit at `rest`.
        fill = np.where(
            resting & (o >= rest), o, np.where(resting & (h >= rest), rest, np.nan)
        )
        # Gaps at the open come first.
        gap_up = waiting & (o >= up)
        gap_down = waiting & (o <= down)
        fill = np.where(gap_up | (gap_down & (o >= rest)), o, fill)
        rests_now = gap_down & (o < rest)
        fill = np.where(rests_now & (h >= rest), rest, fill)
        resting |= rests_now
        # Intraday touches; the stop wins when both are touched.
        intraday = waiting & ~gap_up & ~gap_down
        fill = np.where(intraday & (h >= up), up, fill)
        fill = np.where(intraday & (lo <= down), down, fill)

        now = ~filled & ~np.isnan(fill)
        price[now] = fill[now]
        day[now] = d + 1
        filled |= now
    return price, filled, day


def _stats(x: np.ndarray) -> dict:
    out = {
        f"p{q}": float(v)
        for q, v in zip(PERCENTILES, np.percentile(x, PERCENTILES), strict=True)
    }
    return out | {"mean": float(x.mean()), "std": float(x.std())}


def evaluate_plans(paths: Paths, shares: float, plans: list[Plan]) -> dict:
    """Proceeds of each plan on the same paths, versus selling everything at spot now.

    Anything still unfilled after the last session sells at its close and counts as not filled.
    """
    sell_now = shares * paths.spot
    results = []
    for plan in plans:
        proceeds = np.zeros(paths.open.shape[0])
        tranches = []
        for t in plan.tranches:
            price, filled, day = _fills(t, paths)
            proceeds += t.fraction * shares * price
            any_filled = bool(filled.any())
            tranches.append(
                {
                    "type": t.type,
                    "fill_rate": float(filled.mean()),
                    "mean_fill_price": float(price[filled].mean())
                    if any_filled
                    else None,
                    "mean_days_to_fill": float(day[filled].mean())
                    if any_filled
                    else None,
                }
            )
        results.append(
            {
                "name": plan.name,
                "proceeds": _stats(proceeds),
                "vs_sell_now": _stats(proceeds - sell_now),
                "tranches": tranches,
            }
        )
    return {
        "spot": paths.spot,
        "sell_now": float(sell_now),
        "plans": results,
        "note": NOTE,
    }
