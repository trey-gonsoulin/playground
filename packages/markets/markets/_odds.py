"""Closed-form lognormal (geometric Brownian motion) odds of reaching or finishing beyond a price level."""

import math

from scipy.stats import norm

TRADING_DAYS_PER_YEAR = 252
_MIN_STDEV = 1e-12


def _barrier_terms(spot: float, level: float, sigma: float, years: float, drift: float):
    """Return (b, nu, stdev) oriented so the level is above spot, or None when level == spot."""
    b = math.log(level / spot)
    nu = drift - sigma**2 / 2
    if b == 0:
        return None
    if b < 0:
        b, nu = -b, -nu
    return b, nu, sigma * math.sqrt(years)


def touch_probability(
    spot: float, level: float, sigma: float, years: float, drift: float = 0.0
) -> float:
    """P(price reaches `level` at any time within `years`), for GBM with annual vol `sigma` and `drift`."""
    terms = _barrier_terms(spot, level, sigma, years, drift)
    if terms is None:
        return 1.0
    b, nu, stdev = terms
    if stdev <= _MIN_STDEV:
        return 0.0
    first = norm.cdf((-b + nu * years) / stdev)
    second = math.exp(min(2 * nu * b / sigma**2, 700.0)) * norm.cdf(
        (-b - nu * years) / stdev
    )
    return min(1.0, float(first + second))


def close_beyond_probability(
    spot: float, level: float, sigma: float, years: float, drift: float = 0.0
) -> float:
    """P(price at `years` is at or beyond `level`): >= level above spot, <= level below, 1.0 at spot."""
    terms = _barrier_terms(spot, level, sigma, years, drift)
    if terms is None:
        return 1.0
    b, nu, stdev = terms
    if stdev <= _MIN_STDEV:
        return 0.0
    return float(norm.cdf((-b + nu * years) / stdev))


def lognormal_odds(
    spot: float, levels: list[float], sigma: float, days: int, drift: float = 0.0
) -> list[dict]:
    """Touch and terminal odds for each level over `days` trading days (252 per year)."""
    years = days / TRADING_DAYS_PER_YEAR
    return [
        {
            "level": level,
            "pct_from_spot": round((level / spot - 1) * 100, 2),
            "touch": touch_probability(spot, level, sigma, years, drift),
            "close_beyond": close_beyond_probability(spot, level, sigma, years, drift),
        }
        for level in levels
    ]
