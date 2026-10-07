from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from analyze import (breakdown, build_report, equity_stats, filter_trades, main, r_histogram,
                     summary_stats, time_bucket)
from logger import TradeLog, read_trades_frame, trade_row

ET = ZoneInfo("America/New_York")

# (day, symbol, side, qty, entry, stop, exit, reason, minute after open, config_version)
TRADES = [
    (14, "SPY", "long", 100, 100.0, 99.0, 102.0, "tp", 16, 1),     # +200, +2R
    (14, "QQQ", "short", 50, 200.0, 202.0, 202.0, "sl", 20, 1),    # -100, -1R
    (15, "AAPL", "long", 100, 150.0, 149.0, 149.0, "sl", 62, 2),   # -100, -1R
    (15, "SPY", "long", 100, 101.0, 100.0, 101.5, "time", 70, 2),  # +50, +0.5R
]


def write_trades(data_dir):
    log = TradeLog(data_dir, "live")
    for i, (d, sym, side, qty, entry, stop, exit_, reason, minute, ver) in enumerate(TRADES):
        open_ = datetime(2026, 9, d, 9, 30, tzinfo=ET)
        log.trade(trade_row(
            day=open_.date(), symbol=sym, side=side, qty=qty,
            entry_time=open_ + timedelta(minutes=minute), entry_price=entry,
            exit_time=open_ + timedelta(minutes=minute + 30 + i), exit_price=exit_,
            exit_reason=reason, stop=stop, range_pct=0.5, session_open=open_,
            config_version=ver, strategy="opening_range_breakout", strategy_version=1))
    return log


@pytest.fixture
def trades(tmp_path):
    write_trades(tmp_path)
    return filter_trades(read_trades_frame(tmp_path / "trades.csv"))


def test_summary_stats_known_values(trades):
    s = summary_stats(trades)
    assert (s["n"], s["wins"], s["losses"]) == (4, 2, 2)
    assert s["win_rate"] == 0.5
    assert s["avg_win_usd"] == pytest.approx(125)
    assert s["avg_loss_usd"] == pytest.approx(-100)
    assert s["avg_win_r"] == pytest.approx(1.25)
    assert s["avg_loss_r"] == pytest.approx(-1.0)
    assert s["expectancy_r"] == pytest.approx(0.125)
    assert s["expectancy_usd"] == pytest.approx(12.5)
    assert s["profit_factor"] == pytest.approx(1.25)
    assert s["total_pnl"] == pytest.approx(50)
    assert s["trading_days"] == 2


def test_equity_stats_known_values(trades):
    e = equity_stats(trades, 10_000)
    assert e["total_return_pct"] == pytest.approx(0.5)
    assert e["max_drawdown_usd"] == pytest.approx(200)
    assert e["max_drawdown_pct"] == pytest.approx(200 / 10_200 * 100)
    assert e["longest_losing_streak"] == 2


def test_profit_factor_without_losses(trades):
    assert summary_stats(trades[trades["pnl_usd"] > 0])["profit_factor"] == float("inf")


def test_time_bucket():
    assert time_bucket(0) == "09:30-09:45"
    assert time_bucket(16) == "09:45-10:00"
    assert time_bucket(62) == "10:30-10:45"
    assert time_bucket(float("nan")) == "unknown"


def test_breakdowns(trades):
    by_symbol = breakdown(trades, "symbol")
    assert by_symbol.loc["SPY", "n"] == 2
    assert by_symbol.loc["SPY", "pnl_usd"] == pytest.approx(250)
    assert by_symbol.loc["SPY", "expectancy_r"] == pytest.approx(1.25)
    by_version = breakdown(trades, "config_version")
    assert by_version.loc[2, "total_r"] == pytest.approx(-0.5)
    assert breakdown(trades, "side").loc["short", "win_rate"] == 0


def test_filters(trades):
    assert len(filter_trades(trades, config_version=2)) == 2
    assert len(filter_trades(trades, since=date(2026, 9, 15))) == 2
    assert len(filter_trades(trades, strategy="other")) == 0


def test_r_histogram():
    lines = r_histogram([2, -1, -1, 0.5]).splitlines()
    assert len(lines) == 7
    assert lines[0].startswith(" -1.0R to  -0.5R") and lines[0].endswith(" 2")
    assert lines[3].endswith(" 1") and lines[6].endswith(" 1")
    assert r_histogram([]) == "(no R values)"


