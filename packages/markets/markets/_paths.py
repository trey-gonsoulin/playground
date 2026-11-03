"""Daily open/high/low/close price paths: a block bootstrap of history, or GBM, with Brownian-bridge extremes."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from markets._odds import TRADING_DAYS_PER_YEAR

MAX_CELLS = 2_000_000  # days * n_paths
MEAN_BLOCK_DAYS = 5
DEFAULT_OVERNIGHT_SHARE = 0.3
MIN_ROWS_FOR_SHARE = 60


@dataclass(frozen=True)
class Paths:
    """Simulated daily bars; each array is (n_paths, days)."""

    spot: float
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


def split_returns(history: pd.DataFrame) -> np.ndarray:
    """(N, 2) log returns per day after the first: [overnight open/prev close, intraday close/open]."""
    opens = history["Open"].to_numpy(dtype=float)
    closes = history["Close"].to_numpy(dtype=float)
    pairs = np.column_stack(
        [np.log(opens[1:] / closes[:-1]), np.log(closes[1:] / opens[1:])]
    )
    return pairs[np.isfinite(pairs).all(axis=1)]


def overnight_share(pairs: np.ndarray) -> float:
    """Overnight share of daily variance; the default 0.3 when there are fewer than 60 rows."""
    if len(pairs) < MIN_ROWS_FOR_SHARE:
        return DEFAULT_OVERNIGHT_SHARE
    overnight, intraday = pairs.var(axis=0)
    return float(overnight / (overnight + intraday))


def _block_bootstrap(
    pairs: np.ndarray, n_paths: int, days: int, rng: np.random.Generator
) -> np.ndarray:
    """Stationary block bootstrap: (n_paths, days, 2) rows of `pairs`, blocks of mean length 5, wrapping."""
    n = len(pairs)
    restart = rng.random((n_paths, days)) < 1 / MEAN_BLOCK_DAYS
    fresh = rng.integers(0, n, size=(n_paths, days))
    idx = np.empty((n_paths, days), dtype=np.int64)
    idx[:, 0] = fresh[:, 0]
    for t in range(1, days):
        idx[:, t] = np.where(restart[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    return pairs[idx]


def _bridge_extreme(
    a: np.ndarray, b: np.ndarray, v: float, sign: int, rng: np.random.Generator
) -> np.ndarray:
    """Sample the max (sign=1) or min (sign=-1) of a Brownian bridge from a to b with variance v."""
    u = 1 - rng.random(a.shape)  # in (0, 1], so log(u) is finite
    return (a + b + sign * np.sqrt((b - a) ** 2 - 2 * v * np.log(u))) / 2


def simulate(
    spot: float,
    days: int,
    *,
    sigma: float | None = None,
    pairs: np.ndarray | None = None,
    drift: float = 0.0,
    overnight: float = DEFAULT_OVERNIGHT_SHARE,
    n_paths: int = 10000,
    seed: int = 0,
) -> Paths:
    """Simulate `n_paths` paths of `days` daily bars from `spot`.

    With `pairs` (from `split_returns`), block-bootstrap the demeaned history, rescaled to annual vol
    `sigma` when given. Otherwise GBM with annual vol `sigma`, `overnight` of the variance in the gap.
    `drift` is the annual arithmetic drift: E[close] = spot * e^(drift * t).
    """
    if days < 1 or n_paths < 1:
        raise ValueError("days and n_paths must be at least 1")
    if days * n_paths > MAX_CELLS:
        raise ValueError(f"days * n_paths must be at most {MAX_CELLS:,}")
    if sigma is None and pairs is None:
        raise ValueError("simulate needs sigma or pairs")
    rng = np.random.default_rng(seed)
    if pairs is not None:
        if len(pairs) < 2:
            raise ValueError("pairs needs at least 2 rows")
        centered = pairs - pairs.mean(axis=0)
        if sigma is not None:
            daily_sd = centered.sum(axis=1).std()
            centered = centered * (sigma / (daily_sd * np.sqrt(TRADING_DAYS_PER_YEAR)))
        steps = _block_bootstrap(centered, n_paths, days, rng)
        var_on, var_id = centered.var(axis=0)
        daily_var = centered.sum(axis=1).var()
        share = var_on / (var_on + var_id)
    else:
        daily_var = sigma**2 / TRADING_DAYS_PER_YEAR
        var_on, var_id = overnight * daily_var, (1 - overnight) * daily_var
        steps = rng.standard_normal((n_paths, days, 2)) * np.sqrt([var_on, var_id])
        share = overnight
    log_drift = drift / TRADING_DAYS_PER_YEAR - daily_var / 2
    gap = steps[..., 0] + share * log_drift
    session = steps[..., 1] + (1 - share) * log_drift

    log_close = np.log(spot) + np.cumsum(gap + session, axis=1)
    log_open = log_close - session
    log_high = _bridge_extreme(log_open, log_close, var_id, 1, rng)
    log_low = _bridge_extreme(log_open, log_close, var_id, -1, rng)
    return Paths(
        spot=float(spot),
        open=np.exp(log_open),
        high=np.exp(log_high),
        low=np.exp(log_low),
        close=np.exp(log_close),
    )
