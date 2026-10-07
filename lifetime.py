"""All-time statistics since the first trading day, written to data/lifetime.md.

python lifetime.py [--data-dir data] [--out data/lifetime.md] [--no-baseline]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from analyze import (DEFAULT_EQUITY, _fmt, _table, add_keys, breakdown, equity_stats, expectancy_ci,
                     filter_trades, fmt_range, range_note, spy_baseline, summary_stats)
from logger import read_trades_frame


def read_summary(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=["date", "trades", "gross_pnl", "equity_start", "equity_end", "halted"])
    s = pd.read_csv(path)
    s["date"] = pd.to_datetime(s["date"]).dt.date
    return s.sort_values("date").reset_index(drop=True)


def streaks(values):
    """Longest run of positive and of negative values."""
    best = worst = up = down = 0
    for v in values:
        up, down = (up + 1, 0) if v > 0 else (0, down + 1) if v < 0 else (0, 0)
        best, worst = max(best, up), max(worst, down)
    return best, worst


def period_table(trades, summary, period):
    """Per month ('M') or year ('Y'): days, trades, win rate, net $, return % and expectancy."""
    if trades.empty:
        return "(none)"
    t = trades.assign(_p=pd.to_datetime(trades["date"]).dt.to_period(period),
                      _r=pd.to_numeric(trades["pnl_r"], errors="coerce"), _pnl=trades["pnl_usd"].astype(float))
    g = t.groupby("_p")
    table = pd.DataFrame({"days": g["date"].nunique(), "trades": g.size(),
                          "win_rate": g["_pnl"].apply(lambda s: (s > 0).mean()),
                          "net_usd": g["_pnl"].sum(), "expectancy_r": g["_r"].mean()})
    returns = {}
    if not summary.empty:
        s = summary.assign(_p=pd.to_datetime(summary["date"]).dt.to_period(period))
        for p, rows in s.groupby("_p"):
            start, end = rows["equity_start"].iloc[0], rows["equity_end"].iloc[-1]
            if pd.notna(start) and pd.notna(end) and start:
                returns[p] = (end / start - 1) * 100
    table["return"] = [f"{returns[p]:+.2f}%" if p in returns else "n/a" for p in table.index]
    lines = ["| period | days | trades | win rate | net $ | return | expectancy |", "|---|---|---|---|---|---|---|"]
    for p, r in table.iterrows():
        lines.append(f"| {p} | {r['days']} | {r['trades']} | {_fmt(r['win_rate'], 'pct')} | "
                     f"{_fmt(r['net_usd'], 'usd')} | {r['return']} | {_fmt(r['expectancy_r'], 'r')} |")
    return "\n".join(lines)


def version_table(trades, key, label):
    frame = breakdown(trades, key)
    if frame.empty:
        return "(none)"
    spans = trades.groupby(key)["date"].agg(["min", "max"])
    frame.insert(0, "dates", [f"{spans.loc[i, 'min']} to {spans.loc[i, 'max']}" for i in frame.index])
    frame.index.name = label
    return _table(frame)


def build_lifetime(trades, summary, baseline=None, today=None):
    today = today or date.today()
    lines = [f"# Lifetime statistics", "", f"Generated {today}. Paper trading; not evidence of skill on "
             "its own. Every expectancy comes with a 95% range grouped by trading day.", ""]
    if trades.empty:
        return "\n".join(lines + ["No closed trades yet."]) + "\n"

    trades = add_keys(trades)
    first = min(trades["date"].min(), summary["date"].min() if not summary.empty else trades["date"].max())
    start_equity = float(summary["equity_start"].iloc[0]) if not summary.empty else DEFAULT_EQUITY
    current = float(summary["equity_end"].iloc[-1]) if not summary.empty else None
    s, ci = summary_stats(trades), expectancy_ci(trades)
    e = equity_stats(trades, start_equity)
    equity_curve = start_equity + trades["pnl_usd"].astype(float).cumsum()
    high_water = max(start_equity, float(equity_curve.max()))
    now_equity = current if current is not None else e["end_equity"]
    day_pnl = trades.groupby("date")["pnl_usd"].apply(lambda x: x.astype(float).sum())
    win_days, lose_days = streaks(day_pnl.tolist())
    pnl = trades["pnl_usd"].astype(float)
    best_t, worst_t = trades.loc[pnl.idxmax()], trades.loc[pnl.idxmin()]
    sessions = len(summary) if not summary.empty else trades["date"].nunique()
    halted = int(summary["halted"].astype(str).str.lower().eq("true").sum()) if not summary.empty else 0

    lines += [
        "## All time", "",
        f"- Running since {first} ({sessions} sessions, {s['trading_days']} with trades, {halted} halted)",
        f"- Trades: {s['n']} ({s['wins']} wins, {s['losses']} losses, win rate {_fmt(s['win_rate'], 'pct')})",
        f"- Expectancy: {_fmt(s['expectancy_r'], 'r')} / {_fmt(s['expectancy_usd'], 'usd')} per trade, "
        f"95% range {fmt_range(ci)}{range_note(ci)}",
        f"- Profit factor: {_fmt(s['profit_factor'])}",
        f"- Net P&L: {_fmt(s['total_pnl'], 'usd')}",
        "", "## Equity", "",
        f"- Start {_fmt(start_equity, 'usd')}, now {_fmt(now_equity, 'usd')} "
        f"({(now_equity / start_equity - 1) * 100:+.2f}%)",
        f"- High-water mark {_fmt(high_water, 'usd')}; current drawdown "
        f"{_fmt(high_water - now_equity, 'usd')} ({(1 - now_equity / high_water) * 100:.2f}%)",
        f"- Max drawdown {_fmt(e['max_drawdown_usd'], 'usd')} ({e['max_drawdown_pct']:.2f}%)",
    ]
    if isinstance(baseline, (int, float)):
        lines.append(f"- SPY buy-and-hold over the same period: {baseline:+.2f}%")
    elif baseline:
        lines.append(f"- SPY baseline unavailable: {baseline}")
    lines += [
        "", "## Records", "",
        f"- Best day: {day_pnl.idxmax()} {_fmt(float(day_pnl.max()), 'usd')}; "
        f"worst day: {day_pnl.idxmin()} {_fmt(float(day_pnl.min()), 'usd')}",
        f"- Best trade: {best_t['date']} {best_t['side']} {best_t['symbol']} {_fmt(float(best_t['pnl_usd']), 'usd')}; "
        f"worst trade: {worst_t['date']} {worst_t['side']} {worst_t['symbol']} {_fmt(float(worst_t['pnl_usd']), 'usd')}",
        f"- Longest winning streak: {win_days} days; longest losing streak: {lose_days} days, "
        f"{e['longest_losing_streak']} trades",
        "", "## By month", "", period_table(trades, summary, "M"),
        "", "## By year", "", period_table(trades, summary, "Y"),
        "", "## By strategy version", "", version_table(trades, "strategy_key", "strategy"),
        "", "## By config version", "", version_table(trades, "config_version", "config"),
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default=None, help="default: <data-dir>/lifetime.md")
    ap.add_argument("--no-baseline", action="store_true")
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    trades = filter_trades(read_trades_frame(data_dir / "trades.csv"))
    summary = read_summary(data_dir / "daily_summary.csv")
    baseline = None
    if not trades.empty and not args.no_baseline:
        baseline = spy_baseline(trades["date"].min(), trades["date"].max())
    text = build_lifetime(trades, summary, baseline)
    out = Path(args.out) if args.out else data_dir / "lifetime.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
