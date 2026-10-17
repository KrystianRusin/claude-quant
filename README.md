# Paper day trader

An automated **paper** day trading system on Alpaca's free paper API. Each market morning it runs a
rule-based strategy (opening range breakout to start), logs every order and trade, and produces
performance reports. A nightly headless Claude Code run reviews the results and may tune the
config or add a strategy, inside limits enforced in code. The trading process itself is
deterministic and makes no LLM calls. See [SPEC.md](SPEC.md) for the full design.

> **Not financial advice.** This is an experiment on a paper account. Paper fills are optimistic:
> no slippage, queue position, partial fills or market impact, and the simulated fills used by
> `--dry-run` and `--replay` are more optimistic still. Results over a short window, good or bad,
> are not evidence of skill.
> This is purely for me to test and see how good Claude can actually be at developing trading strategies and executing them

## Setup

### 1. Alpaca paper account (Canadian residents)

Alpaca does not offer live brokerage or paid data to Canadians, but a paper-only account with the
free IEX feed should work. Everything here runs on that free tier.

1. Go to <https://alpaca.markets> and sign up with an email address. Pick your country of residence.
   **If Canada is not in the country list, stop here**: Alpaca is not available to you, and you
   need another backend (see [Swapping the broker](#swapping-the-broker)).
2. Do not fund anything or apply for a live account. The paper account exists as soon as you sign up.
3. In the dashboard, switch to the **Paper** account (top-left account selector), open
   **API Keys** on the home page, and click **Generate New Keys**. Copy the key and secret; the
   secret is shown once.

### 2. Install

Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

On Linux/macOS use `.venv/bin/python` instead of `.venv/Scripts/python` everywhere.

### 3. Credentials

```bash
cp .env.example .env
```

Fill in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` with the **paper** keys. `.env` is git-ignored;
never commit it. Keys are only read from the environment or `.env` and are never printed.

### 4. Check the connection

```bash
.venv/Scripts/python broker.py
```

This confirms the endpoint is paper, prints the account, recent trading days and a few SPY
1-minute bars from IEX.

### 5. Runtime state

```bash
.venv/Scripts/python config.py
```

The repo is the framework. Everything the system produces at runtime lives in `data/`, which is
gitignored and never committed: the active config and watchlist, Claude's strategies and their
tests, memory, journal, changelog, trade logs and reports. `python config.py` creates whatever is
missing from the seeds in `defaults/` (it never overwrites), merges config keys added by a framework
update, and validates the result. The trader runs it on every start too.

| Framework (git) | Runtime state (`data/`, never committed) |
|---|---|
| Code, `strategies/base.py` (the strategy interface) and the registry | `data/strategies/*.py`, `data/tests/` |
| `config_bounds.json` (hard limits) | `data/config.json`, `data/watchlist.json`, `data/config.shadow.json` |
| `defaults/` (seeds for a fresh install) | `data/memory.md`, `data/changelog.md`, `data/journal/` |
| `tests/` (framework tests, run in CI) | trades, orders, summaries, logs, reports, `data/strategy_history/` |

## Daily use

| Command | What it does |
|---|---|
| `python trader.py` | Live paper session for today. Start around 09:20 ET; it waits for the open and exits after the force-exit time. |
| `python trader.py --dry-run` | Live data, simulated fills, writes to `data/dry_run/`. Places no orders. |
| `python trader.py --replay 2026-10-05` | Simulates a past day from historical bars, writes to `data/replay/`. Several dates allowed. |
| `python trader.py --replay-last 10` | Simulates the last 10 completed trading days. |
| `python trader.py --flatten` | Cancels all open orders and closes all positions now. |
| `python analyze.py` | Prints a markdown report and saves it to `data/reports/`. Options: `--since`, `--config-version`, `--strategy`, `--data-dir`, `--no-baseline`. |
| `python config.py` | Validates `config.json` and `watchlist.json` against `config_bounds.json`. |
| `python lifetime.py` | Rebuilds `data/lifetime.md`, the all-time statistics. |
| `python review/universe.py` | Rebuilds `data/universe.json`, the screened list the watchlist is picked from (about 4 minutes). |

Use the venv's Python (`.venv/Scripts/python`) or activate the venv first.

### What a live session does

1. Validates `config.json` against `config_bounds.json` and refuses to run if anything is out of bounds.
2. Exits on weekends and holidays (Alpaca market calendar). Confirms the endpoint is paper.
3. Takes a lock (`data/trader.lock`) so only one instance runs.
4. Rebuilds today's state from Alpaca and `data/orders.csv`, so a restart never enters a symbol twice.
5. Cancels orders and flattens positions left over from an earlier day (`exit_reason = carryover`).
6. Polls 1-minute bars every 30 seconds and sends bracket orders for fresh signals until the entry
   window ends.
7. Closes everything at the force-exit time (15 minutes before an early close) and writes the
   daily summary row.

It stops new entries for the day after `max_trades_per_day`, and halts and flattens on any of these:
the daily loss limit (realized plus unrealized), the 60-orders-per-day cap, repeated errors, or the
kill switch.

**Kill switch:** create an empty file named `HALT` in the repo root. The trader places no new
orders, flattens all positions and ends the session. Delete it to trade again the next day.

**Use a dedicated paper account.** The end-of-day safety net closes every position in the account,
including ones you opened by hand.

### Data files

| File | Contents |
|---|---|
| `data/trades.csv` | One row per closed trade, with `config_version`, `strategy` and `strategy_version`. |
| `data/orders.csv` | Every order submit, fill, cancel, reject and skipped signal. |
| `data/daily_summary.csv` | One row per trading day. |
| `data/trader.log` | Session log with error tracebacks. |
| `data/changelog.md` | Every review entry and config or strategy change, with reasoning. |
| `data/lifetime.md` | All-time record since day one: totals, equity and drawdown, records, monthly and yearly tables, per strategy and config version. Regenerated before each nightly review. |
| `data/universe.json` | Tonight's screened universe with liquidity and movement stats. |
| `data/reports/` | Analytics reports and nightly review logs. |
| `data/strategy_history/` | Every accepted version of every strategy, as `<name>_v<VERSION>.py`. |

All of `data/` is git-ignored, including Claude's config, strategies and memory. Back it up if you
move machines.

## Scheduling

Ontario is on Eastern time, so local times below are ET. The machine must be awake at 09:20 ET
and stay awake until about 16:00. A sleeping laptop misses the run, and any position it leaves
open is flattened as carryover the next morning.

### Windows (Task Scheduler)

Trader, weekdays at 09:20 (holidays exit on their own):

```powershell
schtasks /Create /TN "PaperTrader" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\kryst\Downloads\repos\claude-trade\scripts\run_trader.ps1"
```

Nightly review, weekdays at 17:00 (needs Git Bash and the `claude` CLI logged in):

```powershell
schtasks /Create /TN "PaperTraderReview" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:00 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\kryst\Downloads\repos\claude-trade\scripts\run_review.ps1"
```

Then open Task Scheduler and, for both tasks, tick **Conditions → Wake the computer to run this
task** and untick **Start the task only if the computer is on AC power** if you are on a laptop.
Also set the power plan so the machine does not sleep during market hours.

### Linux / macOS (cron)

`scripts/cron.sh install` (run by `scripts/setup.sh`) installs the schedule, with the system clock
on Eastern time: the trader weekdays at 09:00, the review weekdays at 17:00, and the deploy check
every 5 minutes. `scripts/cron.sh print` shows the lines.

## Continuous deployment

Every push and pull request runs the tests on GitHub Actions (`.github/workflows/ci.yml`, Python 3.11
like the Pi). The Pi pulls changes; GitHub never pushes to it (no inbound access, and no self-hosted runner, which is unsafe on a public repo):

- `scripts/cron.sh` holds the whole schedule. `setup.sh` installs it, and every deploy reinstalls it, so schedule changes ship like code.
- `scripts/deploy.sh` runs every 5 minutes. When `origin/main` has a new commit and CI passed for it,
  it takes the deploy lock, fast-forwards, runs `pip install` if `requirements.txt` changed, runs the
  checks on the Pi, and reinstalls cron. If a check fails it rolls back to the previous commit
  (restoring the config too) and does not retry that commit. It refuses to deploy over local commits
  or local changes, because the checkout should always equal `origin/main`.
- Every scheduled job runs through `scripts/job.sh`, which holds a shared lock on `data/deploy.lock`.
  A deploy only starts when no job holds it, and a job that starts during a deploy waits for it. So
  code never changes under a running trading session or review.
- `data/` is never touched beyond what the checks do (merging new config keys), so Claude's state
  survives every deploy.

Settings in `.env`: `NTFY_TOPIC` for deploy notifications, `DEPLOY_GITHUB_TOKEN` (a fine-grained
read-only token with Checks access, needed for private repos), and `DEPLOY_CI_CHECK=skip` to deploy
without waiting for CI. Logs: `data/deploy.log`, plus the output of the last checks in `data/deploy_checks.log`.


## Nightly review loop

`review/run_review.sh` runs `claude -p` with [review/REVIEW_PROMPT.md](review/REVIEW_PROMPT.md)
and a limited tool allowlist. The reviewer reads the report and logs, always writes a dated
changelog entry, and changes things only when the evidence is strong enough:

- One parameter change per review, only with 30+ trades over 10+ trading days under the current
  config version. Risk increases only after a net-profitable stretch.
- The change must rest on a hypothesis written into memory on an earlier day as a trade filter
  (for example `entry_minute_after_open >= 60`). The guard re-runs
  `python analyze.py --evidence "<filter>" --registered <date>`: 20+ matching trades over 10+ days,
  a 95% range that excludes zero, and 10+ trades since it was written down pointing the same way.
  Ranges group trades by day, since same-day trades are partly the same bet. Writing hypotheses
  down first stops the reviewer from searching until something looks significant by chance.
- It may write a new strategy module under `data/strategies/` (with tests in `data/tests/` and a
  10-day in-sample replay) at most once per 10 trading days.

Nothing is committed. Before Claude starts, `review/guard.py snapshot` hashes and copies `data/`.
After Claude exits, `review/guard.py check` re-checks every rule in code against that snapshot and
reverts anything that fails: files outside the allowlist, any change to framework code, edits to the
trade data, an invalid config, more than one change, too little evidence, risk increases after
losses, strategy edits without a version bump, a strategy written for another interface version,
failing tests, or a failing replay. Accepted strategy versions are archived to
`data/strategy_history/`. The run log goes to `data/reports/review_<date>.log`.

### Watchlist and universe

Claude picks the watchlist. Before each review, `review/universe.py` screens every US stock and ETF
on free full-market daily bars: price at least $10, at least $300M average daily dollar volume,
leveraged and inverse ETFs excluded, top 300 kept, with each symbol's average range and gap.
The reviewer can swap symbols in `data/watchlist.json` from that list: at most 3 adds and 3 removes per
review, 5 to 25 symbols, each with the date and reason, removals logged, and a 10-day wait before
a removed symbol returns. Swaps do not bump the config version, so parameter evidence keeps
accumulating. The screening criteria and swap limits live in `config_bounds.json`.

### Journal and memory

Each review starts by reading `data/memory.md`, then runs `python review/day_report.py` (the day's
trades, skipped signals, halts, log warnings and the SPY move) and writes `data/journal/<date>.md`:
what went well, what went wrong, luck versus logic, and execution problems. Lessons worth keeping
across days go into `data/memory.md`, each with a status (`hypothesis`, `supported`, `confirmed`,
`retired`) and its evidence. The guard enforces that past journal entries are never edited, memory
entries are retired rather than deleted, and there are at most 40 active entries. Memory informs the
review but never overrides the evidence thresholds for config changes.

`REVIEW_MODE=propose review/run_review.sh` makes the reviewer write proposals into the changelog
instead of editing anything, if you prefer to approve changes yourself.

### Shadow mode (optional, off by default)

If the reviewer (or you) writes `data/config.shadow.json` selecting a candidate strategy, the scheduled
launcher also runs it in `--dry-run` alongside the live strategy, logging to `data/shadow/`.
Compare with `python analyze.py --data-dir data/shadow --no-baseline --out ""`. Delete the file to
turn shadow mode off.

## Strategies

Strategies are runtime state, written by Claude: each is a module `data/strategies/<name>.py`
exposing `NAME`, `VERSION`, `INTERFACE`, `PARAMS` (bounds for its own config keys) and
`generate_signals(bars, state, params)`. It returns signals only: symbol, side, reference price, stop
and target. Sizing, stop-distance limits, order caps and halts live in `risk.py` and `trader.py`.
`data/config.json` selects the active strategy with `strategy.name`, and `strategy_version` must
match the module's `VERSION`. A fresh install is seeded with `defaults/strategies/opening_range_breakout.py`.

The framework side is `strategies/base.py` (the interface) and `strategies/__init__.py` (the
registry, which imports modules from `data/strategies/` as `strategies.<name>`). `INTERFACE` must
equal `INTERFACE_VERSION` in `base.py`. Raise `INTERFACE_VERSION` whenever a framework change breaks
the contract; the registry then refuses strategies written for the old one instead of running them.

## Swapping the broker

`broker.py` defines the broker-neutral interface and nothing Alpaca-specific. A backend fills one
or both roles:

- `MarketData`: calendar, 1-minute and daily bars, asset flags, the equity universe.
- `ExecutionBroker`: account, bracket and market orders, cancels, positions, `close_all`, and
  `is_paper()`. It declares `capabilities` (today only `native_brackets`).

`trader.py` gets one `Broker` that combines the two, so orders can go to one broker while bars come
from another. Backends translate their order statuses into the neutral ones in `broker.py` (`new`,
`held`, `partially_filled`, `pending_cancel`, `filled`, `canceled`, `rejected`, `expired`) and their
errors into `BrokerError` subclasses (`DuplicateOrderError`, `OrderRejectedError`, `LiveAccountError`).
Only the backend modules import a broker SDK.

| `.env` setting | Default | Meaning |
|---|---|---|
| `BROKER_BACKEND` | `alpaca` | where orders go: `alpaca` (paper only) or `ibkr` (a stub, see below) |
| `DATA_BACKEND` | `alpaca` | where bars and the calendar come from |
| `ALLOW_LIVE` | unset | must be exactly `1` before an execution backend on a live account may run |

- `alpaca_backend.py` is the Alpaca backend for both roles. It always uses the paper endpoint.
- `ibkr_backend.py` is an Interactive Brokers stub. It can be selected and reports paper or live
  (`IBKR_ACCOUNT` starting `DU`, or ports 4002/7497), so the live gate already applies, but every
  account and order call raises `NotImplementedError`. Its docstring lists what the real backend
  needs (IB Gateway on the Pi, `orderRef` as the client order id, native brackets, a status map).
- The trader refuses a backend without native brackets. It relies on the broker canceling the other
  exit leg when one fills, and emulating that by polling could leave both legs filled.

To add a backend, implement the role classes, register it in `EXECUTION_BACKENDS` or
`DATA_BACKENDS` in `broker.py`, and add a harness to `tests/test_broker_contract.py`. That suite is
the contract the trader relies on (fills, duplicate ids, quiet second cancels, OCO legs, `close_all`),
and SimBroker and Alpaca (on a fake client) both pass it. A backend must pass it before it trades.
Strategies and `risk.py` do not change.

## Open decisions

- Watchlist: picked by the reviewer from the nightly universe. A pre-market gap scanner could come later.
- Whether this machine will reliably be on at 09:20 ET every trading day.
- Whether the nightly review applies changes that pass the guard (default) or only proposes them (`REVIEW_MODE=propose`).

## Tests

See [tests/README.md](tests/README.md).
