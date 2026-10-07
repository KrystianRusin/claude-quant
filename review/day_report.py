"""Facts about one trading day, as markdown, for the nightly review to write its journal from.

python review/day_report.py [--date YYYY-MM-DD] [--data-dir data] [--no-market]
"""
import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_LOG_LINES = 40


def _rows(path, day):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("date") == day.isoformat()]


def _time(ts):
    return ts[11:16] if ts else "?"


def log_lines(path, day, max_lines=MAX_LOG_LINES):
    """WARNING and ERROR lines from trader.log for `day`."""
    if not path.exists():
        return []
    prefix = day.isoformat()
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip() for ln in f if ln.startswith(prefix) and (" WARNING " in ln or " ERROR " in ln)]
    if len(lines) > max_lines:
        return lines[:max_lines] + [f"... {len(lines) - max_lines} more"]
    return lines


def market_move(day):
    """SPY open-to-close move and intraday range in percent, or an explanation string."""
    try:
        from broker import make_broker

        start = datetime.combine(day, datetime.min.time())
        bars = make_broker().daily_bars("SPY", start, start + timedelta(days=1))
        if bars is None or bars.empty:
            return "no SPY bar returned"
        b = bars.iloc[0]
        move = (b["close"] / b["open"] - 1) * 100
        rng = (b["high"] - b["low"]) / b["open"] * 100
        return f"SPY {move:+.2f}% open to close, intraday range {rng:.2f}%"
    except Exception as exc:  # the report is still useful without it
        return f"unavailable ({type(exc).__name__})"


def build_day_report(data_dir, day, market=None):
    data_dir = Path(data_dir)
    trades = _rows(data_dir / "trades.csv", day)
    orders = _rows(data_dir / "orders.csv", day)
    summary = _rows(data_dir / "daily_summary.csv", day)
    lines = [f"# Day report {day}", ""]

    if summary:
        s = summary[-1]
        lines += [f"- Equity {s['equity_start']} -> {s['equity_end']}, halted: {s['halted']}",
                  f"- Notes: {s['notes'] or 'none'}"]
    else:
        lines.append("- No daily summary row (no session ran, or it has not finished).")
    lines.append(f"- Market: {market or 'not requested'}")

    lines += ["", "## Trades", ""]
    if trades:
        pnl = [float(t["pnl_usd"]) for t in trades]
        r = [float(t["pnl_r"]) for t in trades if t["pnl_r"] not in ("", None)]
        lines += [f"{len(trades)} trades, {sum(p > 0 for p in pnl)} wins, {sum(p < 0 for p in pnl)} losses, "
                  f"net ${sum(pnl):,.2f}, net {sum(r):+.2f}R", "",
                  "| symbol | side | qty | entry | exit | reason | pnl $ | R | range % | strategy |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for t in sorted(trades, key=lambda t: t["entry_time"] or t["exit_time"]):
            lines.append(f"| {t['symbol']} | {t['side']} | {t['qty']} | {_time(t['entry_time'])} @ {t['entry_price']} "
                         f"| {_time(t['exit_time'])} @ {t['exit_price']} | {t['exit_reason']} | {t['pnl_usd']} "
                         f"| {t['pnl_r'] or '-'} | {t['range_pct'] or '-'} | {t['strategy']} v{t['strategy_version']} |")
    else:
        lines.append("No closed trades.")

    skips = [o for o in orders if o["event"] in ("skip", "reject")]
    lines += ["", "## Skipped or rejected signals", ""]
    lines += [f"- {_time(o['timestamp'])} {o['symbol']} {o['side']}: {o['event']} ({o['reason']})" for o in skips] or ["None."]

    warnings = log_lines(data_dir / "trader.log", day)
    lines += ["", "## Log warnings and errors", ""]
    lines += [f"    {ln}" for ln in warnings] or ["None."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--no-market", action="store_true")
    args = ap.parse_args(argv)
    market = None if args.no_market else market_move(args.date)
    print(build_day_report(args.data_dir, args.date, market))
    return 0


if __name__ == "__main__":
    sys.exit(main())
