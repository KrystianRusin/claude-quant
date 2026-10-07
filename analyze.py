"""Performance report from data/trades.csv.

Usage: python analyze.py [--since YYYY-MM-DD] [--config-version N] [--strategy NAME]
                         [--data-dir data] [--out data/reports/] [--equity 100000] [--no-baseline]
       python analyze.py --config-version N --evidence "QUERY" [--registered YYYY-MM-DD]

An evidence QUERY selects trades by conditions joined with "and", for example
  "entry_minute_after_open >= 60"   "symbol in [TSLA, AMD] and side == short"   "weekday == 0"
Columns: symbol, side, entry_minute_after_open, range_pct, weekday (0 = Monday).
"""
import argparse
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from logger import read_trades_frame

MIN_SAMPLE = 30
DEFAULT_EQUITY = 100_000.0
EVIDENCE_MIN_TRADES = 20
EVIDENCE_MIN_DAYS = 10
POST_REGISTRATION_MIN_TRADES = 10
T_95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
        12: 2.18, 15: 2.13, 20: 2.09, 30: 2.04, 60: 2.00, 120: 1.98}
QUERY_COLUMNS = {"symbol": str, "side": str, "entry_minute_after_open": float, "range_pct": float, "weekday": float}
CONDITION = re.compile(r"^\s*(\w+)\s*(==|!=|>=|<=|>|<|in)\s*(.+?)\s*$")


def filter_trades(df, since=None, config_version=None, strategy=None):
    if since is not None:
        df = df[df["date"] >= since]
    if config_version is not None:
        df = df[df["config_version"] == config_version]
    if strategy is not None:
        df = df[df["strategy"] == strategy]
    return df.sort_values(["exit_time", "symbol"], na_position="first").reset_index(drop=True)


def summary_stats(df):
    pnl = df["pnl_usd"].astype(float)
    r = pd.to_numeric(df["pnl_r"], errors="coerce")
    wins, losses = df[pnl > 0], df[pnl < 0]
    gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    n = len(df)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n if n else float("nan"),
        "avg_win_usd": pnl[pnl > 0].mean() if len(wins) else float("nan"),
        "avg_loss_usd": pnl[pnl < 0].mean() if len(losses) else float("nan"),
        "avg_win_r": r[pnl > 0].mean() if len(wins) else float("nan"),
        "avg_loss_r": r[pnl < 0].mean() if len(losses) else float("nan"),
        "expectancy_r": r.mean() if r.notna().any() else float("nan"),
        "expectancy_usd": pnl.mean() if n else float("nan"),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else float("nan")),
        "total_pnl": pnl.sum(),
        "trading_days": df["date"].nunique(),
    }


def equity_stats(df, start_equity):
    pnl = df["pnl_usd"].astype(float).to_numpy()
    equity = start_equity + np.concatenate([[0.0], np.cumsum(pnl)])
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    streak = longest = 0
    for p in pnl:
        streak = streak + 1 if p < 0 else 0
        longest = max(longest, streak)
    return {
        "start_equity": start_equity,
        "end_equity": equity[-1],
        "total_return_pct": (equity[-1] - start_equity) / start_equity * 100,
        "max_drawdown_usd": -drawdown.min(),
        "max_drawdown_pct": -(drawdown / peak).min() * 100,
        "longest_losing_streak": longest,
    }


def t_95(dof):
    """Two-sided 95% Student t critical value, rounded toward the more conservative table entry."""
    return T_95[max(k for k in T_95 if k <= dof)] if dof < 120 else 1.96


