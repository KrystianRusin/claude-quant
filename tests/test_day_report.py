import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
from day_report import build_day_report  # noqa: E402

from logger import TradeLog, trade_row  # noqa: E402

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 9, 15, 9, 30, tzinfo=ET)


def test_day_report(tmp_path):
    log = TradeLog(tmp_path, "live")
    log.trade(trade_row(day=OPEN.date(), symbol="AMZN", side="long", qty=78, entry_time=OPEN + timedelta(minutes=29),
                        entry_price=255.55, exit_time=OPEN + timedelta(hours=5), exit_price=259.80,
                        exit_reason="tp", stop=253.21, range_pct=0.86, session_open=OPEN,
                        config_version=1, strategy="opening_range_breakout", strategy_version=1))
    log.order_event(OPEN + timedelta(minutes=20), "skip", "TSLA", side="short", reason="not shortable")
    log.daily_summary({"date": "2026-09-15", "trades": 1, "equity_start": 100000, "equity_end": 100331.5,
                       "halted": False, "notes": "live"})
    (tmp_path / "trader.log").write_text(
        "2026-09-15 10:00:00,000 INFO entered long\n"
        "2026-09-15 10:05:00,000 WARNING exit order for X ended canceled; will retry\n"
        "2026-09-14 10:05:00,000 ERROR yesterday\n", encoding="utf-8")

    report = build_day_report(tmp_path, OPEN.date(), market="SPY +0.16%")
    assert "1 trades, 1 wins, 0 losses, net $331.50" in report
    assert "| AMZN | long | 78 | 09:59 @ 255.55 | 14:30 @ 259.8 | tp |" in report
    assert "TSLA short: skip (not shortable)" in report
    assert "will retry" in report and "yesterday" not in report
    assert "SPY +0.16%" in report and "100000 -> 100331.5" in report


def test_day_report_empty_day(tmp_path):
    report = build_day_report(tmp_path, OPEN.date())
    assert "No closed trades." in report and "No daily summary row" in report
