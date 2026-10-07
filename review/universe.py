"""Screen all US stocks and ETFs into the tradable universe the reviewer picks the watchlist from.

python review/universe.py [--out data/universe.json]

Criteria live under "universe" in config_bounds.json. Uses free historical full-market (SIP)
daily bars, so it must run at least 15 minutes after the last bar it needs (run it after the close).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import BOUNDS_PATH, load_json  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "universe.json"
SYMBOL = re.compile(r"^[A-Z]{1,5}$")


def eligible_assets(assets, criteria):
    """Assets on allowed exchanges, plain symbols, not excluded by symbol or name pattern."""
    patterns = [p.lower() for p in criteria["exclude_name_patterns"]]
    excluded = set(criteria["exclude_symbols"])
    return [a for a in assets
            if a["exchange"] in criteria["exchanges"] and SYMBOL.match(a["symbol"])
            and a["symbol"] not in excluded
            and not any(p in a["name"].lower() for p in patterns)]


def symbol_stats(frame, lookback):
    """Liquidity and movement stats over the last `lookback` daily bars."""
    f = frame.tail(lookback + 1)
    prev_close = f["close"].shift(1)
    f = f.iloc[1:]
    prev_close = prev_close.iloc[1:]
    return {
        "days": len(f),
        "price": round(float(f["close"].iloc[-1]), 2),
        "avg_dollar_volume": round(float((f["close"] * f["volume"]).mean())),
        "avg_range_pct": round(float(((f["high"] - f["low"]) / f["close"] * 100).mean()), 3),
        "avg_abs_gap_pct": round(float(((f["open"] - prev_close).abs() / prev_close * 100).mean()), 3),
    }


def build_universe(assets, frames, criteria, as_of):
    """The ranked universe as a JSON-ready dict."""
    lookback = criteria["lookback_days"]
    rows = []
    for a in eligible_assets(assets, criteria):
        frame = frames.get(a["symbol"])
        if frame is None or len(frame) < lookback * 0.8 + 1:
            continue
        stats = symbol_stats(frame, lookback)
        if stats["price"] < criteria["min_price"] or stats["avg_dollar_volume"] < criteria["min_avg_dollar_volume"]:
            continue
        rows.append({"symbol": a["symbol"], "name": a["name"], "exchange": a["exchange"],
                     "shortable": a["shortable"], "easy_to_borrow": a["easy_to_borrow"], **stats})
    rows.sort(key=lambda r: r["avg_dollar_volume"], reverse=True)
    rows = rows[:criteria["max_symbols"]]
    return {"generated": as_of.isoformat(), "criteria": criteria,
            "symbols": {r.pop("symbol"): r for r in rows}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    from broker import ET, AlpacaBroker

    criteria = load_json(BOUNDS_PATH)["universe"]
    broker = AlpacaBroker(feed="sip")
    assets = eligible_assets(broker.list_equities(), criteria)
    end = datetime.now(ET) - timedelta(minutes=20)
    start = end - timedelta(days=criteria["lookback_days"] * 2 + 10)
    print(f"screening {len(assets)} symbols on {criteria['lookback_days']}-day daily bars")
    frames = broker.daily_bars_many([a["symbol"] for a in assets], start, end)
    universe = build_universe(assets, frames, criteria, end.date())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(universe, indent=1) + "\n", encoding="utf-8")
    print(f"{len(universe['symbols'])} symbols written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
