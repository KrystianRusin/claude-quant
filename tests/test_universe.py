import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
from universe import build_universe, eligible_assets, symbol_stats  # noqa: E402

CRITERIA = {"lookback_days": 5, "min_price": 10, "min_avg_dollar_volume": 1_000_000, "max_symbols": 2,
            "exchanges": ["NYSE", "NASDAQ", "ARCA"], "exclude_name_patterns": ["3x", "leveraged"],
            "exclude_symbols": ["TQQQ"]}


def asset(symbol, name="Corp", exchange="NYSE"):
    return {"symbol": symbol, "name": name, "exchange": exchange, "shortable": True, "easy_to_borrow": True}


def frame(price, volume, days=6, rng=2.0, gap=1.0):
    idx = pd.date_range("2026-09-01", periods=days, freq="D", tz="America/New_York")
    rows = []
    for i in range(days):
        prev = price
        o = prev + gap
        rows.append({"open": o, "high": price + rng / 2, "low": price - rng / 2, "close": price, "volume": volume})
    return pd.DataFrame(rows, index=idx)


def test_eligible_assets_filters():
    assets = [asset("AAA"), asset("TQQQ"), asset("BBB", "ProShares UltraPro 3x QQQ"), asset("CCC", exchange="OTC"),
              asset("BRK.B"), asset("DDD", "Direxion Daily Leveraged")]
    assert [a["symbol"] for a in eligible_assets(assets, CRITERIA)] == ["AAA"]


def test_symbol_stats_known_values():
    s = symbol_stats(frame(100, 10_000, rng=2.0, gap=1.0), 5)
    assert s == {"days": 5, "price": 100.0, "avg_dollar_volume": 1_000_000,
                 "avg_range_pct": pytest.approx(2.0), "avg_abs_gap_pct": pytest.approx(1.0)}


def test_build_universe_filters_and_ranks():
    assets = [asset("BIG"), asset("MID"), asset("SML"), asset("CHEAP"), asset("THIN"), asset("NEW")]
    frames = {"BIG": frame(100, 50_000), "MID": frame(100, 30_000), "SML": frame(100, 20_000),
              "CHEAP": frame(5, 10_000_000), "THIN": frame(100, 100), "NEW": frame(100, 50_000, days=3)}
    u = build_universe(assets, frames, CRITERIA, date(2026, 10, 7))
    assert u["generated"] == "2026-10-07"
    assert list(u["symbols"]) == ["BIG", "MID"]
    assert u["symbols"]["BIG"]["avg_dollar_volume"] == 5_000_000
