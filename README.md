# claude-quant

An automated day trader that runs on an Alpaca paper account, plus a nightly loop in which Claude
reviews the results and tunes the strategy. I built it to see how well Claude can develop and run
trading strategies when its changes have to clear real evidence checks.

The trading process is deterministic Python and makes no LLM calls. Claude only works after the
close, through a headless `claude -p` run whose every change is re-checked in code and reverted if
it breaks a rule.

Paper fills are optimistic (no slippage, queue position or market impact), and results over a short
window say little about skill. This is not financial advice.

## How it works

A weekday looks like this:

1. At 09:00 `trader.py` starts, waits for the open, and runs the active strategy on 1-minute bars
   from the watchlist. Signals become bracket orders (entry, stop and target). Everything is closed
   15 minutes before the close.
2. At 17:00 `review/run_review.sh` runs Claude with [review/REVIEW_PROMPT.md](review/REVIEW_PROMPT.md)
   and a short tool allowlist. It writes a journal entry, updates its memory, and may change the
   config, swap watchlist symbols or write a new strategy.
3. `review/guard.py` compares `data/` against a snapshot taken before the review and reverts
   anything that breaks a rule.

The repo is the framework. Everything produced at runtime, including Claude's strategies, config,
memory and journal, lives in the gitignored `data/` folder and is never committed.
`python config.py` seeds `data/` from `defaults/` on a fresh install.

## Guardrails on the review

Claude can change things, but the guard enforces the limits in code:

- At most one parameter change per review, and only after 30+ trades over 10+ trading days on the
  current config. Risk can only go up after a profitable stretch.
- Every change has to rest on a hypothesis written into memory on an earlier day, as a trade filter
  such as `entry_minute_after_open >= 60`. The guard re-runs
  `analyze.py --evidence "<filter>" --registered <date>` and needs 20+ matching trades over 10+
  days, a 95% range that excludes zero (trades grouped by day), and 10+ trades since registration
  pointing the same way. Registering hypotheses first stops the reviewer from mining the data until
  something looks significant by chance.
- A new strategy needs its own tests and a 10-day replay, at most once every 10 trading days.
- Watchlist swaps come from a nightly screen of every US stock and ETF (`review/universe.py`), at
  most 3 adds and 3 removes per review.
- Memory entries move through `hypothesis`, `supported`, `confirmed` and `retired`. They are never
  deleted, and past journal entries are never edited.

The guard also reverts edits to framework code or trade data, invalid configs, and strategies that
fail their tests or replay. `REVIEW_MODE=propose` makes the reviewer write proposals to the
changelog instead of applying them.

## Strategies

A strategy is a module in `data/strategies/` with `NAME`, `VERSION`, `INTERFACE`, `PARAMS` and
`generate_signals(bars, state, params)`. It only returns signals. Sizing, stop limits, order caps
and halts live in `risk.py` and `trader.py`, so a strategy cannot size its own risk. A fresh
install starts with `defaults/strategies/opening_range_breakout.py`. The interface is defined in
`strategies/base.py`, and the registry refuses strategies written for an older interface version.

## Safety

- Paper only. The Alpaca client refuses non-paper endpoints, and any live account needs
  `ALLOW_LIVE=1`.
- `data/config.json` is validated against `config_bounds.json` on every start.
- The session halts and flattens on the daily loss limit, 60 orders a day, or repeated errors.
- Creating a `HALT` file in the repo root stops new orders and flattens everything.
- A lock file prevents two sessions from running at once, and restarts rebuild state from the
  broker so a symbol is never entered twice.

## Broker abstraction

`broker.py` defines a broker-neutral interface with two roles, `MarketData` and `ExecutionBroker`,
so orders can go to one broker while bars come from another. `alpaca_backend.py` implements both.
`ibkr_backend.py` is an Interactive Brokers stub, since Alpaca's live brokerage isn't available in
Canada. Every backend has to pass the contract suite in `tests/test_broker_contract.py` before it
trades.

## Data

Trades, orders and daily summaries are written as CSV and mirrored into SQLite (`data/trader.db`).
The database also holds equity snapshots, order states, job runs, every version of the config,
strategies and memory, and saved price bars. It feeds
[claude-desk](https://github.com/KrystianRusin/claude-desk), the dashboard. Migrations are numbered
SQL files in `migrations/`. The first three are shared with claude-sentiment-trading and pinned by
hash in both repos. A nightly job backs the database up and purges old bars and snapshots.

## Deployment

The bot runs on a Raspberry Pi under cron. Every push runs the tests on GitHub Actions, and the Pi
pulls new commits itself; nothing pushes to it.

- `scripts/deploy.sh` runs every 5 minutes. When `origin/main` has a new commit that passed CI, it
  fast-forwards, migrates the database, runs the checks and reinstalls cron. If a check fails it
  rolls back and skips that commit.
- Jobs and deploys share a lock (`scripts/job.sh`), so code never changes under a running session
  or review.
- `scripts/cron.sh` holds the schedule and is reinstalled on every deploy.

## Setup

You need Python 3.11+ and an Alpaca paper account with API keys.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys)
.venv/bin/python broker.py    # check the connection
.venv/bin/python config.py    # create data/ from the defaults
```

On the Pi, `scripts/setup.sh` does the same and installs the cron schedule. For Windows,
`scripts/run_trader.ps1` and `scripts/run_review.ps1` work with Task Scheduler.

## Commands

| Command | What it does |
|---|---|
| `python trader.py` | Today's paper session. Waits for the open and exits after the close. |
| `python trader.py --dry-run` | Live data with simulated fills. Places no orders. |
| `python trader.py --replay 2026-10-05` | Simulates a past day from historical bars. |
| `python trader.py --replay-last 10` | Simulates the last 10 trading days. |
| `python trader.py --flatten` | Cancels all orders and closes all positions. |
| `python analyze.py` | Performance report (`--since`, `--config-version`, `--strategy`). |
| `python lifetime.py` | Rebuilds the all-time statistics in `data/lifetime.md`. |
| `python db.py check` | Checks the database against the CSV files. |

## Tests

```bash
.venv/bin/python -m pytest -q
```

No network or credentials needed. See [tests/README.md](tests/README.md).
