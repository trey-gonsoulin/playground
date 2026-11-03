"""Market data layer: a thin, cached wrapper over yfinance.

Everything the analysis modules need from Yahoo goes through `MarketData`, so
they stay pure and tests can swap in a fake provider via `_provider`.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol

import pandas as pd
import yfinance as yf

HISTORY_TTL = 3600
QUOTE_TTL = 30
EARNINGS_TTL = 43200
CHAIN_TTL = 300
RATE_TTL = 3600

NEW_YORK = "America/New_York"
OHLCV = ["Open", "High", "Low", "Close", "Volume"]
CHAIN_COLUMNS = [
    "strike",
    "bid",
    "ask",
    "lastPrice",
    "impliedVolatility",
    "openInterest",
    "volume",
]


class DataSourceError(RuntimeError):
    """Yahoo Finance refused or failed a request."""


class UnknownTicker(ValueError):
    """Yahoo has no data for the ticker."""


@dataclass(frozen=True)
class Quote:
    ticker: str
    currency: str
    last: float
    previous_close: float
    regular_time: datetime
    pre_price: float | None
    pre_time: datetime | None
    post_price: float | None
    post_time: datetime | None
    dividend_yield: float  # fraction, 0.0 if unknown


@dataclass(frozen=True)
class EarningsEvent:
    date: date
    timing: Literal["before_open", "after_close"]
    eps_estimate: float | None
    eps_reported: float | None
    surprise_pct: float | None


class MarketData(Protocol):
    def history(
        self,
        ticker: str,
        start: date,
        end: date | None = None,
        interval: str = "1d",
        prepost: bool = False,
    ) -> pd.DataFrame:
        """Adjusted OHLCV with a tz-naive index."""
        ...

    def quote(self, ticker: str) -> Quote: ...

    def earnings(
        self, ticker: str, limit: int = 80
    ) -> tuple[list[EarningsEvent], EarningsEvent | None]:
        """Reported events ascending, and the next unreported event."""
        ...

    def option_expiries(self, ticker: str) -> list[date]: ...

    def option_chain(
        self, ticker: str, expiry: date
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(calls, puts)."""
        ...

    def rate(self) -> float:
        """13-week T-bill yield (^IRX last close / 100)."""
        ...


def _call[T](fn: Callable[[], T]) -> T:
    """Run one yfinance call, re-raising any failure as DataSourceError."""
    try:
        return fn()
    except Exception as exc:
        raise DataSourceError(
            f"Yahoo Finance refused or failed the request ({exc}); retry later"
        ) from exc


def _num(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _utc(epoch: Any) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC)


