"""markets MCP server: thin tool wrappers over the data, volatility, odds, event and plan modules.

Conventions in every result: `as_of` (ISO UTC) and `source: "yahoo"`; volatilities, drift and
probabilities are fractions (0.25 = 25%); fields ending in `_pct` are percents.
"""

import functools
import math
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from markets import _data
from markets._data import (
    NEW_YORK,
    DataSourceError,
    EarningsEvent,
    MarketData,
    resolve_spot,
)
from markets._events import event_study
from markets._odds import TRADING_DAYS_PER_YEAR, bootstrap_odds, lognormal_odds
from markets._paths import overnight_share, simulate, split_returns
from markets._plans import evaluate_plans, parse_plans
from markets._vol import (
    CHAIN_HORIZON_DAYS,
    default_sigma,
    garch_forecast,
    pending_event,
    realized_vol,
    term_structure,
)

mcp = MCPServer("markets")

MAX_BARS = 1000
BOOTSTRAP_YEARS = 5
REALIZED_WINDOWS = (10, 20, 60)
INTERVALS = {
    "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo",
}  # fmt: skip
PERIOD = re.compile(r"(\d+)(d|wk|mo|y)")
PERIOD_UNITS = {"d": "days", "wk": "weeks", "mo": "months", "y": "years"}
METHODS = ("lognormal", "bootstrap")
VOL_SOURCES = ("options", "history")
EXTENDED_HOURS_SOURCES = ("post_market", "pre_market")
EXTENDED_HOURS_NOTE = (
    "spot is an extended-hours price: market and marketable orders are modelled as filling at it "
    "now, but a real order placed now fills at the next regular open (the overnight gap risk of "
    "that day-0 fill is not modelled)."
)


def _today() -> date:
    """The current New York date (tests pin it to the fixture capture date)."""
    return datetime.now(ZoneInfo(NEW_YORK)).date()


def _envelope(result: dict) -> dict:
    return _jsonable(result) | {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "yahoo",
    }


def _jsonable(value: Any) -> Any:
    """Dates to ISO strings, numpy scalars to Python, NaN to None, dict keys to str."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):  # datetime included
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# Argument checks: fail at the tool boundary with a clear message.


def _ticker(ticker: str) -> str:
    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker must not be empty")
    return symbol


def _check_positive(name: str, value: float | None) -> None:
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")


def _check_at_least(name: str, value: int, low: int = 1) -> None:
    if value < low:
        raise ValueError(f"{name} must be at least {low}, got {value!r}")


def _check_levels(levels: list[float]) -> None:
    if not levels:
        raise ValueError("levels must hold at least one price")
    for level in levels:
        if not (math.isfinite(level) and level > 0):
            raise ValueError(f"levels must be positive finite prices, got {level!r}")


def _check_drift(drift: float) -> None:
    if not math.isfinite(drift):
        raise ValueError(f"drift must be a finite number, got {drift!r}")


def _check_vol_source(vol_source: str) -> None:
    if vol_source not in VOL_SOURCES:
        raise ValueError(f"vol_source must be options or history, got {vol_source!r}")


# Shared data access.


def _history(provider: MarketData, ticker: str, start: date, **kwargs) -> pd.DataFrame:
    history = provider.history(ticker, start, **kwargs)
    if history.empty:
        raise ValueError(f"no price history for {ticker} from {start.isoformat()}")
    return history


def _event_json(event: EarningsEvent | None) -> dict | None:
    return None if event is None else asdict(event)


def _chains(
    provider: MarketData,
    ticker: str,
    today: date,
    *,
    max_expiries: int | None = None,
    min_days: int | None = None,
) -> list[tuple[date, pd.DataFrame, pd.DataFrame]]:
    """Option chains from the first expiry, up to `max_expiries` or the first at least `min_days` out."""
    expiries = [e for e in provider.option_expiries(ticker) if e > today]
    if not expiries:
        raise ValueError(f"{ticker} has no listed options")
    chains = []
    for expiry in expiries[:max_expiries]:
        calls, puts = provider.option_chain(ticker, expiry)
        chains.append((expiry, calls, puts))
        if min_days is not None and (expiry - today).days >= min_days:
            break
    return chains


def _term_structure(
    provider: MarketData, ticker: str, today: date, **chain_kwargs
) -> dict:
    """`term_structure` inverted at the regular-session price, with the pending earnings date."""
    chains = _chains(provider, ticker, today, **chain_kwargs)
    quote = provider.quote(ticker)
    events, nxt = provider.earnings(ticker)
    last_session = quote.regular_time.astimezone(ZoneInfo(NEW_YORK)).date()
    event = pending_event(events, nxt, last_session)
    ts = term_structure(
        chains,
        quote.last,
        today,
        provider.rate(),
        quote.dividend_yield,
        event,
    )
    if not ts["expiries"]:
        raise ValueError(f"no usable option quotes for {ticker}")
    return ts | {"spot": quote.last, "event": event}


def _sigma(
    provider: MarketData, ticker: str, today: date, vol: float | None
) -> tuple[float, str]:
    return (
        (vol, "argument") if vol is not None else default_sigma(provider, ticker, today)
    )


def _bootstrap_pairs(provider: MarketData, ticker: str, today: date) -> np.ndarray:
    start = today - timedelta(days=round(BOOTSTRAP_YEARS * 365.25))
    pairs = split_returns(_history(provider, ticker, start))
    if len(pairs) < 2:
        raise ValueError(f"not enough price history for {ticker} to bootstrap")
    return pairs


def _pairs_vol(pairs: np.ndarray) -> float:
    """Annualized close-to-close vol of the history's daily returns."""
    return float(pairs.sum(axis=1).std() * math.sqrt(TRADING_DAYS_PER_YEAR))


