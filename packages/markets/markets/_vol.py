"""Volatility: Black-Scholes implied vol, forward / ex-event vol from the option term structure, realized vol and GARCH."""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
from arch import arch_model
from scipy.optimize import brentq
from scipy.stats import norm

from markets._data import EarningsEvent, MarketData, resolve_spot
from markets._odds import TRADING_DAYS_PER_YEAR

IV_BOUNDS = (1e-4, 5.0)
GARCH_YEARS = 5
TARGET_DAYS = 30  # ex-event vol and the fallback ATM IV are read at this horizon
CHAIN_HORIZON_DAYS = 45  # default_sigma fetches chains up to the first expiry past this


def bs_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    div_yield: float,
    sigma: float,
    call: bool,
) -> float:
    """Black-Scholes-Merton price with a continuous dividend yield."""
    stdev = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - div_yield + sigma**2 / 2) * years) / stdev
    d2 = d1 - stdev
    disc_spot = spot * math.exp(-div_yield * years)
    disc_strike = strike * math.exp(-rate * years)
    if call:
        return float(disc_spot * norm.cdf(d1) - disc_strike * norm.cdf(d2))
    return float(disc_strike * norm.cdf(-d2) - disc_spot * norm.cdf(-d1))


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    div_yield: float,
    call: bool,
) -> float | None:
    """The sigma that reproduces `price`, or None when none lies in [1e-4, 5]."""
    if years <= 0 or price <= 0:
        return None

    def gap(sigma: float) -> float:
        return bs_price(spot, strike, years, rate, div_yield, sigma, call) - price

    low, high = IV_BOUNDS
    if gap(low) * gap(high) > 0:
        return None
    return float(brentq(gap, low, high, xtol=1e-10))


SOURCES = ("mid", "last", "yahoo")  # strongest to weakest
YAHOO_IV_RANGE = (
    0.01,
    3.0,
)  # outside this, Yahoo's column is a placeholder, not a quote


def _side(
    chain: pd.DataFrame,
    strike: float,
    spot: float,
    years: float,
    rate: float,
    div_yield: float,
    call: bool,
) -> tuple[float, float | None, str] | None:
    """(iv, price, source) of one option, or None when no source gives a usable IV.

    Sources, in order: the bid/ask mid, the last trade price, Yahoo's own
    impliedVolatility (only inside [0.01, 3.0]; it has no price).
    """
    row = chain.loc[chain["strike"] == strike].iloc[0]
    candidates = []
    if row["bid"] > 0 and row["ask"] > row["bid"]:
        candidates.append(("mid", float((row["bid"] + row["ask"]) / 2)))
    if row["lastPrice"] > 0:
        candidates.append(("last", float(row["lastPrice"])))
    for source, price in candidates:
        iv = implied_vol(price, spot, strike, years, rate, div_yield, call)
        if iv is not None:
            return iv, price, source
    yahoo = float(row["impliedVolatility"])
    if YAHOO_IV_RANGE[0] <= yahoo <= YAHOO_IV_RANGE[1]:
        return yahoo, None, "yahoo"
    return None


def atm(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    years: float,
    rate: float,
    div_yield: float,
) -> dict:
    """The at-the-money straddle: the nearest common strike where a call or put has a usable IV.

    `iv_source` is the weakest source of the sides used and `straddle_mid` sums
    their prices. Everything is None when no common strike has a usable side.
    """
    strikes = np.intersect1d(calls["strike"].to_numpy(), puts["strike"].to_numpy())
    for nearest in strikes[np.argsort(np.abs(strikes - spot), kind="stable")]:
        strike = float(nearest)
        call = _side(calls, strike, spot, years, rate, div_yield, True)
        put = _side(puts, strike, spot, years, rate, div_yield, False)
        sides = [side for side in (call, put) if side is not None]
        if not sides:
            continue
        prices = [side[1] for side in sides]
        both = len(sides) == 2 and None not in prices
        return {
            "strike": strike,
            "iv": sum(side[0] for side in sides) / len(sides),
            "call_iv": call[0] if call else None,
            "put_iv": put[0] if put else None,
            "straddle_mid": sum(prices) if both else None,
            "iv_source": max((side[2] for side in sides), key=SOURCES.index),
        }
    return {
        "strike": None,
        "iv": None,
        "call_iv": None,
        "put_iv": None,
        "straddle_mid": None,
        "iv_source": None,
    }


