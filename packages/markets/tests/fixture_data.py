"""Loaders for the recorded AAPL fixtures (captured once from Yahoo on 2026-11-02, never refetched)."""

import json
from datetime import date
from functools import cache
from pathlib import Path

import pandas as pd
from markets._data import EarningsEvent, Quote

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_meta() -> dict:
    return json.loads((FIXTURES / "meta.json").read_text())


@cache
def _history() -> pd.DataFrame:
    return pd.read_csv(
        FIXTURES / "aapl_history.csv", index_col="Date", parse_dates=True
    )


def fixture_history() -> pd.DataFrame:
    return _history().copy()


def fixture_close() -> pd.Series:
    return fixture_history()["Close"]


@cache
def _chains() -> list[tuple[date, pd.DataFrame, pd.DataFrame]]:
    chains = []
    for calls_path in sorted((FIXTURES / "chains").glob("*_calls.csv")):
        expiry = date.fromisoformat(calls_path.name.split("_")[0])
        puts_path = calls_path.with_name(calls_path.name.replace("_calls", "_puts"))
        chains.append((expiry, pd.read_csv(calls_path), pd.read_csv(puts_path)))
    return chains


def fixture_chains() -> list[tuple[date, pd.DataFrame, pd.DataFrame]]:
    """(expiry, calls, puts) for the first six expiries, ascending."""
    return [(expiry, calls.copy(), puts.copy()) for expiry, calls, puts in _chains()]


# AAPL's after-close report dates in the fixture range; the EPS figures are synthetic.
EARNINGS_DATES = [
    date(2021, 1, 27),
    date(2021, 4, 28),
    date(2021, 7, 27),
    date(2021, 10, 28),
    date(2022, 1, 27),
    date(2022, 4, 28),
    date(2022, 7, 28),
    date(2022, 10, 27),
    date(2023, 2, 2),
    date(2023, 5, 4),
    date(2023, 8, 3),
    date(2023, 11, 2),
    date(2024, 2, 1),
    date(2024, 5, 2),
    date(2024, 8, 1),
    date(2024, 10, 31),
    date(2025, 1, 30),
    date(2025, 5, 1),
    date(2025, 7, 31),
    date(2025, 10, 30),
    date(2026, 1, 29),
    date(2026, 4, 30),
    date(2026, 7, 30),
]
EARNINGS = [
    EarningsEvent(day, "after_close", 1.5, 1.5 * (1 + surprise / 100), surprise)
    for day, surprise in zip(EARNINGS_DATES, [3.0, -1.0, 5.0, 0.5] * 6, strict=False)
] + [EarningsEvent(date(2026, 11, 2), "after_close", 1.99, 2.10, 5.75)]
NEXT_EARNINGS = EarningsEvent(date(2027, 1, 29), "after_close", 2.4, None, None)


class FakeProvider:
    """The recorded AAPL data for any ticker, except `NO_DERIVATIVES` (ETF-like: no options, no earnings)."""

    NO_DERIVATIVES = frozenset({"SPYX"})

    def __init__(self, with_options=True, expiry_count=None, post_price=None):
        self.meta = fixture_meta()
        self.with_options = with_options
        self.expiry_count = expiry_count
        self.post_price = post_price

    def history(self, ticker, start, end=None, interval="1d", prepost=False):
        return fixture_history()

    def quote(self, ticker):
        stamp = pd.Timestamp("2026-11-02 21:00", tz="UTC").to_pydatetime()
        return Quote(
            ticker,
            "USD",
            self.meta["spot"],
            self.meta["spot"],
            stamp,
            None,
            None,
            self.post_price,
            None if self.post_price is None else stamp + pd.Timedelta(hours=1),
            self.meta["dividend_yield"],
        )

    def earnings(self, ticker, limit=80):
        if ticker in self.NO_DERIVATIVES:
            return [], None
        return list(EARNINGS), NEXT_EARNINGS

    def option_expiries(self, ticker):
        if not self.with_options or ticker in self.NO_DERIVATIVES:
            return []
        return [c[0] for c in _chains()][: self.expiry_count]

    def option_chain(self, ticker, expiry):
        return next((c[1], c[2]) for c in fixture_chains() if c[0] == expiry)

    def rate(self):
        return self.meta["rate"]
