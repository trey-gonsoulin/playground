import math

import numpy as np
import pandas as pd
import pytest
from markets._odds import bootstrap_odds, touch_probability
from markets._paths import overnight_share, simulate, split_returns


def fixture_pairs() -> np.ndarray:
    """~5 years of fat-tailed (Student-t, df=4) overnight/intraday log returns with a small nonzero mean."""
    rng = np.random.default_rng(0)
    t = rng.standard_t(4, size=(1250, 2)) / math.sqrt(2)  # df=4 has variance 2
    return t * [0.008, 0.013] + [0.0004, 0.0003]


def test_cap_rejected():
    with pytest.raises(ValueError, match="2,000,000"):
        simulate(100, 300, sigma=0.3, n_paths=10000)


def test_needs_sigma_or_pairs():
    with pytest.raises(ValueError, match="sigma or pairs"):
        simulate(100, 5)


def test_zero_drift_is_a_martingale_gbm_and_bootstrap():
    assert simulate(100, 20, sigma=0.3).close[:, -1].mean() == pytest.approx(
        100, rel=0.005
    )
    assert simulate(100, 20, pairs=fixture_pairs()).close[:, -1].mean() == (
        pytest.approx(100, rel=0.005)
    )


def test_positive_drift_grows_the_mean():
    p = simulate(100, 60, sigma=0.3, drift=0.5)
    assert p.close[:, -1].mean() == pytest.approx(
        100 * math.exp(0.5 * 60 / 252), rel=0.01
    )


def test_bootstrap_rescaled_to_sigma():
    p = simulate(100, 20, pairs=fixture_pairs(), sigma=0.25)
    assert np.log(p.close[:, -1] / 100).std() == pytest.approx(
        0.25 * math.sqrt(20 / 252), rel=0.03
    )


def test_gbm_overnight_share_of_variance():
    p = simulate(100, 1, sigma=0.3, overnight=0.4)
    gap = np.log(p.open[:, 0] / 100)
    assert gap.var() / np.log(p.close[:, 0] / 100).var() == pytest.approx(0.4, rel=0.05)


def test_bar_consistency():
    p = simulate(100, 10, sigma=0.3)
    assert p.close.shape == p.open.shape == p.high.shape == p.low.shape == (10000, 10)
    assert (p.high >= np.maximum(p.open, p.close)).all()
    assert (p.low <= np.minimum(p.open, p.close)).all()


def test_bridge_makes_touch_odds_match_closed_form():
    p = simulate(100, 20, sigma=0.3, overnight=0.0)
    got = bootstrap_odds(p, [108])[0]["touch"]
    assert got == pytest.approx(touch_probability(100, 108, 0.3, 20 / 252), abs=0.015)


def test_bootstrap_odds_rows():
    p = simulate(100, 20, sigma=0.3)
    up, at, down = bootstrap_odds(p, [110, 100, 92.5])
    assert up.keys() == {"level", "pct_from_spot", "touch", "close_beyond"}
    assert up["pct_from_spot"] == 10.0 and down["pct_from_spot"] == -7.5
    assert at["touch"] == at["close_beyond"] == 1.0
    assert up["touch"] == ((p.high >= 110).any(axis=1)).mean()
    assert up["close_beyond"] == (p.close[:, -1] >= 110).mean()
    assert down["touch"] == ((p.low <= 92.5).any(axis=1)).mean()
    assert down["close_beyond"] == (p.close[:, -1] <= 92.5).mean()
    assert 0 < up["close_beyond"] < up["touch"] < 1


def test_same_seed_same_paths():
    assert np.array_equal(
        simulate(100, 5, sigma=0.3).close, simulate(100, 5, sigma=0.3).close
    )
    assert not np.array_equal(
        simulate(100, 5, sigma=0.3).close, simulate(100, 5, sigma=0.3, seed=1).close
    )


def test_split_returns():
    history = pd.DataFrame(
        {"Open": [10.0, 11.0, 12.0], "Close": [10.5, 11.5, 11.0]},
        index=pd.date_range("2025-01-01", periods=3),
    )
    np.testing.assert_allclose(
        split_returns(history),
        [
            [math.log(11 / 10.5), math.log(11.5 / 11)],
            [math.log(12 / 11.5), math.log(11 / 12)],
        ],
    )


def test_overnight_share():
    pairs = fixture_pairs()
    expected = pairs[:, 0].var() / (pairs[:, 0].var() + pairs[:, 1].var())
    assert overnight_share(pairs) == pytest.approx(expected)
    assert 0.2 < overnight_share(pairs) < 0.35
    assert overnight_share(pairs[:59]) == 0.3
