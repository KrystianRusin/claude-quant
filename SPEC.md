# Paper Day Trading Bot: Design Spec

## 1. Goal

Build an automated paper day trading system on Alpaca's free paper API. It runs a rule-based strategy each market morning, logs every trade, produces performance analytics, and has a nightly review job where Claude Code reads the results and tunes the strategy config within strict guardrails.

Hard constraints:

* Paper trading only. The code must refuse to run against a live endpoint (see section 9).
* No paid services. Use Alpaca's free paper account and free IEX market data. No Anthropic API key; the review loop runs through Claude Code on the user's existing subscription.
* Python 3.11+, minimal dependencies: `alpaca-py`, `pandas`, `numpy`, `python-dotenv`, `pytest`.
* The trading script itself is deterministic and makes no LLM calls. Claude is the strategy designer and reviewer, not a live decision maker.

## 2. Repo layout

```
paper-trader/
  SPEC.md
  README.md
  .env.example            # ALPACA_API_KEY, ALPACA_SECRET_KEY (never commit .env)
  .gitignore
  config.json             # the only file the review loop may edit
  config_bounds.json      # hard limits the review loop may never exceed
  trader.py               # entry point, runs at market open
  strategy.py             # signal logic, pure functions, no I/O
  broker.py               # thin Alpaca wrapper (data + orders)
  risk.py                 # sizing and daily limits
  logger.py               # trade and event logging
  analyze.py              # analytics CLI
  review/
    REVIEW_PROMPT.md      # instructions for the nightly Claude Code review
    run_review.sh         # headless invocation
  data/
    trades.csv            # one row per closed trade
    orders.csv            # one row per order event
    daily_summary.csv     # one row per trading day
    changelog.md          # every config change, with reasoning
    reports/              # dated analytics reports
  tests/
```

## 3. Strategy v1: Opening Range Breakout (ORB)

Defined by `config.json` (see the file for starting values).

Logic:

1. After the market opens (9:30 ET), wait `opening_range_minutes`, then compute each ticker's opening range high and low from 1-minute bars.
2. Skip tickers whose range as a percent of price is outside `[min_range_pct, max_range_pct]`.
3. Between the end of the range and `entry_window_end`, watch 1-minute bar closes. A close above the range high is a long signal (if `require_above_vwap_for_long`, also require close above session VWAP). A close below the range low is a short signal (if `allow_shorts`, and mirror the VWAP rule).
4. One entry per ticker per day. Entry is a market order sent as a bracket order: stop at the opposite side of the range, or at 1R, and take profit at `take_profit_r` multiples of R, where R = entry price minus stop price.
5. Any position still open at `force_exit_time` is closed at market and any open orders are canceled.
6. Stop taking entries for the day when `max_trades_per_day` or `max_daily_loss_pct` is hit.

`strategy.py` must be pure functions (bars in, signals out) so it is unit-testable and can later be backtested on historical bars.

## 4. Risk and sizing (`risk.py`)

* Position size = (equity × `risk_per_trade_pct`) / R-per-share, rounded down to whole shares.
* Cap notional at `max_position_pct_of_equity` of equity.
* Skip the trade if size rounds to zero.
* Check daily loss using realized plus unrealized P&L from the account endpoint. If it breaches `max_daily_loss_pct`, close everything and halt for the day.
* Shorting on paper requires shortable assets. Check `asset.shortable` and `asset.easy_to_borrow` before a short.

## 5. Execution flow (`trader.py`)

* Intended to run once per trading day, started by cron or Task Scheduler at about 9:25 ET. It is a long-running loop for the session, not a per-minute cron.
* On start: load config, validate it against `config_bounds.json`, check the market calendar (exit cleanly on holidays or weekends), confirm the account is paper.
* Main loop: poll 1-minute bars every ~30 seconds (or use the websocket stream if simple), evaluate signals, place orders, reconcile fills.
* Must be idempotent and restart-safe. If the process crashes and restarts mid-day, it reads today's open positions and orders from Alpaca and from `orders.csv` and does not double enter.
* On exit (normal or exception): ensure no orphan positions remain after `force_exit_time`, write the daily summary row.
* Use a lock file so two instances cannot run at once.
* All times handled in `America/New_York` with timezone-aware datetimes.

## 6. Logging (`logger.py`)

`data/trades.csv`, one row per closed trade:
`date, symbol, side, qty, entry_time, entry_price, exit_time, exit_price, exit_reason (tp|sl|time|halt), pnl_usd, pnl_r, range_pct, entry_minute_after_open, config_version`

`data/orders.csv`: every order submission, fill, cancel, and reject with timestamps and Alpaca order IDs.

`data/daily_summary.csv`: `date, trades, wins, losses, gross_pnl, equity_start, equity_end, halted (bool), notes`.

Every trade row records the `config_version` it ran under so performance can be compared across tweaks. Also log unexpected errors with tracebacks to `data/trader.log`.

## 7. Analytics (`analyze.py`)

