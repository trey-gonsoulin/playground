"""Loaders for the recorded AAPL fixtures (captured once from Yahoo on 2026-11-02, never refetched)."""

import json
from datetime import date
from pathlib import Path

import pandas as pd

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_meta() -> dict:
    return json.loads((FIXTURES / "meta.json").read_text())


def fixture_history() -> pd.DataFrame:
    return pd.read_csv(
        FIXTURES / "aapl_history.csv", index_col="Date", parse_dates=True
    )


def fixture_close() -> pd.Series:
    return fixture_history()["Close"]


def fixture_chains() -> list[tuple[date, pd.DataFrame, pd.DataFrame]]:
    """(expiry, calls, puts) for the first six expiries, ascending."""
    chains = []
    for calls_path in sorted((FIXTURES / "chains").glob("*_calls.csv")):
        expiry = date.fromisoformat(calls_path.name.split("_")[0])
        puts_path = calls_path.with_name(calls_path.name.replace("_calls", "_puts"))
        chains.append((expiry, pd.read_csv(calls_path), pd.read_csv(puts_path)))
    return chains
