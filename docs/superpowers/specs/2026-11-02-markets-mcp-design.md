# markets MCP — design

Date: 2026-11-02 · Status: approved in chat, awaiting spec review

## Goal

Quantitative tooling for short-horizon equity decisions, available from Claude Desktop / iOS as a
deployed MCP server. The first use case: deciding when to sell `n` shares of AAPL in the 2–4 weeks after
the trading window opens following a quarterly earnings report. The tooling answers:

- How has this stock behaved after past earnings reports, optionally only those with a similar day-1 reaction?
- What move does the options market price in for the window, with the earnings event removed?
- What is the probability that given price levels are touched (or are the close) within the window?
- How do candidate sell plans (scale-out tranches, take-profit / stop-limit, deadline) compare with selling now?

Non-goals: price-direction ML models (LSTM, Prophet, etc.), storing holdings or portfolios, placing
orders, tax modeling, commissions.

## Decisions (from brainstorming)

- Deployed MCP in this repo (option A), following the `packages/<name>/` pattern and `mcp_lambda`.
- Data: **yfinance only** (no keys). If Yahoo refuses requests from Lambda's IP ranges, tools
  raise a clear `DataSourceError`; a keyed fallback provider is a follow-up, not part of this spec.
- No auth by default (like genealogy); the app honors `MARKETS_MCP_SECRET` via `auth_env` if set.
- No persisted state. Share counts and prices are tool arguments.

## Package layout

```
packages/markets/
  markets/
    mcp_server.py      # MCPServer("markets") tools; thin wrappers over the modules below
    app.py             # create_mcp_app(mcp, title=..., auth_env="MARKETS_MCP_SECRET")
    lambda_handler.py  # Mangum(app, lifespan="auto")
    main.py            # uvicorn CLI (`markets-mcp`)
    _data.py           # provider interface + yfinance provider + /tmp cache
    _events.py         # earnings event study
    _vol.py            # implied vol, forward vol, realized vol, GARCH forecast
    _odds.py           # touch / terminal probabilities (lognormal + bootstrap)
    _paths.py          # path simulation shared by _odds and _plans
    _plans.py          # sell-plan evaluation
  tests/
    fixtures/          # recorded yfinance frames (parquet/CSV + JSON) for offline tests
  Dockerfile
  pyproject.toml
```

Dependencies: `yfinance` (pulls `curl_cffi`, `pandas`), `numpy`, `scipy`, `arch`, plus the standard
`mangum`, `mcp>=2.3,<3`, `fastapi`, `uvicorn[standard]`, `mcp-lambda` (workspace). `.dockerignore`'s
allowlist gains `packages/markets`.

## Data layer (`_data.py`)

`MarketData` protocol with one implementation, `YahooProvider`:

- `history(ticker, start, end=None, interval="1d", prepost=False) -> DataFrame` — split/dividend-adjusted
  OHLCV (`auto_adjust=True`), tz-naive daily index.
- `quote(ticker) -> Quote` — last, previous close, pre/post-market price and change, currency, timestamp.
- `earnings_dates(ticker, limit=80) -> list[EarningsEvent]` — date, timing (before open / after close,
  inferred from the timestamp hour: < 12:00 ET = before open), EPS estimate, reported, surprise %.
  Future (unreported) events are returned separately as `next_earnings`.
- `option_expiries(ticker)` and `option_chain(ticker, expiry) -> (calls, puts)` — strike, bid, ask, last,
  implied volatility, open interest, volume.

Caching: a small file cache under `$MARKETS_CACHE_DIR` (default `/tmp/markets-cache`), keyed by call +
args. TTLs: daily history 1 h, quote 30 s, earnings dates 12 h, option chains 5 min. A warm Lambda reuses it.

Errors: any yfinance exception, HTTP 401/403/429, or an empty frame for a known-good call becomes
`DataSourceError("Yahoo Finance refused or failed the request (…); retry later")`. An unknown ticker
(empty history *and* no quote) becomes `UnknownTicker`. Tools surface both as MCP tool errors with that
message — never a silent empty result.

`spot` resolution (shared by tools): explicit `spot` argument > post-market price (if newer than the
regular close) > pre-market price > last regular price. Results report which one was used.

## Event study (`_events.py`)

For each reported earnings event, define day 0 as the last regular session whose close precedes the
announcement (the report date for after-close reports, the previous session for before-open reports).
Day k is the k-th session after day 0. Per event compute:

