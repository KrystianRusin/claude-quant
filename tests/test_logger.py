from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from logger import TradeLog, trade_row

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 9, 15, 9, 30, tzinfo=ET)


def row(**overrides):
    args = dict(day=OPEN.date(), symbol="SPY", side="short", qty=10, entry_time=OPEN + timedelta(minutes=20),
                entry_price=100.0, exit_time=OPEN + timedelta(hours=2), exit_price=97.0, exit_reason="tp",
                stop=101.5, range_pct=0.8, session_open=OPEN, config_version=3,
                strategy="opening_range_breakout", strategy_version=1)
    args.update(overrides)
    return trade_row(**args)


def test_trade_row_pnl():
    r = row()
    assert r["pnl_usd"] == 30.0
    assert r["pnl_r"] == 2.0
    assert r["entry_minute_after_open"] == 20
    assert r["config_version"] == 3 and r["strategy_version"] == 1


def test_trade_row_without_stop_or_entry_time():
    r = row(stop=None, entry_time=None, range_pct=float("nan"), exit_reason="carryover")
    assert r["pnl_r"] == "" and r["entry_minute_after_open"] == "" and r["range_pct"] == ""


def test_trade_dedupe(tmp_path):
    log = TradeLog(tmp_path, "live")
    assert log.trade(row())
    assert not log.trade(row())
    assert log.trade(row(entry_time=OPEN + timedelta(minutes=50)))
    assert len(log.trades(OPEN.date())) == 2
    assert log.trades(date(2026, 1, 1)) == []


def test_daily_summary_replaces_same_date(tmp_path):
    log = TradeLog(tmp_path, "live")
    log.daily_summary({"date": "2026-09-15", "trades": 1, "notes": "first"})
    log.daily_summary({"date": "2026-09-14", "trades": 2})
    log.daily_summary({"date": "2026-09-15", "trades": 3, "notes": "second"})
    lines = (tmp_path / "daily_summary.csv").read_text().splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("2026-09-14") and lines[2].startswith("2026-09-15,3")


def test_order_events(tmp_path):
    log = TradeLog(tmp_path, "dry-run")
    log.order_event(OPEN, "submit", "SPY", side="long", qty=5, order_id="abc")
    log.order_event(OPEN + timedelta(days=1), "fill", "SPY", order_id="abc")
    rows = log.order_rows(OPEN.date())
    assert len(rows) == 1 and rows[0]["mode"] == "dry-run" and rows[0]["order_id"] == "abc"
