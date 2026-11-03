"""The MCP tools, called directly against the recorded AAPL fixtures (no network)."""

import json
import math
from datetime import datetime

import pandas as pd
import pytest
from fixture_data import FakeProvider, fixture_meta
from markets._data import CHAIN_COLUMNS, DataSourceError
from markets._vol import realized_vol
from markets.mcp_server import (
    MAX_BARS,
    earnings_event_study,
    evaluate_sell_plan,
    implied_volatility,
    price_history,
    price_target_odds,
    quote,
    volatility_forecast,
)

pytestmark = pytest.mark.usefixtures("fake_provider")

META = fixture_meta()
LADDER = {
    "name": "ladder",
    "tranches": [
        {"fraction": 0.25, "type": "market"},
        {"fraction": 0.25, "type": "limit", "price": 345},
        {"fraction": 0.25, "type": "limit", "price": 355},
        {"fraction": 0.25, "type": "stop_limit", "price": 310, "limit_price": 305},
    ],
}
SELL_NOW = {"name": "sell now", "tranches": [{"fraction": 1, "type": "market"}]}


def _check_envelope(result: dict) -> None:
    json.dumps(result, allow_nan=False)
    assert result["source"] == "yahoo"
    assert datetime.fromisoformat(result["as_of"]).utcoffset().total_seconds() == 0


def test_quote_reports_spot_source():
    res = quote("aapl")
    _check_envelope(res)
    assert res["ticker"] == "AAPL"
    assert res["spot_source"] == "post_market"
    assert res["spot"] == META["spot_after_hours"]
    assert res["last"] == META["spot"]
    assert res["next_earnings"]["date"] == "2027-01-29"


def test_quote_without_after_hours_uses_last(fake_provider):
    fake_provider.post_price = None
    res = quote("AAPL")
    assert (res["spot"], res["spot_source"]) == (META["spot"], "last")


def test_price_history_keeps_newest_bars():
    res = price_history("AAPL", period="max")
    _check_envelope(res)
    bars = res["bars"]
    assert len(bars) == MAX_BARS
    assert res["truncated"] is True
    assert bars[-1]["date"] == "2026-11-02"
    assert set(bars[-1]) == {"date", "open", "high", "low", "close", "volume"}


@pytest.mark.parametrize("period", ["6 months", "0mo", "forever"])
def test_price_history_rejects_bad_period(period):
    with pytest.raises(ValueError, match="period"):
        price_history("AAPL", period=period)


def test_price_history_rejects_bad_interval():
    with pytest.raises(ValueError, match="interval"):
        price_history("AAPL", interval="7m")


def test_empty_history_names_the_ticker(fake_provider, monkeypatch):
    monkeypatch.setattr(
        fake_provider, "history", lambda *a, **k: FakeProvider().history("X", None)[:0]
    )
    with pytest.raises(ValueError, match="no price history for AAPL"):
        price_history("AAPL")


def test_earnings_event_study():
    res = earnings_event_study("AAPL", horizon_days=10, levels_pct=[3, 5])
    _check_envelope(res)
    assert res["n_events"] > 15
    assert res["dropped_events"] == 1  # the Nov 2 2026 report has no window yet
    assert [h["pct"] for h in res["hit_rates"]] == [3, 5]
    assert res["next_earnings"]["date"] == "2027-01-29"


def test_earnings_event_study_since_filter():
    res = earnings_event_study("AAPL", since="2025-01-01")
    assert all(e["date"] >= "2025-01-01" for e in res["events"])
    with pytest.raises(ValueError, match="since"):
        earnings_event_study("AAPL", since="last year")


def test_no_earnings_is_clear_error():
    with pytest.raises(ValueError, match="no earnings history"):
        earnings_event_study("SPYX")


def test_implied_volatility_inverts_at_regular_price():
    res = implied_volatility("AAPL")
    _check_envelope(res)
    assert res["spot"] == META["spot"]
    assert res["spot_source"] == "regular_session"
    assert res["event"] == "2026-11-02"
    assert len(res["expiries"]) == 6
    assert res["expiries"][0]["expiry"] == "2026-11-04"
    assert 0.1 < res["ex_event_vol"] < 1.0
    assert implied_volatility("AAPL", max_expiries=2)["expiries"][1]["days"] == 4


def test_no_options_is_clear_error():  # fake provider with no expiries (ETF-like)
    with pytest.raises(ValueError, match="no listed options"):
        implied_volatility("SPYX")


def test_volatility_forecast_side_by_side():
    res = volatility_forecast("AAPL", horizon_days=20)
    _check_envelope(res)
    assert set(res["realized"]) == {"10d", "20d", "60d"}
    assert 0.05 < res["garch"] < 1.0
    assert res["implied"]["ex_event_vol"] == pytest.approx(
        implied_volatility("AAPL")["ex_event_vol"]
    )
    assert res["default_sigma_source"] == "options_ex_event"


def test_volatility_forecast_without_options():
    res = volatility_forecast("SPYX")
    assert res["implied"] is None
    assert "no listed options" in res["implied_note"]
    assert res["default_sigma_source"] == "realized_20d"