- `gap_open` = open₁ / close₀ − 1; `r_k` = close_k / close₀ − 1 for k in 1..horizon.
- From the anchor day `a` (default 2, the first plausible sell day): `max_after` = max(high_{a+1..a+H}) /
  close_a − 1, `min_after` = min(low_{…}) / close_a − 1, `end_after` = close_{a+H} / close_a − 1, and
  `best_day` (argmax close).

Filters: `day1_min` / `day1_max` on `r_1`, `surprise_min` / `surprise_max` on EPS surprise %, `since` date.
Events without a full window are dropped (and counted in the response).

Output: n events; per-day mean / median / % positive of `r_k` for k in {1,2,3,5,10,15,20} (∩ horizon);
quartiles of `max_after`, `min_after`, `end_after`; hit rates `P(max_after ≥ +x)` and `P(min_after ≤ −x)`
for the `levels_pct` argument (default 2,3,4,5,7,10); the per-event table (last 12 by default,
`include_all_events` for all); and the unconditional H-day return distribution over the same date range
as a baseline, so the user can see whether the earnings effect differs from ordinary drift.

## Volatility (`_vol.py`)

- **ATM implied vol per expiry:** strike nearest the forward (spot is fine at these horizons); average of
  call and put IV, using the mid-quote-implied IV recomputed with Black–Scholes from the bid/ask mid
  (scipy `brentq`), falling back to Yahoo's `impliedVolatility` when the mid is zero/crossed. Rate from
  `^IRX` (13-week T-bill) via the same provider, dividend yield from `quote`.
- **Expected move:** ATM straddle mid / spot, and σ√T.
- **Forward (ex-event) vol** between expiries T₁ < T₂: σ_f = √((σ₂²T₂ − σ₁²T₁)/(T₂ − T₁)). When the
  next earnings date falls before the first expiry, the tool reports the forward vol from the first
  expiry after the event to each later expiry; this is the "ex-earnings" vol used as the default for odds.
  Time in calendar years (ACT/365).
- **Realized vol:** close-to-close, 10/20/60 sessions, annualized with √252.
- **GARCH(1,1) forecast** (`arch`, Student-t errors, on 5 y of daily % log returns): annualized average vol
  over the horizon. Reported next to implied and realized vols; not used as a default.

## Odds (`_odds.py`, `_paths.py`)

Inputs: `levels[]` (absolute prices), `days` (trading sessions), `method`, optional `spot`, `vol`,
`drift` (annualized, default 0).

- **lognormal (closed form):** with b = ln(L/S), ν = μ − σ²/2, τ = days/252,
  P(touch) for L > S = Φ((−b + ντ)/(σ√τ)) + e^{2νb/σ²} Φ((−b − ντ)/(σ√τ)) (mirror for L < S);
  P(close beyond L at the horizon) from the terminal lognormal. Default σ: ex-event forward vol from
  `_vol`, else 20-day realized.
- **bootstrap:** `_paths.simulate(spot, days, n_paths, returns, seed)` builds paths by stationary block
  bootstrap (mean block 5 days) of the last 5 years of daily log returns, demeaned (drift is set by the
  `drift` argument), optionally rescaled to the target σ. Each day is split into an overnight gap and an
  intraday move, using the historical share of variance overnight (close→open vs. open→close, default
  0.3 if unavailable). Intraday touches between the open and the close use the Brownian-bridge crossing
  probability p = exp(−2·ln(L/x₀)·ln(L/x₁)/(σ_d²)) per step, so touch odds aren't understated by
  looking only at closes.
- `method="both"` returns both columns. Default `n_paths` 10 000, seed 0; `days × n_paths` capped
  at 2 000 000 (well within 29 s).

## Sell plans (`_plans.py`)

`evaluate_sell_plan(ticker, shares, tranches, deadline_days, …)` where each tranche is
`{fraction, type, price?, limit_price?}`:

- `market` — fills at `spot` on day 0.
- `limit` (sell at or above `price`) — fills at `price` the first day the intraday path touches it, or at
  the open if the day gaps above it.
- `stop_limit` (`price` = stop, `limit_price` ≤ stop) — triggers when the path touches the stop. Fills at
  the stop if touched intraday, at the open if it gaps below the stop but the open is still ≥ `limit_price`;
  otherwise it becomes a resting limit at `limit_price` (fills if the price later rises back to it).