def test_report_contents(trades):
    report = build_report(trades, 10_000, baseline=1.23)
    assert "Sample size warning" in report
    assert "Expectancy: +0.12R / $12.50" in report or "Expectancy: +0.13R / $12.50" in report
    assert "SPY 2026-09-14 open to 2026-09-15 close: +1.23%" in report
    for heading in ("By strategy", "By ticker", "By side", "By entry time", "By exit reason",
                    "By config version", "R-multiple distribution"):
        assert heading in report
    assert "opening_range_breakout v1" in report


def test_no_warning_with_enough_trades(trades):
    import pandas as pd
    many = pd.concat([trades] * 8, ignore_index=True)
    assert "Sample size warning" not in build_report(many, 10_000)


def test_empty_report():
    import pandas as pd
    from logger import TRADE_FIELDS
    report = build_report(pd.DataFrame(columns=TRADE_FIELDS), 10_000)
    assert "No closed trades" in report and "Sample size warning" in report


def test_cli_writes_report(tmp_path, capsys):
    write_trades(tmp_path)
    out = tmp_path / "reports"
    assert main(["--data-dir", str(tmp_path), "--out", str(out), "--no-baseline", "--equity", "10000"]) == 0
    assert "Total return: +0.50%" in capsys.readouterr().out
    assert len(list(out.glob("report_*.md"))) == 1


def r_trades(by_day, symbol="SPY", minute=20):
    """Synthetic trades: {day_offset: [R, ...]}, each with 1R = $100."""
    rows = []
    for d, rs in by_day.items():
        for r in rs:
            rows.append({"date": date(2026, 9, 1) + timedelta(days=d), "symbol": symbol, "side": "long",
                         "pnl_r": r, "pnl_usd": r * 100, "entry_minute_after_open": minute, "range_pct": 0.5})
    import pandas as pd
    return pd.DataFrame(rows)


def test_expectancy_ci_clusters_by_day():
    from analyze import expectancy_ci
    ci = expectancy_ci(r_trades({0: [1, -1], 1: [2, 0], 2: [-1, -1]}))
    assert (ci["n"], ci["days"], ci["mean"]) == (6, 3, 0)
    assert ci["hi"] == pytest.approx(4.30 * (1 / 3) ** 0.5, rel=1e-3)
    assert ci["lo"] == pytest.approx(-ci["hi"])


def test_expectancy_ci_one_day_has_no_range():
    import math
    from analyze import expectancy_ci
    assert math.isnan(expectancy_ci(r_trades({0: [1, -1, 2]}))["lo"])


def test_parse_and_apply_query():
    import pandas as pd
    from analyze import apply_query, parse_query
    df = pd.concat([r_trades({0: [1]}, "TSLA", 70), r_trades({1: [1]}, "AMD", 10), r_trades({2: [1]}, "SPY", 70)])
    assert list(apply_query(df, parse_query("entry_minute_after_open >= 60"))["symbol"]) == ["TSLA", "SPY"]
    assert list(apply_query(df, parse_query("symbol in [TSLA, AMD] and entry_minute_after_open < 60"))["symbol"]) == ["AMD"]
    assert len(apply_query(df, parse_query("weekday == 1"))) == 1  # 2026-09-01 is a Tuesday
    for bad in ("pnl_r > 0", "symbol ~ TSLA", "exit_reason == tp"):
        with pytest.raises(ValueError):
            parse_query(bad)


def test_evidence_verdicts():
    from analyze import evidence
    losing = r_trades({d: [-1, -0.8] for d in range(12)})
    result = evidence(losing, "symbol == SPY")
    assert result["passed"] and result["all"]["hi"] < 0

    assert not evidence(r_trades({d: [-1, -0.8] for d in range(8)}), "symbol == SPY")["passed"]
    noisy = r_trades({d: [2, 1, 0] if d % 2 else [-1, -1, -1] for d in range(12)})
    assert any("includes zero" in r for r in evidence(noisy, "symbol == SPY")["reasons"])

    registered = date(2026, 9, 1) + timedelta(days=9)
    few_after = evidence(losing, "symbol == SPY", registered)
    assert not few_after["passed"] and any("since" in r for r in few_after["reasons"])
    flipped = pd_concat(losing, r_trades({d: [3, 3] for d in range(12, 18)}))
    result = evidence(flipped, "symbol == SPY", registered)
    assert not result["passed"]


def pd_concat(*frames):
    import pandas as pd
    return pd.concat(frames, ignore_index=True)


def test_report_shows_range(trades):
    assert "Expectancy 95% range: [" in build_report(trades, 10_000)
    assert "range_95" in build_report(trades, 10_000)