def forward_vol(sigma1: float, t1: float, sigma2: float, t2: float) -> float | None:
    """Vol implied for (t1, t2] by total variances sigma1^2 t1 and sigma2^2 t2."""
    if t2 <= t1:
        return None
    variance = (sigma2**2 * t2 - sigma1**2 * t1) / (t2 - t1)
    return math.sqrt(variance) if variance > 0 else None


def pending_event(
    events: list[EarningsEvent], nxt: EarningsEvent | None, last_close: date
) -> date | None:
    """The earnings date the option quotes have not yet reacted to."""
    for event in events:
        if event.date == last_close and event.timing == "after_close":
            return event.date
    return nxt.date if nxt else None


def term_structure(
    chains: list[tuple[date, pd.DataFrame, pd.DataFrame]],
    spot: float,
    today: date,
    rate: float,
    div_yield: float,
    event: date | None,
) -> dict:
    expiries = []
    for expiry, calls, puts in chains:
        days = (expiry - today).days
        if days <= 0:
            continue
        years = days / 365
        mid = atm(calls, puts, spot, years, rate, div_yield)
        if mid["iv"] is None:
            continue
        straddle = mid["straddle_mid"]
        expiries.append(
            {
                "expiry": expiry,
                "days": days,
                "atm_iv": mid["iv"],
                "straddle_pct": None if straddle is None else straddle / spot * 100,
                "expected_move_pct": mid["iv"] * math.sqrt(years) * 100,
            }
        )

    # Forward vols start at the first expiry after the event, so they exclude its variance.
    start = next(
        (
            i
            for i, e in enumerate(expiries)
            if event is not None and e["expiry"] > event
        ),
        0,
    )
    forward = []
    if expiries:
        first = expiries[start]
        for later in expiries[start + 1 :]:
            vol = forward_vol(
                first["atm_iv"],
                first["days"] / 365,
                later["atm_iv"],
                later["days"] / 365,
            )
            if vol is not None:
                forward.append(
                    {"from": first["expiry"], "to": later["expiry"], "vol": vol}
                )

    ex_event = None
    if forward:
        target = today + timedelta(days=TARGET_DAYS)
        ex_event = min(forward, key=lambda f: abs((f["to"] - target).days))["vol"]
    return {"expiries": expiries, "forward": forward, "ex_event_vol": ex_event}


def _log_returns(close: pd.Series) -> pd.Series:
    return np.log(close.astype(float)).diff().dropna()


def realized_vol(close: pd.Series, window: int) -> float:
    """Annualized standard deviation of the last `window` daily log returns."""
    returns = _log_returns(close).iloc[-window:]
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def garch_forecast(close: pd.Series, horizon_days: int) -> float:
    """Annualized vol averaged over the next `horizon_days`: GARCH(1,1), Student-t, last 5 years."""
    returns = _log_returns(close)
    returns = returns[
        returns.index > returns.index[-1] - pd.DateOffset(years=GARCH_YEARS)
    ]
    # Returns in percent keep the optimizer well scaled; rescale=False stops arch rescaling again.
    fit = arch_model(
        returns * 100, mean="Constant", vol="GARCH", p=1, q=1, dist="t", rescale=False
    ).fit(disp="off")
    variance = fit.forecast(horizon=horizon_days).variance.iloc[-1].mean()
    return float(math.sqrt(variance * TRADING_DAYS_PER_YEAR) / 100)


def default_sigma(provider: MarketData, ticker: str, today: date) -> tuple[float, str]:
    """The volatility to simulate with and where it came from."""
    history = provider.history(ticker, today - timedelta(days=400))
    last_close = history.index[-1].date()
    expiries = provider.option_expiries(ticker)
    if expiries:
        quote = provider.quote(ticker)
        spot, _ = resolve_spot(quote)
        events, nxt = provider.earnings(ticker)
        chains = []
        for expiry in expiries:
            calls, puts = provider.option_chain(ticker, expiry)
            chains.append((expiry, calls, puts))
            if (expiry - today).days >= CHAIN_HORIZON_DAYS:
                break
        ts = term_structure(
            chains,
            spot,
            today,
            provider.rate(),
            quote.dividend_yield,
            pending_event(events, nxt, last_close),
        )
        if ts["ex_event_vol"] is not None:
            return ts["ex_event_vol"], "options_ex_event"
        if ts["expiries"]:
            nearest = min(ts["expiries"], key=lambda e: abs(e["days"] - TARGET_DAYS))
            return nearest["atm_iv"], "options_atm"
    return realized_vol(history["Close"], 20), "realized_20d"
