import json

import numpy as np
import pytest
from markets._paths import Paths, simulate
from markets._plans import NOTE, evaluate_plans, parse_plans

SHARES = 10
SPOT = 100.0
LADDER = {
    "name": "ladder",
    "tranches": [
        {"fraction": 0.25, "type": "market"},
        {"fraction": 0.25, "type": "limit", "price": 105},
        {"fraction": 0.25, "type": "limit", "price": 110},
        {"fraction": 0.25, "type": "stop_limit", "price": 92, "limit_price": 90},
    ],
}


def bars(*paths: list[tuple[float, float, float, float]]) -> Paths:
    """Paths from per-path lists of (open, high, low, close) bars."""
    a = np.array(paths, dtype=float)  # (n_paths, days, 4)
    return Paths(
        spot=SPOT, open=a[..., 0], high=a[..., 1], low=a[..., 2], close=a[..., 3]
    )


def evaluate(paths: Paths, *tranches: dict) -> dict:
    res = evaluate_plans(
        paths, SHARES, parse_plans([{"name": "p", "tranches": list(tranches)}])
    )
    return res["plans"][0]


FLAT = bars([(100, 101, 99, 100), (100, 101, 99, 100)])


def test_market_sells_at_spot():
    plan = evaluate(FLAT, {"fraction": 1, "type": "market"})
    assert plan["proceeds"] == {
        "p5": 1000.0,
        "p25": 1000.0,
        "p50": 1000.0,
        "p75": 1000.0,
        "p95": 1000.0,
        "mean": 1000.0,
        "std": 0.0,
    }
    assert plan["vs_sell_now"]["mean"] == 0.0
    assert plan["tranches"] == [
        {
            "type": "market",
            "fill_rate": 1.0,
            "mean_fill_price": 100.0,
            "mean_days_to_fill": 0.0,
        }
    ]


def test_limit_fills_at_price_or_gap_open():
    paths = bars(
        [
            (100, 106, 99, 104),
            (104, 104, 101, 102),
            (102, 103, 100, 101),
        ],  # day 1 high 106 -> 105
        [
            (101, 103, 99, 102),
            (107, 108, 106, 107),
            (107, 108, 105, 106),
        ],  # day 2 opens 107 -> 107
        [
            (100, 101, 97, 98),
            (98, 99, 96, 97),
            (97, 99, 96, 98),
        ],  # never -> deadline close 98
    )
    plan = evaluate(paths, {"fraction": 1, "type": "limit", "price": 105})
    assert plan["proceeds"]["mean"] == pytest.approx((1050 + 1070 + 980) / 3)
    assert plan["proceeds"]["p50"] == 1050.0
    assert plan["vs_sell_now"]["mean"] == pytest.approx(100 / 3)
    assert plan["tranches"] == [
        {
            "type": "limit",
            "fill_rate": pytest.approx(2 / 3),
            "mean_fill_price": 106.0,
            "mean_days_to_fill": 1.5,
        }
    ]


STOP_LIMIT = {"fraction": 1, "type": "stop_limit", "price": 95, "limit_price": 93}


def test_stop_limit_gap_below_limit_rests_then_fills_on_recovery():
    paths = bars(
        # opens 90 below the limit, rests at 93; day 3 high 93.5 -> fills 93
        [(100, 101, 97, 98), (90, 92, 89, 91), (91, 93.5, 90, 92)],
        # touched intraday on day 1 -> fills at the stop
        [(100, 101, 94, 96), (96, 97, 95, 96), (96, 97, 95, 96)],
        # gaps to 94 on day 2, still above the limit -> fills at the open
        [(100, 100.5, 99, 99.5), (94, 96, 93.8, 95), (95, 96, 94, 95)],
    )
    plan = evaluate(paths, STOP_LIMIT)
    assert plan["proceeds"]["mean"] == pytest.approx((930 + 950 + 940) / 3)
    assert plan["tranches"] == [
        {
            "type": "stop_limit",
            "fill_rate": 1.0,
            "mean_fill_price": pytest.approx(94.0),
            "mean_days_to_fill": pytest.approx(2.0),
        }
    ]


def test_stop_limit_rests_and_fills_the_same_day():
    paths = bars([(100, 101, 97, 98), (90, 93.2, 89, 91), (91, 92, 90, 91)])
    plan = evaluate(paths, STOP_LIMIT)
    assert plan["proceeds"]["mean"] == 930.0
    assert plan["tranches"][0]["mean_days_to_fill"] == 2.0


def test_resting_stop_limit_fills_at_the_open_when_it_gaps_above_the_limit():
    paths = bars([(100, 101, 97, 98), (90, 92, 89, 91), (96, 97, 95, 96)])
    plan = evaluate(paths, STOP_LIMIT)
    assert plan["tranches"][0]["mean_fill_price"] == 96.0
    assert plan["tranches"][0]["mean_days_to_fill"] == 3.0


def test_stop_limit_gap_below_limit_never_recovers_sells_at_deadline_close():
    paths = bars([(100, 101, 97, 98), (90, 92, 89, 91), (91, 92.5, 88, 89)])
    plan = evaluate(paths, STOP_LIMIT)
    assert plan["proceeds"]["mean"] == 890.0
    assert plan["vs_sell_now"]["mean"] == -110.0
    assert plan["tranches"] == [
        {
            "type": "stop_limit",
            "fill_rate": 0.0,
            "mean_fill_price": None,
            "mean_days_to_fill": None,
        }
    ]