- `bracket` — one tranche with `take_profit` and `stop` (+ `limit_price`), one-cancels-other.
- Anything unfilled at `deadline_days` sells at that day's close.

Fractions must sum to 1 (validated). Simulated on the same `_paths` engine (bootstrap by default,
`method="lognormal"` uses GBM paths with the same gap/intraday split).

Output: proceeds percentiles (5/25/50/75/95), mean, and standard deviation — all also as a difference
from `shares × spot`; per-tranche fill rate, mean fill price, mean days to fill; and the fixed note:
"With zero drift, no plan changes expected proceeds much; plans trade off the spread of outcomes.
Positive drift favors waiting, negative favors selling now." `plans[]` may hold several plans, compared
on the same paths so the differences aren't sampling noise.

## MCP tools (`mcp_server.py`)

All sync `def`, snake_case arguments, JSON-serializable dict results with `as_of`, `source: "yahoo"`,
`spot_source` where relevant.

| Tool | Wraps |
|---|---|
| `quote(ticker)` | `_data.quote` + next earnings date |
| `price_history(ticker, period="6mo", interval="1d", prepost=False)` | `_data.history`; ≤ 1 000 bars, newest kept |
| `earnings_event_study(ticker, horizon_days=20, anchor_day=2, day1_min=None, day1_max=None, surprise_min=None, surprise_max=None, since=None, levels_pct=None, include_all_events=False)` | `_events` |
| `implied_volatility(ticker, max_expiries=8)` | `_vol` ATM IV, expected moves, forward vols |
| `volatility_forecast(ticker, horizon_days=20)` | `_vol` realized + GARCH + implied side by side |
| `price_target_odds(ticker, levels, days=20, method="both", spot=None, vol=None, drift=0.0, n_paths=10000)` | `_odds` |
| `evaluate_sell_plan(ticker, shares, plans, deadline_days=20, method="bootstrap", spot=None, vol=None, drift=0.0, n_paths=10000)` | `_plans` |

## Deployment

- `template.yaml`: `MarketsMcpFunction` (Image, Timeout 29, MemorySize 1024, env
  `MARKETS_CACHE_DIR=/tmp/markets-cache`, `HOME=/tmp` for yfinance's own cache) + `MarketsHttpApi`
  with `/` and `/{proxy+}` events; output `MarketsMcpApiUrl`. Same `mcp-playground` stack.
- Dockerfile cloned from genealogy's (pinned base image and uv digests, hash-verified install).
- Deployment follows the CLAUDE.md change-set procedure; the change set should show only Add rows.
- Post-deploy smoke test: `quote AAPL` and `implied_volatility AAPL` through the deployed URL. If Yahoo
  blocks Lambda, record it and open a follow-up issue for a keyed fallback provider.

## Testing

TDD with `uv run --package markets pytest packages/markets/tests/`; no network in tests.

- `_odds`: closed-form touch probability vs. a fine-step GBM Monte Carlo (within 1 pp); symmetry and
  limit cases (L → S gives 1, σ → 0 gives 0).
- `_vol`: forward-vol formula vs. a hand calculation; Black–Scholes IV round trip; ATM selection.
- `_events`: synthetic price series with hand-placed before-open and after-close events → exact day-0
  alignment, `r_k`, `max_after`, filters, dropped-window count.
- `_paths` / `_plans`: deterministic seed; market tranche = spot exactly; limit at spot fills on day 0;
  bracket one-cancels-other; deadline sale; fractions validation; zero-drift mean proceeds ≈ shares × spot.
- `_data`: recorded fixtures through a fake provider; error mapping (429 → `DataSourceError`, empty →
  `UnknownTicker`); cache TTL behavior with an injected clock.
- App: the MCP `tools/list` and one `tools/call` through the httpx test client with the fake provider.

## Docs

CLAUDE.md gains a `markets` package section: data source and its Lambda-blocking risk, the spot
resolution rule, the forward-vol default, the bridge/gap model, and the tool list.

## Risks

- Yahoo blocking or rate-limiting Lambda IPs (mitigation: clear error; fallback provider as follow-up).
- yfinance breaking on Yahoo-side changes (pin a version; fixtures catch parsing regressions).
- Image size and cold start with pandas/scipy/arch (1024 MB memory; compileall in the image).
