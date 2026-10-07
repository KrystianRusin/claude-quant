from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from analyze import filter_trades
from lifetime import build_lifetime, read_summary, streaks
from logger import TradeLog, read_trades_frame, trade_row

ET = ZoneInfo("America/New_York")

# (date, symbol, pnl in R with 1R = $100 on 100 shares)
TRADES = [
    (date(2026, 9, 29), "SPY", 2.0),
    (date(2026, 9, 29), "QQQ", -1.0),
    (date(2026, 9, 30), "AMD", -1.0),
    (date(2026, 10, 1), "MU", -1.0),
    (date(2026, 10, 2), "SPY", 3.0),
]
SUMMARY = [  # date, equity_start, equity_end, halted
    (date(2026, 9, 29), 100_000, 100_100, False),
    (date(2026, 9, 30), 100_100, 100_000, False),
    (date(2026, 10, 1), 100_000, 99_900, True),
    (date(2026, 10, 2), 99_900, 100_200, False),
]


def write(tmp_path):
    log = TradeLog(tmp_path, "live")
    for i, (d, sym, r) in enumerate(TRADES):
        open_ = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
        log.trade(trade_row(day=d, symbol=sym, side="long", qty=100, entry_time=open_ + timedelta(minutes=20 + i),
                            entry_price=100.0, exit_time=open_ + timedelta(hours=2, minutes=i),
                            exit_price=100.0 + r, exit_reason="tp" if r > 0 else "sl", stop=99.0,
                            range_pct=0.5, session_open=open_, config_version=1 if d.month == 9 else 2,
                            strategy="opening_range_breakout", strategy_version=1))
    for d, start, end, halted in SUMMARY:
        log.daily_summary({"date": d.isoformat(), "trades": 1, "gross_pnl": end - start,
                           "equity_start": start, "equity_end": end, "halted": halted, "notes": "live"})
    return filter_trades(read_trades_frame(tmp_path / "trades.csv")), read_summary(tmp_path / "daily_summary.csv")


def test_streaks():
    assert streaks([1, 2, -1, -1, -1, 0, 3]) == (2, 3)
    assert streaks([]) == (0, 0)


def test_lifetime_report(tmp_path):
    trades, summary = write(tmp_path)
    text = build_lifetime(trades, summary, baseline=1.5, today=date(2026, 10, 2))
    assert "Running since 2026-09-29 (4 sessions, 4 with trades, 1 halted)" in text
    assert "Trades: 5 (2 wins, 3 losses, win rate 40.0%)" in text
    assert "Net P&L: $200.00" in text
    assert "Start $100,000.00, now $100,200.00 (+0.20%)" in text
    assert "High-water mark $100,200.00; current drawdown $0.00" in text
    assert "SPY buy-and-hold over the same period: +1.50%" in text
    assert "Best day: 2026-10-02 $300.00; worst day: 2026-09-30 $-100.00" in text
    assert "longest losing streak: 2 days" in text
    assert "| 2026-09 | 2 | 3 | 33.3% | $0.00 | +0.00% | +0.00R |" in text
    assert "| 2026-10 | 2 | 2 | 50.0% | $200.00 | +0.20% | +1.00R |" in text
    assert "| 2026 | 4 | 5 |" in text
    assert "2026-09-29 to 2026-09-30" in text and "2026-10-01 to 2026-10-02" in text


def test_lifetime_without_trades():
    empty = pd.DataFrame(columns=["date"])
    assert "No closed trades yet." in build_lifetime(empty, read_summary("missing.csv"))