BRACKET = {
    "fraction": 1,
    "type": "bracket",
    "take_profit": 110,
    "stop": 95,
    "limit_price": 93,
}


def test_bracket_same_day_both_touched_stop_wins():
    paths = bars([(100, 111, 94, 100), (100, 101, 99, 100)])
    plan = evaluate(paths, BRACKET)
    assert plan["proceeds"]["mean"] == 950.0
    assert plan["tranches"][0]["mean_fill_price"] == 95.0
    assert plan["tranches"][0]["mean_days_to_fill"] == 1.0


def test_bracket_one_cancels_other():
    paths = bars(
        # take profit fills on day 1; the day 2 crash never fills the stop
        [(100, 111, 99, 105), (104, 105, 80, 82)],
        # gaps above the take profit -> fills at the open
        [(112, 113, 111, 112), (112, 113, 111, 112)],
        # gaps below the limit -> rests at 93; the stop leg's trigger cancels the take profit
        [(90, 92, 89, 91), (91, 111, 90, 100)],
    )
    plan = evaluate(paths, BRACKET)
    assert plan["proceeds"]["p50"] == 1100.0
    assert plan["proceeds"]["p5"] == pytest.approx(947.0)  # 930 + 0.1 * (1100 - 930)
    assert plan["proceeds"]["mean"] == pytest.approx((1100 + 1120 + 930) / 3)
    assert plan["tranches"] == [
        {
            "type": "bracket",
            "fill_rate": 1.0,
            "mean_fill_price": pytest.approx(105.0),
            "mean_days_to_fill": pytest.approx(4 / 3),
        }
    ]


def test_bracket_without_limit_price_fills_a_gap_down_at_the_open():
    paths = bars([(90, 92, 89, 91), (91, 92, 90, 91)])
    plan = evaluate(
        paths, {"fraction": 1, "type": "bracket", "take_profit": 110, "stop": 95}
    )
    assert plan["tranches"][0]["mean_fill_price"] == 90.0
    assert plan["tranches"][0]["mean_days_to_fill"] == 1.0


def test_tranches_weighted_by_fraction_and_result_is_json():
    paths = bars([(100, 106, 99, 104), (104, 104, 101, 102)])
    res = evaluate_plans(
        paths,
        SHARES,
        parse_plans(
            [
                {
                    "name": "half",
                    "tranches": [
                        {"fraction": 0.5, "type": "market"},
                        {"fraction": 0.5, "type": "limit", "price": 105},
                    ],
                }
            ]
        ),
    )
    assert res["spot"] == 100.0
    assert res["sell_now"] == 1000.0
    assert res["note"] == NOTE
    assert res["plans"][0]["name"] == "half"
    assert res["plans"][0]["proceeds"]["mean"] == 1025.0
    assert [t["type"] for t in res["plans"][0]["tranches"]] == ["market", "limit"]
    assert json.loads(json.dumps(res, allow_nan=False)) == res


def test_fractions_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        parse_plans(
            [
                {
                    "name": "p",
                    "tranches": [
                        {"fraction": 0.5, "type": "market"},
                        {"fraction": 0.4, "type": "limit", "price": 105},
                    ],
                }
            ]
        )


@pytest.mark.parametrize(
    ("tranche", "match"),
    [
        ({"fraction": 1, "type": "limit"}, "price"),
        ({"fraction": 1, "type": "stop_limit", "price": 95}, "limit_price"),
        (
            {"fraction": 1, "type": "stop_limit", "price": 95, "limit_price": 96},
            "limit_price",
        ),
        ({"fraction": 1, "type": "bracket", "take_profit": 110}, "stop"),
        ({"fraction": 1, "type": "bracket", "stop": 95}, "take_profit"),
        (
            {"fraction": 1, "type": "bracket", "take_profit": 95, "stop": 95},
            "take_profit",
        ),
        (
            {
                "fraction": 1,
                "type": "bracket",
                "take_profit": 110,
                "stop": 95,
                "limit_price": 96,
            },
            "limit_price",
        ),
        ({"fraction": 1, "type": "trailing_stop", "price": 95}, "type"),
        ({"fraction": 1, "type": "limit", "price": 0}, "positive"),
        ({"fraction": 1, "type": "limit", "price": 105, "colour": "red"}, "colour"),
        ({"fraction": 0, "type": "market"}, "fraction"),
    ],
)
def test_invalid_tranches_rejected(tranche, match):
    with pytest.raises(ValueError, match=match):
        parse_plans([{"name": "p", "tranches": [tranche]}])


def test_plan_needs_a_name_and_tranches():
    with pytest.raises(ValueError, match="name"):
        parse_plans([{"tranches": [{"fraction": 1, "type": "market"}]}])
    with pytest.raises(ValueError, match="at least one tranche"):
        parse_plans([{"name": "p", "tranches": []}])


def test_zero_drift_mean_close_to_sell_now():
    paths = simulate(100, 20, sigma=0.3, n_paths=20000)
    res = evaluate_plans(paths, 100, parse_plans([LADDER]))
    assert abs(res["plans"][0]["vs_sell_now"]["mean"]) < 0.005 * 100 * 100