def test_volatility_forecast_degrades_when_option_quotes_unusable(
    fake_provider, monkeypatch
):
    # Expiries are listed but every chain is empty: no ATM IV anywhere.
    empty = pd.DataFrame(columns=CHAIN_COLUMNS)
    monkeypatch.setattr(fake_provider, "option_chain", lambda t, e: (empty, empty))
    res = volatility_forecast("AAPL")
    _check_envelope(res)
    assert res["implied"] is None
    assert "no usable option quotes for AAPL" in res["implied_note"]
    assert set(res["realized"]) == {"10d", "20d", "60d"}
    assert 0.05 < res["garch"] < 1.0
    assert res["default_sigma_source"] == "realized_20d"
    # The dedicated tool still raises its clear error.
    with pytest.raises(ValueError, match="no usable option quotes"):
        implied_volatility("AAPL")


def test_price_target_odds_both_methods_agree_roughly():
    res = price_target_odds("AAPL", [342, 350, 310], days=20)
    _check_envelope(res)
    rows = res["levels"]
    assert all(abs(r["touch_lognormal"] - r["touch_bootstrap"]) < 0.12 for r in rows)
    assert (res["spot"], res["spot_source"]) == (
        META["spot_after_hours"],
        "post_market",
    )
    assert res["sigma_source"] == "options_ex_event"
    assert res["bootstrap"]["sigma_source"] == "history_5y"
    assert 0 < res["bootstrap"]["overnight_share"] < 1


def test_price_target_odds_vol_argument_rescales_bootstrap():
    res = price_target_odds("AAPL", [350], vol=0.3, spot=340)
    assert (res["sigma"], res["sigma_source"]) == (0.3, "argument")
    assert (res["spot"], res["spot_source"]) == (340, "argument")
    assert res["bootstrap"]["sigma"] == 0.3
    row = res["levels"][0]
    assert abs(row["touch_lognormal"] - row["touch_bootstrap"]) < 0.05


def test_price_target_odds_single_method_columns():
    lognormal = price_target_odds("AAPL", [350], method="lognormal")["levels"][0]
    assert set(lognormal) == {
        "level",
        "pct_from_spot",
        "touch_lognormal",
        "close_beyond_lognormal",
    }
    res = price_target_odds("AAPL", [350], method="bootstrap")
    assert set(res["levels"][0]) == {
        "level",
        "pct_from_spot",
        "touch_bootstrap",
        "close_beyond_bootstrap",
    }
    assert res["sigma_source"] == "history_5y"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"levels": []}, "levels"),
        ({"levels": [0]}, "levels"),
        ({"levels": [math.nan]}, "levels"),
        ({"levels": [math.inf]}, "levels"),
        ({"days": 0}, "days"),
        ({"vol": 0}, "vol"),
        ({"spot": -1}, "spot"),
        ({"n_paths": 0}, "n_paths"),
        ({"drift": math.nan}, "drift"),
        ({"method": "magic"}, "method"),
        ({"days": 300, "n_paths": 10000}, "2,000,000"),
    ],
)
def test_price_target_odds_validation(kwargs, match):
    with pytest.raises(ValueError, match=match):
        price_target_odds("AAPL", **{"levels": [350], **kwargs})


def test_evaluate_sell_plan_shape():
    res = evaluate_sell_plan("AAPL", 100, [LADDER, SELL_NOW])
    _check_envelope(res)
    assert len(res["plans"]) == 2
    assert res["spot_source"] == "post_market"
    assert res["sell_now"] == pytest.approx(100 * META["spot_after_hours"])
    sell_now = res["plans"][1]
    assert sell_now["vs_sell_now"]["std"] == pytest.approx(0, abs=1e-9)
    assert res["note"]


def test_evaluate_sell_plan_lognormal_uses_default_sigma():
    res = evaluate_sell_plan("AAPL", 10, [SELL_NOW], method="lognormal", n_paths=500)
    assert res["sigma_source"] == "options_ex_event"
    assert res["method"] == "lognormal"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"shares": 0}, "shares"),
        ({"deadline_days": 0}, "deadline_days"),
        ({"method": "both"}, "method"),
        (
            {
                "plans": [
                    {"name": "x", "tranches": [{"fraction": 0.5, "type": "market"}]}
                ]
            },
            "sum to 1",
        ),
        ({"plans": []}, "plans"),
    ],
)
def test_evaluate_sell_plan_validation(kwargs, match):
    args = {"ticker": "AAPL", "shares": 10, "plans": [SELL_NOW], **kwargs}
    with pytest.raises(ValueError, match=match):
        evaluate_sell_plan(**args)


def test_data_source_error_propagates_message(monkeypatch):
    import markets._data

    class Refusing(FakeProvider):
        def quote(self, ticker):
            raise DataSourceError(
                "Yahoo Finance refused or failed the request (HTTP 429); retry later"
            )

    monkeypatch.setattr(markets._data, "_provider", Refusing())
    with pytest.raises(DataSourceError, match="retry later"):
        quote("AAPL")
    with pytest.raises(DataSourceError, match="retry later"):
        price_target_odds("AAPL", [350])


def test_realized_sigma_fallback_matches_vol_module():
    res = price_target_odds("SPYX", [350], method="lognormal")
    assert res["sigma_source"] == "realized_20d"
    assert res["sigma"] == pytest.approx(
        realized_vol(FakeProvider().history("SPYX", None)["Close"], 20)
    )
