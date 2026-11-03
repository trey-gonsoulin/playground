# markets MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deployed `markets` MCP server with tools for earnings event studies, option-implied and forecast volatility, price-level odds and sell-plan simulation.

**Architecture:** New uv workspace package `packages/markets/` on the shared `mcp_lambda` wiring. Pure-math modules (`_odds`, `_paths`, `_vol`, `_events`, `_plans`) take DataFrames/arrays and never fetch; `_data` is the only module that talks to Yahoo; `mcp_server.py` glues them. One new function + HttpApi in the `mcp-playground` stack.

**Tech Stack:** Python 3.13, mcp 2.x (`MCPServer`), FastAPI + Mangum, yfinance, pandas, numpy, scipy, arch, pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-markets-mcp-design.md`

## Global Constraints

- `mcp>=2.3,<3`; server name `"markets"`; app via `create_mcp_app(mcp, title="Markets MCP", description=..., auth_env="MARKETS_MCP_SECRET")`.
- All tools and route handlers are sync `def`; tool arguments snake_case; results are JSON-serializable dicts with `as_of` (ISO UTC) and `source: "yahoo"`.
- Data comes only from yfinance. Yahoo failures raise `DataSourceError("Yahoo Finance refused or failed the request (<detail>); retry later")`; unknown tickers raise `UnknownTicker`. Never return a silent empty result.
- Tests never touch the network. Run with `env -u UV_DEFAULT_INDEX -u PIP_INDEX_URL uv run --package markets pytest packages/markets/tests/`.
- Trading-day annualization 252; option time to expiry ACT/365.
- Simulation defaults: `n_paths=10000`, `seed=0`, cap `days * n_paths <= 2_000_000`, stationary block bootstrap mean block 5 days on 5 years of history, overnight variance share default 0.3.
- Lambda: Timeout 29, MemorySize 1024, `HOME=/tmp`.
- Lint: `uv run ruff check --fix . && uv run ruff format .` clean.
- Commit after each task, with the `Co-Authored-By: Claude <noreply@anthropic.com>` trailer.

**Deviation from spec (flag in review):** the cache is an in-process TTL dict instead of a `/tmp` file cache. A warm Lambda keeps module state, so the hit rate is the same and no pickling of files is needed; `MARKETS_CACHE_DIR` is dropped.

## Review Focus

1. **Ticker with no options or no earnings history (e.g. an ETF like `SPY`)** — `implied_volatility` / `earnings_event_study` should return a clear error message, not crash on an empty frame. Test in Task 7.
2. **Levels on both sides of spot, including a level equal to spot** — `price_target_odds` returns touch = 1.0 for `level == spot`, correct-direction odds for each side. Test in Task 2.
3. **Gap through a stop-limit's limit price** — the tranche must not fill at the stop; it rests at `limit_price` or sells at the deadline. Test in Task 6.
4. **Requests right after an after-close report (stale option quotes still include the event)** — the ex-event vol must come from expiries after the event, not the event-week expiry. Test in Task 4.
5. **Event too recent for a full window (the report from last night)** — dropped from the study and counted in `dropped_events`, not crashing on index overflow. Test in Task 5.

---

### Task 1: Package skeleton and data layer

**Files:**
- Create: `packages/markets/pyproject.toml`, `packages/markets/markets/__init__.py`, `packages/markets/markets/_data.py`
- Create: `packages/markets/tests/test_data.py`
- Modify: `.dockerignore` (add `!packages/markets/markets`)

**Interfaces:**
- Produces (in `markets._data`):
  - `class DataSourceError(RuntimeError)`, `class UnknownTicker(ValueError)`
  - `@dataclass(frozen=True) class Quote: ticker: str; currency: str; last: float; previous_close: float; regular_time: datetime; pre_price: float | None; pre_time: datetime | None; post_price: float | None; post_time: datetime | None; dividend_yield: float` (fraction, 0.0 if unknown)
  - `@dataclass(frozen=True) class EarningsEvent: date: date; timing: Literal["before_open", "after_close"]; eps_estimate: float | None; eps_reported: float | None; surprise_pct: float | None`
  - `class MarketData(Protocol)` with `history(ticker: str, start: date, end: date | None = None, interval: str = "1d", prepost: bool = False) -> pd.DataFrame` (columns `Open High Low Close Volume`, tz-naive index, adjusted), `quote(ticker) -> Quote`, `earnings(ticker, limit: int = 80) -> tuple[list[EarningsEvent], EarningsEvent | None]` (reported events ascending, next unreported event), `option_expiries(ticker) -> list[date]`, `option_chain(ticker, expiry: date) -> tuple[pd.DataFrame, pd.DataFrame]` (calls, puts; columns `strike bid ask lastPrice impliedVolatility openInterest volume`), `rate() -> float` (13-week T-bill `^IRX` last close / 100)
  - `class YahooProvider(MarketData)` with `__init__(self, clock: Callable[[], float] = time.time)`
  - `def get_provider() -> MarketData` — module-level singleton; tests monkeypatch `markets._data._provider`
  - `def resolve_spot(quote: Quote, spot: float | None = None) -> tuple[float, str]` — source in `{"argument", "post_market", "pre_market", "last"}`

- [ ] **Step 1: Scaffold.** `pyproject.toml` copied from `packages/ynab-mcp/pyproject.toml` with name `markets`, description "MCP server for short-horizon market analysis", script `markets-mcp = "markets.main:cli"`, wheel package `markets`, deps `yfinance>=1.0`, `pandas>=2.2`, `numpy>=2.1`, `scipy>=1.14`, `arch>=7.2`, `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `mangum>=0.21.0`, `mcp>=2.3,<3`, `mcp-lambda`. Run `env -u UV_DEFAULT_INDEX -u PIP_INDEX_URL uv sync`; expected: lock updated, resolves. (Pin the exact yfinance version uv resolves, `yfinance==X.Y.Z`, per the spec's risk note.)

- [ ] **Step 2: Write failing tests** in `tests/test_data.py`, with yfinance replaced by a fake `Ticker` object injected via `monkeypatch.setattr(markets._data.yf, "Ticker", FakeTicker)`:

```python
def test_resolve_spot_prefers_argument_then_post_then_pre(quote_factory):
    q = quote_factory(last=338.32, post_price=328.81, post_time=LATER)
    assert resolve_spot(q, 330.0) == (330.0, "argument")
    assert resolve_spot(q) == (328.81, "post_market")
    assert resolve_spot(quote_factory(last=338.32, pre_price=331.0, pre_time=LATER)) == (331.0, "pre_market")
    assert resolve_spot(quote_factory(last=338.32, post_price=320.0, post_time=EARLIER)) == (338.32, "last")

def test_earnings_timing_and_split():
    # timestamps 16:00 ET -> after_close, 08:30 ET -> before_open; NaN reported -> next event
    events, nxt = YahooProvider().earnings("AAPL")
    assert [e.timing for e in events] == ["before_open", "after_close"]
    assert events[0].date < events[1].date and nxt.eps_reported is None

def test_http_429_maps_to_data_source_error():   # FakeTicker.history raises an exception whose str contains "429 Too Many Requests"
    with pytest.raises(DataSourceError, match="retry later"): YahooProvider().history("AAPL", date(2025, 1, 1))

def test_empty_history_and_no_quote_is_unknown_ticker():
    with pytest.raises(UnknownTicker): YahooProvider().history("ZZZZQ", date(2025, 1, 1))

def test_cache_hits_within_ttl_and_expires():
    now = [1000.0]; p = YahooProvider(clock=lambda: now[0])
    p.quote("AAPL"); p.quote("AAPL"); assert FakeTicker.calls["info"] == 1
    now[0] += 31; p.quote("AAPL"); assert FakeTicker.calls["info"] == 2
```

- [ ] **Step 3: Run** `… pytest packages/markets/tests/test_data.py -v` → FAIL (`ModuleNotFoundError: markets._data`).

- [ ] **Step 4: Implement `_data.py`.** TTLs (seconds): history 3600, quote 30, earnings 43200, option chains 300, rate 3600; cache key `(method, args)`. Timing: convert the earnings timestamp to `America/New_York`; hour < 12 → `before_open`. Quote fields from `Ticker.info`: `regularMarketPrice`, `regularMarketPreviousClose`, `regularMarketTime`, `preMarketPrice/Time`, `postMarketPrice/Time`, `currency`, `dividendYield` (Yahoo reports percent; divide by 100). `resolve_spot` uses post/pre only when their time is later than `regular_time` (post before pre when both are). Wrap every yfinance call in one helper that re-raises any exception as `DataSourceError`; raise `UnknownTicker` when history is empty and `info` has no `regularMarketPrice`. `history` strips tz and normalizes the daily index, `auto_adjust=True`.

- [ ] **Step 5: Run tests** → PASS. Add `!packages/markets/markets` to `.dockerignore`.

- [ ] **Step 6: Commit** `feat(markets): package skeleton and yfinance data layer`.

---

### Task 2: Closed-form lognormal odds

**Files:**
- Create: `packages/markets/markets/_odds.py`, `packages/markets/tests/test_odds.py`

**Interfaces:**
- Produces:
  - `touch_probability(spot: float, level: float, sigma: float, years: float, drift: float = 0.0) -> float`
  - `close_beyond_probability(spot: float, level: float, sigma: float, years: float, drift: float = 0.0) -> float` — P(S_T ≥ L) when L > spot, P(S_T ≤ L) when L < spot, 1.0 when equal
  - `lognormal_odds(spot: float, levels: list[float], sigma: float, days: int, drift: float = 0.0) -> list[dict]` — each `{"level", "pct_from_spot", "touch", "close_beyond"}`, `years = days / 252`

- [ ] **Step 1: Failing tests**

```python
def test_zero_drift_matches_reflection_formula():
    s = 0.25 * math.sqrt(28 / 365)
    assert touch_probability(328.8, 345.0, 0.25, 28 / 365) == pytest.approx(2 * (1 - norm.cdf(math.log(345 / 328.8) / s)), rel=1e-9)

def test_level_at_spot_and_zero_vol_limits():
    assert touch_probability(100, 100, 0.3, 0.1) == 1.0
    assert touch_probability(100, 110, 1e-9, 0.1) == pytest.approx(0.0, abs=1e-9)

def test_downside_mirrors_upside_for_zero_log_drift():   # drift = sigma**2/2 -> nu = 0
    up = touch_probability(100, 110, 0.3, 0.2, drift=0.045); dn = touch_probability(100, 100 / 1.1, 0.3, 0.2, drift=0.045)
    assert up == pytest.approx(dn, rel=1e-9)

def test_matches_fine_step_monte_carlo():   # 20k paths x 2000 steps, seed 0, drift 0.05
    assert touch_probability(100, 108, 0.3, 20 / 252, 0.05) == pytest.approx(mc_estimate, abs=0.01)

def test_lognormal_odds_both_sides():
    rows = lognormal_odds(100, [90, 100, 110], 0.3, 20)
    assert [r["touch"] == 1.0 for r in rows] == [False, True, False]
    assert rows[0]["close_beyond"] < rows[0]["touch"] and rows[2]["close_beyond"] < rows[2]["touch"]
```

- [ ] **Step 2: Run** → FAIL (import error).
- [ ] **Step 3: Implement** with b = ln(L/S), ν = drift − σ²/2: for L > S, `Φ((−b + ντ)/(σ√τ)) + exp(2νb/σ²)·Φ((−b − ντ)/(σ√τ))`; for L < S use the same with b → −b, ν → −ν. Guard σ√τ ≤ 1e-12 → 0.0 unless L == S.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(markets): closed-form touch and terminal odds`.

---

### Task 3: Path simulation and bootstrap odds

**Files:**
- Create: `packages/markets/markets/_paths.py`, `packages/markets/tests/test_paths.py`
- Modify: `packages/markets/markets/_odds.py` (add `bootstrap_odds`)

**Interfaces:**
- Consumes: Task 2's row shape.
- Produces:
  - `@dataclass(frozen=True) class Paths: spot: float; open: np.ndarray; high: np.ndarray; low: np.ndarray; close: np.ndarray` — each `(n_paths, days)`
  - `split_returns(history: pd.DataFrame) -> np.ndarray` — shape `(N, 2)`: `[ln(open_t/close_{t-1}), ln(close_t/open_t)]`
  - `overnight_share(pairs: np.ndarray) -> float` — `var(col0) / (var(col0) + var(col1))`, 0.3 if fewer than 60 rows
  - `simulate(spot: float, days: int, *, sigma: float | None = None, pairs: np.ndarray | None = None, drift: float = 0.0, overnight: float = 0.3, n_paths: int = 10000, seed: int = 0) -> Paths` — `pairs` given → block bootstrap; else GBM with `sigma` (one of the two required → `ValueError`)
  - `bootstrap_odds(paths: Paths, levels: list[float]) -> list[dict]` (same keys as `lognormal_odds`)

- [ ] **Step 1: Failing tests**

```python
def test_cap_rejected():
    with pytest.raises(ValueError, match="2,000,000"): simulate(100, 300, sigma=0.3, n_paths=10000)

def test_zero_drift_is_a_martingale_gbm_and_bootstrap():
    assert simulate(100, 20, sigma=0.3).close[:, -1].mean() == pytest.approx(100, rel=0.005)
    assert simulate(100, 20, pairs=fixture_pairs()).close[:, -1].mean() == pytest.approx(100, rel=0.005)

def test_bootstrap_rescaled_to_sigma():
    p = simulate(100, 20, pairs=fixture_pairs(), sigma=0.25)
    assert np.log(p.close[:, -1] / 100).std() == pytest.approx(0.25 * math.sqrt(20 / 252), rel=0.03)

def test_bar_consistency():
    p = simulate(100, 10, sigma=0.3)
    assert (p.high >= np.maximum(p.open, p.close)).all() and (p.low <= np.minimum(p.open, p.close)).all()

def test_bridge_makes_touch_odds_match_closed_form():
    p = simulate(100, 20, sigma=0.3, overnight=0.0)
    got = bootstrap_odds(p, [108])[0]["touch"]
    assert got == pytest.approx(touch_probability(100, 108, 0.3, 20 / 252), abs=0.015)

def test_same_seed_same_paths():
    assert np.array_equal(simulate(100, 5, sigma=0.3).close, simulate(100, 5, sigma=0.3).close)
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Bootstrap: demean each column of `pairs`; stationary block bootstrap (each step restarts a block with probability 1/5 at a uniform random row, otherwise takes the next row, wrapping); if `sigma` given, scale both columns by `sigma / (std(col0 + col1) * sqrt(252))`. Then add per-day log drift `(drift − σ_d²·252/2)/252`, split between the columns by their variance share, where σ_d² is the per-day variance of the sampled sum (so E[S_t] = S₀·e^{drift·t}). GBM: normal columns with variances `overnight·σ²/252` and `(1−overnight)·σ²/252`, same drift split. Intraday extremes by sampling the Brownian-bridge max/min in log space between open `a` and close `b` with intraday variance `v`:

```python
u1, u2 = rng.random(shape), rng.random(shape)
log_high = (a + b + np.sqrt((b - a) ** 2 - 2 * v * np.log(u1))) / 2
log_low  = (a + b - np.sqrt((b - a) ** 2 - 2 * v * np.log(u2))) / 2
```

  For bootstrap, `v` = the intraday column's variance after scaling. `bootstrap_odds`: touch = any high ≥ L (L > spot) or any low ≤ L (L < spot); `close_beyond` from the last close; level == spot → 1.0.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(markets): bootstrap/GBM path engine with bridge extremes`.

---

### Task 4: Volatility

**Files:**
- Create: `packages/markets/markets/_vol.py`, `packages/markets/tests/test_vol.py`, `packages/markets/tests/fixtures/` (one recorded AAPL chain pair as CSV, one 6-year AAPL daily history CSV, captured once with a throwaway script and committed)

**Interfaces:**
- Consumes: `MarketData`, `Quote`, `EarningsEvent` (Task 1).
- Produces:
  - `bs_price(spot, strike, years, rate, div_yield, sigma, call: bool) -> float`
  - `implied_vol(price, spot, strike, years, rate, div_yield, call: bool) -> float | None` (brentq on [1e-4, 5]; None if no root)
  - `atm(calls: pd.DataFrame, puts: pd.DataFrame, spot, years, rate, div_yield) -> dict` — `{"strike", "iv", "call_iv", "put_iv", "straddle_mid"}`; IV from the bid/ask mid, falling back to `impliedVolatility` when bid ≤ 0 or ask ≤ bid
  - `forward_vol(sigma1: float, t1: float, sigma2: float, t2: float) -> float | None` (None when t2 ≤ t1 or forward variance ≤ 0)
  - `pending_event(events: list[EarningsEvent], nxt: EarningsEvent | None, last_close: date) -> date | None` — an `after_close` event on `last_close` (quotes not yet reacted) wins; else `nxt.date`
  - `term_structure(chains: list[tuple[date, pd.DataFrame, pd.DataFrame]], spot, today: date, rate, div_yield, event: date | None) -> dict` — `{"expiries": [{"expiry", "days", "atm_iv", "straddle_pct", "expected_move_pct"}], "forward": [{"from", "to", "vol"}], "ex_event_vol": float | None}`
  - `realized_vol(close: pd.Series, window: int) -> float`
  - `garch_forecast(close: pd.Series, horizon_days: int) -> float` — annualized average vol, GARCH(1,1) Student-t on % log returns of the last 5 y
  - `default_sigma(provider: MarketData, ticker: str, today: date) -> tuple[float, str]` — `(ex_event_vol, "options_ex_event")`, else `(atm_iv of expiry nearest 30 days, "options_atm")`, else `(realized_vol 20, "realized_20d")`

- [ ] **Step 1: Failing tests**

```python
def test_bs_iv_round_trip():
    p = bs_price(100, 105, 0.25, 0.04, 0.005, 0.3, call=True)
    assert implied_vol(p, 100, 105, 0.25, 0.04, 0.005, call=True) == pytest.approx(0.3, abs=1e-6)

def test_forward_vol_hand_calc():
    assert forward_vol(0.59, 4 / 365, 0.295, 46 / 365) == pytest.approx(0.2493, abs=5e-4)
    assert forward_vol(0.2, 0.1, 0.1, 0.11) is None

def test_ex_event_vol_skips_event_week_expiry():   # event = today (after-close report), first expiry in 4 days
    ts = term_structure(fixture_chains(), spot=328.8, today=date(2026, 11, 2), rate=0.04, div_yield=0.004, event=date(2026, 11, 2))
    assert all(f["from"] > date(2026, 11, 2) for f in ts["forward"])
    assert 0.15 < ts["ex_event_vol"] < ts["expiries"][0]["atm_iv"]

def test_pending_event_after_close_today_wins():
    ev = [EarningsEvent(date(2026, 11, 2), "after_close", 1.99, 2.10, 5.75)]
    assert pending_event(ev, EarningsEvent(date(2027, 1, 28), "after_close", None, None, None), date(2026, 11, 2)) == date(2026, 11, 2)

def test_atm_falls_back_on_crossed_quote():   # bid 0 ask 0 -> uses impliedVolatility column
    ...assert atm(...)["call_iv"] == pytest.approx(row_iv)

def test_garch_is_positive_and_reasonable():
    assert 0.05 < garch_forecast(fixture_close(), 20) < 1.0
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `years = days / 365`. `expected_move_pct = atm_iv · √years · 100`; `straddle_pct = straddle_mid / spot · 100`. Forward vols: from the first expiry strictly after `event` (or the first expiry when `event` is None or later than every expiry) to each later expiry; `ex_event_vol` = the forward vol whose `to` is nearest 30 days after `today`, None if no forward exists.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(markets): implied, forward, realized and GARCH volatility`.

---

### Task 5: Earnings event study

**Files:**
- Create: `packages/markets/markets/_events.py`, `packages/markets/tests/test_events.py`

**Interfaces:**
- Consumes: `EarningsEvent` (Task 1).
- Produces:
  - `day_zero(index: pd.DatetimeIndex, event: EarningsEvent) -> int | None` — after_close: last session ≤ event date; before_open: last session < event date
  - `event_study(prices: pd.DataFrame, events: list[EarningsEvent], *, horizon_days: int = 20, anchor_day: int = 2, day1_min: float | None = None, day1_max: float | None = None, surprise_min: float | None = None, surprise_max: float | None = None, since: date | None = None, levels_pct: list[float] | None = None, include_all_events: bool = False) -> dict`
  - Result keys: `n_events`, `dropped_events`, `days` (`{k: {"mean", "median", "pct_up"}}` for k ∈ {1,2,3,5,10,15,20} ≤ horizon), `after_anchor` (`{"max": quartiles, "min": quartiles, "end": quartiles}`), `hit_rates` (`[{"pct", "up", "down"}]`), `baseline` (`{"mean", "median", "pct_up"}` of all H-day close-to-close returns over the same date span), `events` (rows: `date, timing, surprise_pct, gap_open, r1, r2, r5, r10, r20, max_after, min_after, end_after, best_day`; last 12 unless `include_all_events`). Day-1 filters are in percent (`day1_max=-2` means r1 ≤ −2%), like `levels_pct`. All returns in output are percents rounded to 2 dp.

- [ ] **Step 1: Failing tests** on a synthetic 300-session price frame (flat 100 except hand-set bars):

```python
def test_day_zero_alignment():
    idx = pd.bdate_range("2026-01-01", periods=10)
    assert day_zero(idx, EarningsEvent(idx[4].date(), "after_close", None, None, None)) == 4
    assert day_zero(idx, EarningsEvent(idx[4].date(), "before_open", None, None, None)) == 3

def test_returns_and_after_anchor_exact():   # close0=100, close1=95, close2=94, later high 101.05 -> max_after = +7.5%
    res = event_study(prices, [ev], horizon_days=5, anchor_day=2)
    row = res["events"][0]
    assert row["r1"] == -5.0 and row["r2"] == -6.0 and row["max_after"] == 7.5

def test_filters_and_dropped():
    res = event_study(prices, [ev_down, ev_up, ev_last_night], horizon_days=5, day1_max=-2)
    assert res["n_events"] == 1 and res["dropped_events"] == 1

def test_hit_rates_default_levels():
    assert [h["pct"] for h in event_study(prices, [ev])["hit_rates"]] == [2, 3, 4, 5, 7, 10]
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Per spec: `gap_open = open₁/close₀ − 1`, `r_k = close_k/close₀ − 1`; after anchor `a` over sessions a+1..a+H: `max_after = max(high)/close_a − 1`, `min_after = min(low)/close_a − 1`, `end_after = close_{a+H}/close_a − 1`, `best_day` = a + 1 + argmax(close). Drop events where day 0 is None or `day0 + anchor_day + horizon_days` ≥ len(prices); filters apply after dropping; filters on `surprise_*` exclude events with no surprise.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(markets): earnings event study`.

---

### Task 6: Sell-plan evaluation

**Files:**
- Create: `packages/markets/markets/_plans.py`, `packages/markets/tests/test_plans.py`

**Interfaces:**
- Consumes: `Paths`, `simulate` (Task 3).
- Produces:
  - `@dataclass(frozen=True) class Tranche: fraction: float; type: Literal["market", "limit", "stop_limit", "bracket"]; price: float | None = None; limit_price: float | None = None; take_profit: float | None = None; stop: float | None = None`
  - `@dataclass(frozen=True) class Plan: name: str; tranches: list[Tranche]`
  - `parse_plans(raw: list[dict]) -> list[Plan]` — raw shape `{"name": str, "tranches": [{"fraction", "type", ...}]}`; `ValueError` when fractions don't sum to 1 (±1e-6), a required price is missing, `limit_price > price` for stop_limit, or a bracket lacks `take_profit`/`stop`
  - `evaluate_plans(paths: Paths, shares: float, plans: list[Plan]) -> dict` — `{"spot", "sell_now": shares·spot, "plans": [{"name", "proceeds": {"p5","p25","p50","p75","p95","mean","std"}, "vs_sell_now": {same keys}, "tranches": [{"type", "fill_rate", "mean_fill_price", "mean_days_to_fill"}]}], "note": NOTE}`
  - `NOTE = "With zero drift, no plan changes expected proceeds much; plans trade off the spread of outcomes. Positive drift favors waiting, negative favors selling now."`

- [ ] **Step 1: Failing tests** on hand-built `Paths` (1–3 paths, 3–5 days) plus one simulated case:

```python
def test_market_sells_at_spot(): assert one_tranche("market").proceeds == shares * spot
def test_limit_fills_at_price_or_gap_open():   # day1 high 106 -> fills 105; path2 opens 107 -> fills 107
def test_stop_limit_gap_below_limit_rests_then_fills_on_recovery():   # stop 95 / limit 93; opens 90 -> no fill; day 3 high 93.5 -> fills 93
def test_stop_limit_gap_below_limit_never_recovers_sells_at_deadline_close()
def test_bracket_same_day_both_touched_stop_wins()
def test_bracket_one_cancels_other():   # take-profit fills -> stop never fills later on that path
def test_fractions_must_sum_to_one(): with pytest.raises(ValueError, match="sum to 1"): parse_plans([...0.5, 0.4...])
def test_zero_drift_mean_close_to_sell_now():
    paths = simulate(100, 20, sigma=0.3, n_paths=20000)
    res = evaluate_plans(paths, 100, parse_plans([LADDER]))
    assert abs(res["plans"][0]["vs_sell_now"]["mean"]) < 0.005 * 100 * 100
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** vectorized over paths, day by day, with the spec's fill rules: limit — open ≥ price → open, else high ≥ price → price. stop_limit — trigger when open ≤ stop (fill at open if open ≥ limit_price, else rest) or low ≤ stop (fill at stop); a resting order fills at `limit_price` the first time high ≥ limit_price, including later the same day. bracket — gap rules first (open ≥ take_profit → open; open ≤ stop → stop_limit behavior), then if both touched the same day the stop wins. Unfilled at the last day sells at the last close (counts as not filled in `fill_rate`). Day indices in `mean_days_to_fill` are 1-based sessions; market = 0.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(markets): sell-plan simulation`.

---

### Task 7: MCP server, app and entry points

**Files:**
- Create: `packages/markets/markets/mcp_server.py`, `app.py`, `lambda_handler.py`, `main.py` (port 8003)
- Create: `packages/markets/tests/test_app.py`, `packages/markets/tests/test_tools.py`, `packages/markets/tests/conftest.py` (a `FakeProvider` built from the Task 4 fixtures, installed by monkeypatching `markets._data._provider`)

**Interfaces:**
- Consumes: everything above.
- Produces: `quote` returns the `Quote` fields plus `spot`, `spot_source` (via `resolve_spot`) and `next_earnings`. Tools exactly as in the spec table — `quote`, `price_history`, `earnings_event_study`, `implied_volatility`, `volatility_forecast`, `price_target_odds`, `evaluate_sell_plan` (7 tools). `price_history` keeps the newest 1 000 bars. `price_target_odds(method="both")` returns rows with `touch_lognormal`, `close_beyond_lognormal`, `touch_bootstrap`, `close_beyond_bootstrap`; it also returns `spot`, `spot_source`, `sigma`, `sigma_source`. Bootstrap paths use `split_returns` of 5 years of history and `overnight_share` of those pairs, rescaled to `sigma` only when the caller passed `vol` (otherwise the history's own vol). `evaluate_sell_plan` uses one `simulate` call for all plans, `days = deadline_days`.

- [ ] **Step 1: Failing tests.** `test_app.py`: copy genealogy's lifespan re-entry, bearer secret (`MARKETS_MCP_SECRET`) and Lambda-handler tests with `N_TOOLS = 7`. `test_tools.py`, calling the tool functions directly with the fake provider:

```python
def test_quote_reports_spot_source(): assert quote("AAPL")["spot_source"] == "post_market"
def test_price_target_odds_both_methods_agree_roughly():
    rows = price_target_odds("AAPL", [342, 350, 310], days=20)["levels"]
    assert all(abs(r["touch_lognormal"] - r["touch_bootstrap"]) < 0.12 for r in rows)
def test_no_options_is_clear_error():   # fake provider with no expiries (ETF-like)
    with pytest.raises(ValueError, match="no listed options"): implied_volatility("SPYX")
def test_no_earnings_is_clear_error():
    with pytest.raises(ValueError, match="no earnings history"): earnings_event_study("SPYX")
def test_data_source_error_propagates_message(): ... match="retry later"
def test_evaluate_sell_plan_shape(): res = evaluate_sell_plan("AAPL", 100, [LADDER, SELL_NOW]); assert len(res["plans"]) == 2
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the four files following `packages/ynab-mcp/ynab_mcp/{app,lambda_handler,main}.py`; tool docstrings say what each returns and the units (percents vs fractions). Tools raise `ValueError` with the messages above; `DataSourceError`/`UnknownTicker` propagate (mcp turns them into tool errors).
- [ ] **Step 4: Run the whole package suite** → all PASS; `uv run ruff check . && uv run ruff format --check .` clean.
- [ ] **Step 5: Local smoke (network, manual):** `uv run --package markets markets-mcp`, then call `quote AAPL` and `price_target_odds AAPL [342,350]` through an MCP client or curl JSON-RPC; expected: real numbers, `source: "yahoo"`.
- [ ] **Step 6: Commit** `feat(markets): MCP tools, app and Lambda entry point`.

---

### Task 8: Container, SAM resources and docs

**Files:**
- Create: `packages/markets/Dockerfile` (genealogy's, with `genealogy` → `markets`, same pinned digests)
- Modify: `template.yaml` (add `MarketsHttpApi`, `MarketsMcpFunction`, output `MarketsMcpApiUrl`), `CLAUDE.md` (a `### markets` section under Packages, and mention it in the stack sentence of "SAM / deployment")

- [ ] **Step 1: Template.** `MarketsMcpFunction`: `PackageType: Image`, `Timeout: 29`, `MemorySize: 1024`, env `HOME: /tmp`, HttpApi events `/` and `/{proxy+}` on `MarketsHttpApi`, Metadata `DockerTag: latest`, `DockerContext: .`, `Dockerfile: packages/markets/Dockerfile`; a comment that it has no auth like genealogy and honors `MARKETS_MCP_SECRET` if set.
- [ ] **Step 2: Build.** `export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock` then `sam build`; expected: `Build Succeeded`, the markets image built. Then `docker run --rm --entrypoint python markets:latest -c "import markets.mcp_server, arch, yfinance"` exits 0 (wheels load on the Lambda base).
- [ ] **Step 3: CLAUDE.md section** covering: tools; yfinance-only data and the Lambda IP blocking risk; spot resolution order; default σ order (`options_ex_event` → `options_atm` → `realized_20d`); the path model (block bootstrap, overnight/intraday split, bridge extremes); the 2 000 000 path-day cap; in-process cache TTLs.
- [ ] **Step 4: Commit** `build(markets): Dockerfile, SAM function and CLAUDE.md section`.

---

### Task 9: Deploy and smoke test (needs the user's go-ahead)

- [ ] **Step 1:** `sam deploy --no-execute-changeset` (with `DOCKER_HOST` exported); expected: the change set shows only **Add** rows (function, role, HttpApi, stage, permissions) and no Modify/Remove rows for ynab or genealogy. Stop and show the user the change set.
- [ ] **Step 2 (after approval):** `aws cloudformation execute-change-set --change-set-name <arn> --profile personal`, then `aws cloudformation wait stack-update-complete --stack-name mcp-playground --profile personal`.
- [ ] **Step 3:** Verify the markets image tag in the deployed template matches `docker image inspect marketsmcpfunction:latest --format '{{.Id}}'` (CLAUDE.md check).
- [ ] **Step 4:** Smoke: JSON-RPC `tools/call` `quote` and `implied_volatility` for AAPL against `MarketsMcpApiUrl` + `/mcp/`. If Yahoo refuses (DataSourceError), record it in CLAUDE.md and open a follow-up issue for a keyed fallback provider.
- [ ] **Step 5:** Tell the user the connector URL to add in claude.ai.
