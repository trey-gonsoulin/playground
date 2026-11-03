import math

import numpy as np
import pytest
from markets._odds import close_beyond_probability, lognormal_odds, touch_probability
from scipy.stats import norm


def test_zero_drift_matches_reflection_formula():
    s = 0.25 * math.sqrt(28 / 365)
    expected = 2 * (1 - norm.cdf(math.log(345 / 328.8) / s))
    # the reflection formula is the zero *log* drift case: drift = sigma**2 / 2 -> nu = 0
    assert touch_probability(
        328.8, 345.0, 0.25, 28 / 365, drift=0.25**2 / 2
    ) == pytest.approx(expected, rel=1e-9)


def test_level_at_spot_and_zero_vol_limits():
    assert touch_probability(100, 100, 0.3, 0.1) == 1.0
    assert close_beyond_probability(100, 100, 0.3, 0.1) == 1.0
    assert touch_probability(100, 110, 1e-9, 0.1) == pytest.approx(0.0, abs=1e-9)


def test_downside_mirrors_upside_for_zero_log_drift():  # drift = sigma**2/2 -> nu = 0
    up = touch_probability(100, 110, 0.3, 0.2, drift=0.045)
    dn = touch_probability(100, 100 / 1.1, 0.3, 0.2, drift=0.045)
    assert up == pytest.approx(dn, rel=1e-9)


def test_matches_fine_step_monte_carlo():
    sigma, drift, years, level = 0.3, 0.05, 20 / 252, 108.0
    n_paths, n_steps, chunk = 20_000, 1000, 5_000
    dt = years / n_steps
    rng = np.random.default_rng(0)
    # discrete monitoring misses crossings between steps; Broadie-Glasserman-Kou lowers the barrier to correct it
    barrier = math.log(level / 100) - 0.5826 * sigma * math.sqrt(dt)
    hits = 0
    for _ in range(n_paths // chunk):
        z = rng.standard_normal((chunk, n_steps))
        log_paths = np.cumsum(
            (drift - sigma**2 / 2) * dt + sigma * math.sqrt(dt) * z, axis=1
        )
        hits += int((log_paths.max(axis=1) >= barrier).sum())
    mc_estimate = hits / n_paths
    assert touch_probability(100, level, sigma, years, drift) == pytest.approx(
        mc_estimate, abs=0.01
    )


def test_lognormal_odds_both_sides():
    rows = lognormal_odds(100, [90, 100, 110], 0.3, 20)
    assert [r["touch"] == 1.0 for r in rows] == [False, True, False]
    assert rows[0]["close_beyond"] < rows[0]["touch"]
    assert rows[2]["close_beyond"] < rows[2]["touch"]
    assert set(rows[0]) == {"level", "pct_from_spot", "touch", "close_beyond"}
    assert rows[2]["pct_from_spot"] == 10.0
    assert rows[0]["pct_from_spot"] == -10.0