class YahooProvider(MarketData):
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def _cached[T](self, key: tuple, ttl: float, fetch: Callable[[], T]) -> T:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = fetch()
        self._cache[key] = (now + ttl, value)
        return value

    def history(
        self,
        ticker: str,
        start: date,
        end: date | None = None,
        interval: str = "1d",
        prepost: bool = False,
    ) -> pd.DataFrame:
        key = ("history", ticker, start, end, interval, prepost)
        return self._cached(
            key, HISTORY_TTL, lambda: self._fetch_history(*key[1:])
        ).copy()

    def _fetch_history(
        self,
        ticker: str,
        start: date,
        end: date | None,
        interval: str,
        prepost: bool,
    ) -> pd.DataFrame:
        raw = _call(
            lambda: yf.Ticker(ticker).history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                prepost=prepost,
            )
        )
        if raw.empty:
            if _call(lambda: yf.Ticker(ticker).info).get("regularMarketPrice") is None:
                raise UnknownTicker(f"No market data for {ticker!r}")
            return pd.DataFrame(columns=OHLCV)
        df = raw[OHLCV].copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if interval.endswith(("d", "wk", "mo")):
            df.index = df.index.normalize()
        return df

    def quote(self, ticker: str) -> Quote:
        return self._cached(
            ("quote", ticker), QUOTE_TTL, lambda: self._fetch_quote(ticker)
        )

    def _fetch_quote(self, ticker: str) -> Quote:
        info = _call(lambda: yf.Ticker(ticker).info)
        last = _num(info.get("regularMarketPrice"))
        if last is None:
            raise UnknownTicker(f"No quote for {ticker!r}")
        previous = _num(info.get("regularMarketPreviousClose"))
        return Quote(
            ticker=ticker,
            currency=info.get("currency", "USD"),
            last=last,
            previous_close=last if previous is None else previous,
            regular_time=_utc(info.get("regularMarketTime")) or datetime.now(UTC),
            pre_price=_num(info.get("preMarketPrice")),
            pre_time=_utc(info.get("preMarketTime")),
            post_price=_num(info.get("postMarketPrice")),
            post_time=_utc(info.get("postMarketTime")),
            # Yahoo reports the yield in percent.
            dividend_yield=(_num(info.get("dividendYield")) or 0.0) / 100,
        )

    def earnings(
        self, ticker: str, limit: int = 80
    ) -> tuple[list[EarningsEvent], EarningsEvent | None]:
        return self._cached(
            ("earnings", ticker, limit),
            EARNINGS_TTL,
            lambda: self._fetch_earnings(ticker, limit),
        )

    def _fetch_earnings(
        self, ticker: str, limit: int
    ) -> tuple[list[EarningsEvent], EarningsEvent | None]:
        df = _call(lambda: yf.Ticker(ticker).get_earnings_dates(limit=limit))
        if df is None or df.empty:
            return [], None
        today = datetime.fromtimestamp(self._clock(), tz=UTC).date()
        reported: list[EarningsEvent] = []
        upcoming: list[EarningsEvent] = []
        for stamp, row in df.sort_index().iterrows():
            ts = pd.Timestamp(stamp)
            ts = (
                ts.tz_localize(NEW_YORK)
                if ts.tzinfo is None
                else ts.tz_convert(NEW_YORK)
            )
            event = EarningsEvent(
                date=ts.date(),
                timing="before_open" if ts.hour < 12 else "after_close",
                eps_estimate=_num(row.get("EPS Estimate")),
                eps_reported=_num(row.get("Reported EPS")),
                surprise_pct=_num(row.get("Surprise(%)")),
            )
            if event.eps_reported is not None:
                reported.append(event)
            elif event.date >= today:
                upcoming.append(event)
        return reported, upcoming[0] if upcoming else None

    def option_expiries(self, ticker: str) -> list[date]:
        options = self._cached(
            ("expiries", ticker),
            CHAIN_TTL,
            lambda: _call(lambda: yf.Ticker(ticker).options),
        )
        return [date.fromisoformat(d) for d in options]

    def option_chain(
        self, ticker: str, expiry: date
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        chain = self._cached(
            ("chain", ticker, expiry),
            CHAIN_TTL,
            lambda: _call(lambda: yf.Ticker(ticker).option_chain(expiry.isoformat())),
        )
        return chain.calls[CHAIN_COLUMNS].copy(), chain.puts[CHAIN_COLUMNS].copy()

    def rate(self) -> float:
        return self._cached("rate", RATE_TTL, self._fetch_rate)

    def _fetch_rate(self) -> float:
        today = datetime.fromtimestamp(self._clock(), tz=UTC).date()
        start = today - timedelta(days=10)
        closes = self.history("^IRX", start)["Close"].dropna()
        if closes.empty:
            raise DataSourceError(
                "Yahoo Finance refused or failed the request (no ^IRX data); retry later"
            )
        return float(closes.iloc[-1]) / 100


_provider: MarketData | None = None


def get_provider() -> MarketData:
    global _provider  # noqa: PLW0603
    if _provider is None:
        _provider = YahooProvider()
    return _provider


def resolve_spot(quote: Quote, spot: float | None = None) -> tuple[float, str]:
    """Pick the price to analyse from: argument, then the newest post/pre quote later than the close, then last.

    Yahoo keeps the previous evening's post-market price through the next pre-market session, so
    the newer of the two wins rather than a fixed order.
    """
    if spot is not None:
        return spot, "argument"
    later = [
        (stamp, price, source)
        for price, stamp, source in (
            (quote.post_price, quote.post_time, "post_market"),
            (quote.pre_price, quote.pre_time, "pre_market"),
        )
        if price is not None and stamp is not None and stamp > quote.regular_time
    ]
    if later:
        _, price, source = max(later, key=lambda c: c[0])
        return price, source
    return quote.last, "last"
