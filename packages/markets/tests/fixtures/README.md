# Recorded AAPL fixtures

Captured once from Yahoo Finance (yfinance) on 2026-11-02, at about 00:36 ET on 2026-11-03 (23:36 CST on the 2nd), after Apple reported earnings (after the close on Nov 2). Tests never refetch them.

- `aapl_history.csv`: daily adjusted OHLCV, 2020-10-29 to 2026-11-02.
- `chains/<expiry>_{calls,puts}.csv`: the first six expiries (Nov 4, 6, 9, 11, 13, 20), strikes within +/-25% of spot, columns strike, bid, ask, lastPrice, impliedVolatility, openInterest, volume.
- `meta.json`: spot, capture date, 13-week rate and dividend yield (fraction).

The market was closed, so Yahoo returned bid = ask = 0 and a placeholder `impliedVolatility` for every option. The chains were rebuilt: rows with bid and ask both 0 got bid = 0.99 x lastPrice and ask = 1.01 x lastPrice (lastPrice is a regular-session trade). `meta.json` `spot` is therefore the Nov 2 regular close (338.32), consistent with those trades; the post-market price is kept as `spot_after_hours`. The `impliedVolatility` column is the raw Yahoo value and is not reliable.

The history, rate and dividend yield are unmodified.
