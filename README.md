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

### 5. Git

The nightly review commits config changes, so the repo must be a git repository with at least
one commit and a configured `user.name` / `user.email`.

## Daily use

| Command | What it does |
|---|---|
| `python trader.py` | Live paper session for today. Start around 09:20 ET; it waits for the open and exits after the force-exit time. |
| `python trader.py --dry-run` | Live data, simulated fills, writes to `data/dry_run/`. Places no orders. |
| `python trader.py --replay 2026-10-05` | Simulates a past day from historical bars, writes to `data/replay/`. Several dates allowed. |
| `python trader.py --replay-last 10` | Simulates the last 10 completed trading days. |
| `python trader.py --flatten` | Cancels all open orders and closes all positions now. |
| `python analyze.py` | Prints a markdown report and saves it to `data/reports/`. Options: `--since`, `--config-version`, `--strategy`, `--data-dir`, `--no-baseline`. |
| `python config.py` | Validates `config.json` against `config_bounds.json`. |

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
| `data/reports/` | Analytics reports and nightly review logs. |

The CSVs are git-ignored. Back them up if you move machines.

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

`crontab -e`, with the system clock on Eastern time (or adjust the hours):

```cron
20 9 * * 1-5  /path/to/claude-trade/scripts/run_trader.sh >> /path/to/claude-trade/data/cron.log 2>&1
0 17 * * 1-5  /path/to/claude-trade/review/run_review.sh
```

## Nightly review loop

`review/run_review.sh` runs `claude -p` with [review/REVIEW_PROMPT.md](review/REVIEW_PROMPT.md)
and a limited tool allowlist. The reviewer reads the report and logs, always writes a dated
changelog entry, and changes things only when the evidence is strong enough:

- One parameter or one watchlist change per review, only with 30+ trades over 10+ trading days
  under the current config version. Risk increases only after a net-profitable stretch.
- It may write a new strategy module under `strategies/` (with tests and a 10-day in-sample replay)
  at most once per 10 trading days.

After Claude exits, `review/guard.py` re-checks every rule in code and reverts anything that fails:
files outside the allowlist, edits to the trade data, an invalid config, more than one change,
too little evidence, risk increases after losses, strategy edits without a version bump, failing
tests, or a failing replay. The run log goes to `data/reports/review_<date>.log`.

`REVIEW_MODE=propose review/run_review.sh` makes the reviewer write proposals into the changelog
instead of editing anything, if you prefer to approve changes yourself.

### Shadow mode (optional, off by default)

If the reviewer (or you) writes `config.shadow.json` selecting a candidate strategy, the scheduled
launcher also runs it in `--dry-run` alongside the live strategy, logging to `data/shadow/`.
Compare with `python analyze.py --data-dir data/shadow --no-baseline --out ""`. Delete the file to
turn shadow mode off.

## Strategies

Each strategy is a module `strategies/<name>.py` exposing `NAME`, `VERSION`, `PARAMS` (bounds for
its own config keys) and `generate_signals(bars, state, params)`. It returns signals only: symbol,
side, reference price, stop and target. Sizing, stop-distance limits, order caps and halts live in
`risk.py` and `trader.py`. `config.json` selects the active strategy with `strategy.name`, and
`strategy_version` must match the module's `VERSION`.

## Swapping the broker

`broker.py` defines a `Broker` interface (`get_account`, `get_bars`, `submit_bracket_order`,
`list_positions`, `close_all`, `is_market_open` and a few order helpers) and the Alpaca backend.
To add another backend, such as Interactive Brokers paper or a simulator fed by free delayed data,
implement `Broker`, add it to `BACKENDS` in `broker.py`, and set `BROKER_BACKEND=<name>` in `.env`.
`simbroker.SimBroker` already implements the interface on top of any bar source and is what
`--dry-run` and `--replay` use, so it is a starting point for a fully local backend.
Strategies and `risk.py` do not change.

## Open decisions

- Watchlist: the fixed list in `config.json`, or a pre-market gap scanner later.
- Whether this machine will reliably be on at 09:20 ET every trading day.
- Whether the nightly review auto-commits changes (default) or only proposes them (`REVIEW_MODE=propose`).

## Tests

See [tests/README.md](tests/README.md).