def _bootstrap_sigma(
    provider: MarketData,
    ticker: str,
    today: date,
    vol: float | None,
    vol_source: str,
    pairs: np.ndarray,
) -> tuple[float, str]:
    """The vol the bootstrap rescales to: the lognormal one (`_sigma`), or the history's own.

    The history's own vol applies only with vol_source="history" and no `vol`; rescaling to it
    leaves the paths as they are.
    """
    if vol is None and vol_source == "history":
        return _pairs_vol(pairs), "history_5y"
    return _sigma(provider, ticker, today, vol)


# Tools.


def _tool[F: Callable[..., dict]](fn: F) -> F:
    """Register `fn` as a tool whose expected failures reach the client with their message.

    mcp 2.x replaces the text of any exception other than ToolError with a generic
    "Error executing tool <name>", so bad arguments (ValueError, UnknownTicker) and Yahoo
    failures (DataSourceError) are re-raised as ToolError. `fn` itself is returned unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except (ValueError, DataSourceError) as exc:
            raise ToolError(str(exc)) from exc

    mcp.tool()(wrapper)
    return fn


@_tool
def quote(ticker: str) -> dict:
    """Latest quote for a stock or ETF, plus its next earnings date.

    Returns last (regular-session price), previous_close, change_pct (percent), pre/post-market
    prices and times, dividend_yield (fraction), next_earnings (date and before_open/after_close,
    or null), and spot / spot_source: the price the other tools start from (the newest post- or
    pre-market price later than the regular close, else last).
    """
    symbol = _ticker(ticker)
    provider = _data.get_provider()
    q = provider.quote(symbol)
    _, nxt = provider.earnings(symbol)
    spot, spot_source = resolve_spot(q)
    return _envelope(
        asdict(q)
        | {
            "change_pct": round((q.last / q.previous_close - 1) * 100, 2),
            "spot": spot,
            "spot_source": spot_source,
            "next_earnings": _event_json(nxt),
        }
    )


def _period_start(period: str, today: date) -> date:
    if period == "max":
        return date(1970, 1, 1)
    if period == "ytd":
        return date(today.year, 1, 1)
    match = PERIOD.fullmatch(period)
    if match is None or int(match[1]) < 1:
        raise ValueError(
            f"period must look like 5d, 2wk, 6mo, 1y, ytd or max, got {period!r}"
        )
    offset = pd.DateOffset(**{PERIOD_UNITS[match[2]]: int(match[1])})
    return (pd.Timestamp(today) - offset).date()


@_tool
def price_history(
    ticker: str, period: str = "6mo", interval: str = "1d", prepost: bool = False
) -> dict:
    """Split- and dividend-adjusted OHLCV bars.

    period: Nd, Nwk, Nmo, Ny (e.g. 5d, 6mo, 2y), ytd or max. interval: 1m 2m 5m 15m 30m 60m 90m
    1h 1d 5d 1wk 1mo 3mo (Yahoo keeps 1m bars for ~7 days, other intraday bars for ~60).
    prepost includes pre/post-market bars for intraday intervals. At most the newest 1000 bars are
    returned (truncated: true when older ones were dropped). Prices are in the quote currency.
    """
    symbol = _ticker(ticker)
    if interval not in INTERVALS:
        raise ValueError(
            f"interval must be one of {', '.join(sorted(INTERVALS))}, got {interval!r}"
        )
    start = _period_start(period, _today())
    history = _history(
        _data.get_provider(), symbol, start, interval=interval, prepost=prepost
    )
    daily = interval.endswith(("d", "wk", "mo"))
    bars = [
        {
            "date": stamp.date() if daily else stamp,
            "open": round(float(row.Open), 4),
            "high": round(float(row.High), 4),
            "low": round(float(row.Low), 4),
            "close": round(float(row.Close), 4),
            "volume": int(row.Volume) if pd.notna(row.Volume) else None,
        }
        for stamp, row in zip(
            history.index[-MAX_BARS:],
            history.iloc[-MAX_BARS:].itertuples(),
            strict=True,
        )
    ]
    return _envelope(
        {
            "ticker": symbol,
            "period": period,
            "interval": interval,
            "n_bars": len(bars),
            "truncated": len(history) > MAX_BARS,
            "bars": bars,
        }
    )


@_tool
def earnings_event_study(
    ticker: str,
    horizon_days: int = 20,
    anchor_day: int = 2,
    day1_min: float | None = None,
    day1_max: float | None = None,
    surprise_min: float | None = None,
    surprise_max: float | None = None,
    since: str | None = None,
    levels_pct: list[float] | None = None,
    include_all_events: bool = False,
) -> dict:
    """How the stock moved after past earnings reports (all returns and filters in percent).

    Day 0 is the last session before the market could react; day k is the k-th session after.
    anchor_day is the first day you could sell; max/min/end are measured over horizon_days
    sessions after it, relative to the anchor-day close. Filters: day1_min/day1_max on the day-1
    return, surprise_min/surprise_max on the EPS surprise, since (YYYY-MM-DD) on the report date.
    Returns n_events, dropped_events (no full window), per-day mean/median/pct_up of r_k,
    quartiles of max/min/end after the anchor, hit_rates P(max >= +pct) and P(min <= -pct) for
    levels_pct (default 2,3,4,5,7,10), a baseline of all horizon_days returns over the same span,
    and the last 12 events (all with include_all_events).
    """
    symbol = _ticker(ticker)
    _check_at_least("horizon_days", horizon_days)
    _check_at_least("anchor_day", anchor_day, 0)
    for level in levels_pct or []:
        _check_positive("levels_pct", level)
    try:
        since_date = None if since is None else date.fromisoformat(since)
    except ValueError:
        raise ValueError(f"since must be a YYYY-MM-DD date, got {since!r}") from None

    provider = _data.get_provider()
    events, nxt = provider.earnings(symbol)
    events = list(events)
    if not events:
        raise ValueError(f"{symbol} has no earnings history on Yahoo Finance")
    # From before the first event, so none is dropped for lack of a day 0.
    start = min(e.date for e in events) - timedelta(days=30)
    prices = _history(provider, symbol, start)
    study = event_study(
        prices,
        events,
        horizon_days=horizon_days,
        anchor_day=anchor_day,
        day1_min=day1_min,
        day1_max=day1_max,
        surprise_min=surprise_min,
        surprise_max=surprise_max,
        since=since_date,
        levels_pct=levels_pct,
        include_all_events=include_all_events,
    )
    return _envelope(
        {
            "ticker": symbol,
            "horizon_days": horizon_days,
            "anchor_day": anchor_day,
            "next_earnings": _event_json(nxt),
        }
        | study
    )


@_tool
def implied_volatility(ticker: str, max_expiries: int = 8) -> dict:
    """Option-implied volatility term structure for the nearest max_expiries expiries.

    Per expiry: days (calendar), atm_iv (fraction, annualized, call/put average at the strike
    nearest spot), straddle_pct and expected_move_pct (percent of spot: the ATM straddle price and
    atm_iv * sqrt(days/365)). forward: vols (fractions) between the first expiry after the pending
    earnings date (event) and each later expiry, so they exclude the earnings move; ex_event_vol is
    the one ending nearest 30 days out. IVs are inverted at the regular-session price (spot),
    which the option quotes match, never an after-hours price.
    """
    symbol = _ticker(ticker)
    _check_at_least("max_expiries", max_expiries)
    provider = _data.get_provider()
    ts = _term_structure(provider, symbol, _today(), max_expiries=max_expiries)
    return _envelope(
        {
            "ticker": symbol,
            "spot": ts["spot"],
            "spot_source": "regular_session",
            "event": ts["event"],
            "expiries": ts["expiries"],
            "forward": ts["forward"],
            "ex_event_vol": ts["ex_event_vol"],
        }
    )


@_tool
def volatility_forecast(ticker: str, horizon_days: int = 20) -> dict:
    """Volatility estimates side by side, all annualized fractions (0.25 = 25%).

    realized: close-to-close over the last 10/20/60 sessions. garch: GARCH(1,1) forecast averaged
    over the next horizon_days sessions, fit on 5 years. implied: ex_event_vol (forward vol
    excluding the pending earnings move) and atm_iv_at_horizon (ATM IV of the expiry nearest the
    horizon); null without listed or usable options, with implied_note saying why. default_sigma / default_sigma_source: what price_target_odds
    and evaluate_sell_plan use when no vol is given.
    """
    symbol = _ticker(ticker)
    _check_at_least("horizon_days", horizon_days)
    provider = _data.get_provider()
    today = _today()
    start = today - timedelta(days=round(BOOTSTRAP_YEARS * 365.25) + 30)
    close = _history(provider, symbol, start)["Close"]
    horizon_calendar = math.ceil(horizon_days * 365 / TRADING_DAYS_PER_YEAR)

    implied = implied_note = None
    try:
        ts = _term_structure(
            provider,
            symbol,
            today,
            min_days=max(CHAIN_HORIZON_DAYS, horizon_calendar),
        )
    except ValueError as exc:  # no (usable) options: still report realized and GARCH
        implied_note = str(exc)
    else:
        nearest = min(ts["expiries"], key=lambda e: abs(e["days"] - horizon_calendar))
        implied = {
            "ex_event_vol": ts["ex_event_vol"],
            "event": ts["event"],
            "atm_iv_at_horizon": nearest["atm_iv"],
            "expiry_at_horizon": nearest["expiry"],
        }
    sigma, sigma_source = default_sigma(provider, symbol, today)
    return _envelope(
        {
            "ticker": symbol,
            "horizon_days": horizon_days,
            "realized": {f"{w}d": realized_vol(close, w) for w in REALIZED_WINDOWS},
            "garch": garch_forecast(close, horizon_days),
            "implied": implied,
            "implied_note": implied_note,
            "default_sigma": sigma,
            "default_sigma_source": sigma_source,
        }
    )


@_tool
def price_target_odds(
    ticker: str,
    levels: list[float],
    days: int = 20,
    method: str = "both",
    spot: float | None = None,
    vol: float | None = None,
    drift: float = 0.0,
    n_paths: int = 10000,
    vol_source: str = "options",
) -> dict:
    """Probability (fraction 0-1) that each price level is touched, or closed beyond, within days sessions.

    levels: absolute prices. method: lognormal (closed form), bootstrap (5 years of the stock's
    own daily moves, overnight gaps and intraday ranges), or both. spot defaults to the quote's
    spot (the newest pre/post-market price when later than the close). vol: annualized fraction;
    default is the ex-earnings option vol, else the nearest at-the-money option vol, else 20-day
    realized. vol_source: options (default; the bootstrap is rescaled to that same vol, so both
    methods agree on volatility) or history (the bootstrap keeps the 5-year history's own vol); a
    given vol rescales both either way. sigma / sigma_source: the lognormal vol (the bootstrap's
    when it runs alone); bootstrap.sigma / bootstrap.sigma_source: the bootstrap's. Sources:
    argument, options_ex_event, options_atm, realized_20d, or history_5y (vol_source=history).
    drift: annualized fraction (0 = no drift). Each row: level, pct_from_spot (percent),
    touch_<method> and close_beyond_<method> (close on the last day at or beyond the level, on its
    side of spot). days * n_paths must be at most 2,000,000 (larger requests are rejected).
    """
    symbol = _ticker(ticker)
    _check_levels(levels)
    _check_at_least("days", days)
    _check_at_least("n_paths", n_paths)
    _check_positive("spot", spot)
    _check_positive("vol", vol)
    _check_drift(drift)
    if method not in (*METHODS, "both"):
        raise ValueError(f"method must be lognormal, bootstrap or both, got {method!r}")
    _check_vol_source(vol_source)
    methods = METHODS if method == "both" else (method,)

    provider = _data.get_provider()
    today = _today()
    spot, spot_source = resolve_spot(provider.quote(symbol), spot)
    result: dict[str, Any] = {"ticker": symbol, "days": days, "method": method}
    columns: list[list[dict]] = []
    sigma = sigma_source = None
    if "bootstrap" in methods:
        pairs = _bootstrap_pairs(provider, symbol, today)
        boot_sigma, boot_source = _bootstrap_sigma(
            provider, symbol, today, vol, vol_source, pairs
        )
        paths = simulate(
            spot, days, sigma=boot_sigma, pairs=pairs, drift=drift, n_paths=n_paths
        )
        result["bootstrap"] = {
            "n_paths": n_paths,
            "sigma": boot_sigma,
            "sigma_source": boot_source,
            "overnight_share": overnight_share(pairs),
        }
        columns.append(_suffix(bootstrap_odds(paths, levels), "bootstrap"))
        sigma, sigma_source = boot_sigma, boot_source
    if "lognormal" in methods:
        sigma, sigma_source = _sigma(provider, symbol, today, vol)
        columns.insert(
            0, _suffix(lognormal_odds(spot, levels, sigma, days, drift), "lognormal")
        )
    rows = [
        {k: v for cols in row for k, v in cols.items()}
        for row in zip(*columns, strict=True)
    ]
    return _envelope(
        result
        | {
            "spot": spot,
            "spot_source": spot_source,
            "sigma": sigma,
            "sigma_source": sigma_source,
            "drift": drift,
            "levels": rows,
        }
    )


def _suffix(rows: list[dict], method: str) -> list[dict]:
    return [
        {
            "level": r["level"],
            "pct_from_spot": r["pct_from_spot"],
            f"touch_{method}": r["touch"],
            f"close_beyond_{method}": r["close_beyond"],
        }
        for r in rows
    ]


@_tool
def evaluate_sell_plan(
    ticker: str,
    shares: float,
    plans: list[dict[str, Any]],
    deadline_days: int = 20,
    method: str = "bootstrap",
    spot: float | None = None,
    vol: float | None = None,
    drift: float = 0.0,
    n_paths: int = 10000,
    vol_source: str = "options",
) -> dict:
    """Compare plans for selling `shares` over the next deadline_days sessions, on the same simulated paths.

    plans: [{"name": str, "tranches": [{"fraction", "type", ...}]}], fractions summing to 1 per
    plan. Types: market (sells at spot now); limit (price: sells when touched, or at a higher
    open); stop_limit (price = stop, limit_price <= stop: triggers at the stop, then rests as a
    limit at limit_price); bracket (take_profit and stop, optional limit_price <= stop; whichever
    hits first). Unfilled shares sell at the deadline close. method: bootstrap (5 years of daily
    moves) or lognormal; spot, vol (annualized fraction), vol_source (options or history; used by
    bootstrap) and drift (annualized fraction) as in price_target_odds; sigma / sigma_source: the
    vol the method used. Returns per plan proceeds and vs_sell_now (proceeds minus shares * spot)
    percentiles p5..p95, mean and std in currency, and per tranche fill_rate (fraction),
    mean_fill_price and mean_days_to_fill. spot_note (only when spot is a pre/post-market price):
    day-0 market fills are modelled at that price, while a real order fills at the next open.
    """
    symbol = _ticker(ticker)
    _check_positive("shares", shares)
    _check_at_least("deadline_days", deadline_days)
    _check_at_least("n_paths", n_paths)
    _check_positive("spot", spot)
    _check_positive("vol", vol)
    _check_drift(drift)
    if method not in METHODS:
        raise ValueError(f"method must be bootstrap or lognormal, got {method!r}")
    _check_vol_source(vol_source)
    if not plans:
        raise ValueError("plans must hold at least one plan")
    parsed = parse_plans(plans)

    provider = _data.get_provider()
    today = _today()
    spot, spot_source = resolve_spot(provider.quote(symbol), spot)
    pairs = _bootstrap_pairs(provider, symbol, today)
    if method == "bootstrap":
        sigma, sigma_source = _bootstrap_sigma(
            provider, symbol, today, vol, vol_source, pairs
        )
        paths = simulate(
            spot, deadline_days, sigma=sigma, pairs=pairs, drift=drift, n_paths=n_paths
        )
    else:
        sigma, sigma_source = _sigma(provider, symbol, today, vol)
        paths = simulate(
            spot,
            deadline_days,
            sigma=sigma,
            drift=drift,
            overnight=overnight_share(pairs),
            n_paths=n_paths,
        )
    result = {
        "ticker": symbol,
        "shares": shares,
        "deadline_days": deadline_days,
        "method": method,
        "n_paths": n_paths,
        "spot_source": spot_source,
        "sigma": sigma,
        "sigma_source": sigma_source,
        "drift": drift,
    }
    if spot_source in EXTENDED_HOURS_SOURCES:
        result["spot_note"] = EXTENDED_HOURS_NOTE
    return _envelope(result | evaluate_plans(paths, shares, parsed))
