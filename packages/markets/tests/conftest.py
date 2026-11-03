"""Shared fixtures: a FakeTicker that stands in for yfinance, so tests never touch the network."""

from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import markets._data
import pandas as pd
import pytest
from markets._data import Quote

NOW = datetime(2025, 6, 2, 15, 0, tzinfo=UTC)


class FakeTicker:
    """Class-level configuration, reset per test by the `fake_ticker` fixture."""

    calls: Counter = Counter()  # noqa: RUF012
    info_data: dict = {}  # noqa: RUF012
    history_data: pd.DataFrame = pd.DataFrame()
    history_error: Exception | None = None
    earnings_data: pd.DataFrame = pd.DataFrame()
    expiries: tuple = ()
    chain: SimpleNamespace | None = None

    def __init__(self, symbol: str):
        self.symbol = symbol

    @property
    def info(self) -> dict:
        FakeTicker.calls["info"] += 1
        return FakeTicker.info_data

    @property
    def options(self) -> tuple:
        FakeTicker.calls["options"] += 1
        return FakeTicker.expiries

    def history(self, **kwargs) -> pd.DataFrame:
        FakeTicker.calls["history"] += 1
        FakeTicker.last_history_kwargs = kwargs
        if FakeTicker.history_error is not None:
            raise FakeTicker.history_error
        return FakeTicker.history_data

    def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame:
        FakeTicker.calls["earnings"] += 1
        return FakeTicker.earnings_data

    def option_chain(self, date: str) -> SimpleNamespace:
        FakeTicker.calls["option_chain"] += 1
        return FakeTicker.chain


@pytest.fixture(autouse=True)
def fake_ticker(monkeypatch):
    FakeTicker.calls = Counter()
    FakeTicker.info_data = {}
    FakeTicker.history_data = pd.DataFrame()
    FakeTicker.history_error = None
    FakeTicker.earnings_data = pd.DataFrame()
    FakeTicker.expiries = ()
    FakeTicker.chain = None
    monkeypatch.setattr(markets._data.yf, "Ticker", FakeTicker)
    return FakeTicker


@pytest.fixture
def quote_factory():
    def make(**overrides) -> Quote:
        fields = {
            "ticker": "AAPL",
            "currency": "USD",
            "last": 100.0,
            "previous_close": 99.0,
            "regular_time": NOW,
            "pre_price": None,
            "pre_time": None,
            "post_price": None,
            "post_time": None,
            "dividend_yield": 0.0,
        }
        return Quote(**{**fields, **overrides})

    return make