def expectancy_ci(df):
    """Mean R per trade with a 95% range that treats each trading day as one cluster.

    Trades on the same day share market conditions, so the effective sample size is closer to
    the number of days than the number of trades.
    """
    r = pd.to_numeric(df["pnl_r"], errors="coerce")
    keep = r.notna()
    r, days_col = r[keep], df["date"][keep]
    n, days = len(r), days_col.nunique()
    if n == 0:
        return {"n": 0, "days": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan")}
    mean = float(r.mean())
    if days < 2:
        return {"n": n, "days": days, "mean": mean, "lo": float("nan"), "hi": float("nan")}
    resid = (r - mean).groupby(days_col).sum()
    half = t_95(days - 1) * math.sqrt(days / (days - 1) * float((resid ** 2).sum()) / n ** 2)
    return {"n": n, "days": days, "mean": mean, "lo": mean - half, "hi": mean + half}


def fmt_range(ci):
    if math.isnan(ci["lo"]):
        return "n/a (needs 2+ days)"
    return f"[{ci['lo']:+.2f}R, {ci['hi']:+.2f}R]"


def range_note(ci):
    if math.isnan(ci["lo"]):
        return ""
    if excludes_zero(ci):
        return " (trades grouped by day; clearly away from zero)"
    return " (trades grouped by day; includes zero, so the sign is not established)"


def excludes_zero(ci):
    return not math.isnan(ci["lo"]) and (ci["lo"] > 0 or ci["hi"] < 0)


def parse_query(text):
    """Parse 'col op value [and col op value ...]' into (column, op, value) tuples."""
    conditions = []
    for part in re.split(r"\s+and\s+", text.strip()):
        m = CONDITION.match(part)
        if not m or m.group(1) not in QUERY_COLUMNS:
            raise ValueError(f"bad condition {part!r}; columns: {sorted(QUERY_COLUMNS)}")
        col, op, raw = m.groups()
        cast = QUERY_COLUMNS[col]
        if op == "in":
            if not (raw.startswith("[") and raw.endswith("]")):
                raise ValueError(f"'in' needs a [list]: {part!r}")
            value = [cast(v.strip().strip("'\"")) for v in raw[1:-1].split(",") if v.strip()]
        else:
            value = cast(raw.strip("'\""))
        conditions.append((col, op, value))
    return conditions


def apply_query(df, conditions):
    df = df.assign(weekday=pd.to_datetime(df["date"]).dt.weekday)
    mask = pd.Series(True, index=df.index)
    for col, op, value in conditions:
        series = pd.to_numeric(df[col], errors="coerce") if QUERY_COLUMNS[col] is float else df[col].astype(str)
        if op == "in":
            mask &= series.isin(value)
        else:
            mask &= {"==": series.eq, "!=": series.ne, ">=": series.ge, "<=": series.le,
                     ">": series.gt, "<": series.lt}[op](value)
    return df[mask]


def evidence(df, query, registered=None):
    """Does the subset of trades matching `query` have an expectancy clearly away from zero?

    Passes when the 95% range over 20+ trades and 10+ days excludes zero and, if `registered`
    is given, the trades after that date (when the hypothesis was written down) point the same way.
    """
    subset = apply_query(df, parse_query(query))
    ci = expectancy_ci(subset)
    reasons = []
    if ci["n"] < EVIDENCE_MIN_TRADES or ci["days"] < EVIDENCE_MIN_DAYS:
        reasons.append(f"{ci['n']} trades over {ci['days']} days; needs {EVIDENCE_MIN_TRADES}+ over "
                       f"{EVIDENCE_MIN_DAYS}+")
    if math.isnan(ci["lo"]):
        reasons.append("no 95% range yet (needs 2+ days)")
    elif not excludes_zero(ci):
        reasons.append(f"95% range {fmt_range(ci)} includes zero")
    post = None
    if registered is not None:
        post = expectancy_ci(subset[subset["date"] > registered])
        if post["n"] < POST_REGISTRATION_MIN_TRADES:
            reasons.append(f"{post['n']} trades since {registered}; needs {POST_REGISTRATION_MIN_TRADES}+")
        elif math.isnan(ci["mean"]) or (post["mean"] > 0) != (ci["mean"] > 0):
            reasons.append(f"trades since {registered} average {post['mean']:+.2f}R, the other direction")
    return {"query": query, "all": ci, "since_registered": post, "registered": registered,
            "passed": not reasons, "reasons": reasons}


def format_evidence(result):
    ci, post = result["all"], result["since_registered"]
    lines = [f"Evidence for: {result['query']}",
             f"- All matching trades: {ci['n']} over {ci['days']} days, expectancy {_fmt(ci['mean'], 'r')}, "
             f"95% range {fmt_range(ci)}"]
    if post is not None:
        lines.append(f"- Since {result['registered']}: {post['n']} trades, expectancy {_fmt(post['mean'], 'r')}")
    lines.append("- Verdict: PASS" if result["passed"] else "- Verdict: FAIL (" + "; ".join(result["reasons"]) + ")")
    return "\n".join(lines)


def time_bucket(minute_after_open, width=15):
    if minute_after_open is None or (isinstance(minute_after_open, float) and math.isnan(minute_after_open)):
        return "unknown"
    start = int(minute_after_open) // width * width
    t0 = datetime(2000, 1, 1, 9, 30) + timedelta(minutes=start)
    return f"{t0:%H:%M}-{t0 + timedelta(minutes=width):%H:%M}"


def breakdown(df, key):
    """Per-group trade count, days, win rate, total $, expectancy in R and its 95% range."""
    columns = ["n", "days", "win_rate", "pnl_usd", "expectancy_r", "range_95", "total_r"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    groups = df.assign(_r=pd.to_numeric(df["pnl_r"], errors="coerce"),
                       _pnl=df["pnl_usd"].astype(float)).groupby(key, sort=True)
    return pd.DataFrame({
        "n": groups.size(),
        "days": groups["date"].nunique(),
        "win_rate": groups["_pnl"].apply(lambda s: (s > 0).mean()),
        "pnl_usd": groups["_pnl"].sum(),
        "expectancy_r": groups["_r"].mean(),
        "range_95": groups.apply(lambda g: fmt_range(expectancy_ci(g)), include_groups=False),
        "total_r": groups["_r"].sum(),
    })[columns]


def add_keys(df):
    df = df.copy()
    df["time_bucket"] = df["entry_minute_after_open"].map(time_bucket)
    df["strategy_key"] = df["strategy"].astype(str) + " v" + df["strategy_version"].astype(str)
    return df


def r_histogram(r_values, width=0.5, bar_width=40):
    """Text histogram of R multiples in `width`-R bins; each bin is [lo, lo + width)."""
    r = pd.to_numeric(pd.Series(r_values), errors="coerce").dropna()
    if r.empty:
        return "(no R values)"
    lo = math.floor(r.min() / width) * width
    idx = np.floor((r - lo) / width + 1e-9).astype(int)
    counts = np.bincount(idx)
    scale = max(1.0, counts.max() / bar_width)
    return "\n".join(f"{lo + i * width:+5.1f}R to {lo + (i + 1) * width:+5.1f}R | "
                     f"{'#' * math.ceil(c / scale)} {c}" for i, c in enumerate(counts))


def _fmt(v, kind="num"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if kind == "usd":
        return f"${v:,.2f}"
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "r":
        return f"{v:+.2f}R"
    return f"{v:,.2f}" if isinstance(v, float) else str(v)


def _table(frame):
    if frame.empty:
        return "(none)"
    out = frame.copy()
    out["win_rate"] = out["win_rate"].map(lambda v: _fmt(v, "pct"))
    out["pnl_usd"] = out["pnl_usd"].map(lambda v: _fmt(v, "usd"))
    out["expectancy_r"] = out["expectancy_r"].map(lambda v: _fmt(v, "r"))
    out["total_r"] = out["total_r"].map(lambda v: _fmt(v, "r"))
    header = "| " + " | ".join([out.index.name or "group", *out.columns]) + " |"
    sep = "|" + "---|" * (len(out.columns) + 1)
    rows = ["| " + " | ".join([str(i), *map(str, row)]) + " |" for i, row in out.iterrows()]
    return "\n".join([header, sep, *rows])


def build_report(df, start_equity, baseline=None, title="Performance report", filters=""):
    """Markdown report. `baseline` is SPY buy-and-hold return in percent, or an explanation string."""
    lines = [f"# {title}", ""]
    if filters:
        lines += [f"Filters: {filters}", ""]
    n = len(df)
    if n < MIN_SAMPLE:
        lines += [f"**Sample size warning:** only {n} trades (< {MIN_SAMPLE}). "
                  "Conclusions from this report are not statistically meaningful.", ""]
    if n == 0:
        lines.append("No closed trades in this selection.")
        return "\n".join(lines) + "\n"

    df = add_keys(df)
    s = summary_stats(df)
    e = equity_stats(df, start_equity)
    ci = expectancy_ci(df)
    first, last = df["date"].min(), df["date"].max()
    lines += [
        f"Period: {first} to {last} ({s['trading_days']} trading days with trades)", "",
        "## Summary", "",
        f"- Trades: {s['n']} ({s['wins']} wins, {s['losses']} losses)",
        f"- Win rate: {_fmt(s['win_rate'], 'pct')}",
        f"- Average win: {_fmt(s['avg_win_usd'], 'usd')} ({_fmt(s['avg_win_r'], 'r')})",
        f"- Average loss: {_fmt(s['avg_loss_usd'], 'usd')} ({_fmt(s['avg_loss_r'], 'r')})",
        f"- Expectancy: {_fmt(s['expectancy_r'], 'r')} / {_fmt(s['expectancy_usd'], 'usd')} per trade",
        f"- Expectancy 95% range: {fmt_range(ci)}{range_note(ci)}",
        f"- Profit factor: {_fmt(s['profit_factor'])}",
        f"- Net P&L: {_fmt(s['total_pnl'], 'usd')}",
        "", "## Equity curve", "",
        f"- Start equity: {_fmt(e['start_equity'], 'usd')}",
        f"- Total return: {e['total_return_pct']:+.2f}%",
        f"- Max drawdown: {_fmt(e['max_drawdown_usd'], 'usd')} ({e['max_drawdown_pct']:.2f}%)",
        f"- Longest losing streak: {e['longest_losing_streak']} trades",
        "", "## SPY buy-and-hold baseline", "",
    ]
    if isinstance(baseline, (int, float)):
        lines.append(f"- SPY {first} open to {last} close: {baseline:+.2f}% vs strategy {e['total_return_pct']:+.2f}%")
    else:
        lines.append(f"- Unavailable: {baseline or 'not requested'}")
    for title_, key in [("By strategy", "strategy_key"), ("By config version", "config_version"),
                        ("By ticker", "symbol"), ("By side", "side"),
                        ("By entry time (15-minute buckets)", "time_bucket"),
                        ("By exit reason", "exit_reason")]:
        lines += ["", f"## {title_}", "", _table(breakdown(df, key))]
    lines += ["", "## R-multiple distribution", "", "```", r_histogram(df["pnl_r"]), "```"]
    return "\n".join(lines) + "\n"


def spy_baseline(first, last):
    """SPY buy-and-hold return in percent from `first` open to `last` close, or an error string."""
    try:
        from broker import make_broker

        bars = make_broker().daily_bars("SPY", datetime.combine(first, datetime.min.time()),
                                        datetime.combine(last + timedelta(days=1), datetime.min.time()))
        if bars is None or bars.empty:
            return "no SPY bars returned"
        return (float(bars["close"].iloc[-1]) / float(bars["open"].iloc[0]) - 1) * 100
    except Exception as exc:  # report still useful without the baseline
        return f"{type(exc).__name__}: {exc}"


def start_equity_for(data_dir, first_day, default):
    path = Path(data_dir) / "daily_summary.csv"
    if path.exists() and path.stat().st_size:
        s = pd.read_csv(path)
        s = s[pd.to_datetime(s["date"]).dt.date >= first_day]
        if not s.empty and pd.notna(s["equity_start"].iloc[0]):
            return float(s["equity_start"].iloc[0])
    return default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=date.fromisoformat)
    ap.add_argument("--config-version", type=int)
    ap.add_argument("--strategy")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="data/reports/")
    ap.add_argument("--equity", type=float, default=None, help="starting equity if daily_summary.csv lacks it")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--evidence", metavar="QUERY", help="test one hypothesis instead of writing a report")
    ap.add_argument("--registered", type=date.fromisoformat,
                    help="with --evidence: date the hypothesis was written down")
    args = ap.parse_args(argv)

    df = filter_trades(read_trades_frame(Path(args.data_dir) / "trades.csv"),
                       args.since, args.config_version, args.strategy)
    if args.evidence:
        try:
            print(format_evidence(evidence(df, args.evidence, args.registered)))
        except ValueError as e:
            print(f"error: {e}")
            return 2
        return 0
    filters = ", ".join(f"{k}={v}" for k, v in [("since", args.since), ("config_version", args.config_version),
                                                 ("strategy", args.strategy), ("data_dir", args.data_dir)] if v)
    baseline = "skipped (--no-baseline)"
    start_equity = args.equity or DEFAULT_EQUITY
    if not df.empty:
        start_equity = args.equity or start_equity_for(args.data_dir, df["date"].min(), DEFAULT_EQUITY)
        if not args.no_baseline:
            baseline = spy_baseline(df["date"].min(), df["date"].max())
    report = build_report(df, start_equity, baseline, title=f"Performance report {date.today()}", filters=filters)
    print(report)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"report_{date.today().isoformat()}.md"
        path.write_text(report, encoding="utf-8")
        print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