CLI: `python analyze.py [--since YYYY-MM-DD] [--config-version N] [--out data/reports/]`

Output a markdown report and print it:

* Trade count, win rate, average win, average loss, expectancy in R and dollars, profit factor
* Equity curve stats: total return, max drawdown, longest losing streak
* Breakdown by ticker, by side (long vs short), by entry time bucket (15-minute buckets), by exit reason, and by `config_version`
* Comparison against a buy-and-hold SPY baseline for the same period
* A "sample size" warning line whenever n < 30 trades, stating that conclusions are not statistically meaningful
* Per-trade R-multiple distribution (text histogram is fine)

Include tests using synthetic trade data with known answers.

## 8. Automated review and tuning loop

Nightly after close (about 17:00 ET), a headless Claude Code run reviews performance and may edit `config.json`.

`review/run_review.sh` runs something like `claude -p "$(cat review/REVIEW_PROMPT.md)"` from the repo root, with tools limited to reading data files, running `analyze.py`, editing `config.json`, appending to `data/changelog.md`, and running git. The user can adjust the exact invocation.

`review/REVIEW_PROMPT.md` must instruct the reviewer to:

1. Run `analyze.py` and read the report, `daily_summary.csv`, and `changelog.md`.
2. Write a short dated entry to `data/changelog.md` with what it observed, even if it changes nothing.
3. Default to no change. Only modify `config.json` if all of these hold:
   * At least 30 closed trades under the current `config_version`, and at least 5 trading days since the last change.
   * There is a specific, evidenced pattern (for example, entries after 10:30 have negative expectancy over 20+ trades, or one ticker has lost more than 3R over 10+ trades).
   * The change is a single parameter, or a single watchlist add or remove, per review.
4. Stay within `config_bounds.json` (for example `risk_per_trade_pct` max 1.0, `max_daily_loss_pct` max 3.0, `take_profit_r` between 1.0 and 4.0, watchlist limited to liquid large caps and major ETFs, max 15 symbols).
5. Never increase risk parameters in response to losses. Risk-increasing changes are only allowed after a net-profitable stretch of 30+ trades.
6. Increment `config_version`, commit the change with a message that includes the reasoning, and record it in the changelog.
7. Guard against overfitting. Do not tune to a handful of trades, do not stack changes, and note when a result could plausibly be noise.
8. If expectancy is negative after 60+ trades across at least two config versions, do not keep patching. Write a recommendation in the changelog to switch strategy family (for example mean reversion on VWAP, or gap-and-go) and flag it for the user, who decides. The reviewer must not rewrite `strategy.py` unattended.
9. The reviewer must never edit `trader.py`, `broker.py`, `risk.py`, `config_bounds.json`, or the `.env` file.

`trader.py` validates `config.json` against `config_bounds.json` at startup and refuses to run if anything is out of bounds. This enforces the guardrails in code rather than trusting the prompt.

## 9. Safety rules (non-negotiable)

* `broker.py` constructs the client with `paper=True` and asserts the base URL contains `paper-api`. Any other value raises and exits.
* Credentials come only from environment variables or `.env`. Never log or print keys.
* Kill switch: if a file named `HALT` exists in the repo root, `trader.py` places no new orders and flattens positions.
* Hard cap on orders per day (for example 20) as a runaway-bug guard.
* Include a README note that nothing here is financial advice, paper fills are optimistic, and results over a short window are not evidence of skill.

## 10. Testing

* Unit tests for `strategy.py` (range calc, breakout detection, VWAP filter, one-entry-per-ticker), `risk.py` (sizing, caps, halt), config validation, and `analyze.py` (synthetic known results).
* A `--dry-run` flag on `trader.py` that evaluates signals on live data but logs hypothetical trades instead of sending orders.
* A `--replay YYYY-MM-DD` mode that runs the strategy over that day's historical 1-minute bars to simulate and log trades, for checking logic after hours.

## 11. Milestones (build in this order)

1. Repo scaffolding, config loading and bounds validation, tests.
2. `broker.py` with account check, market calendar, bar fetching; verify against a real paper account.
3. `strategy.py` and `risk.py` with full unit tests.
4. `logger.py` and `analyze.py` with synthetic-data tests.
5. `trader.py` with `--dry-run` and `--replay`; run replay on the last 10 trading days.
6. Live paper run with order placement, supervised for the first day.
7. Review loop scripts, `REVIEW_PROMPT.md`, and scheduling instructions (cron for Linux/macOS, Task Scheduler for Windows).
8. README with setup: Alpaca paper signup (confirm availability for Canadian residents), where to generate paper API keys, `.env` setup, install, scheduling.

## 12. Open items for the user to decide

* Watchlist: fixed list above, or add a pre-market gap scanner later.
* Whether the machine running this will be on at 9:25 ET daily (a laptop that sleeps will miss runs).
* Whether the nightly review should auto-commit config changes or only propose them for approval. Default in this spec is auto-commit within bounds.
