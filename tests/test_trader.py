import logging
from datetime import timedelta

import pandas as pd
import pytest

import risk
from helpers import CLOSE, OPEN, flat_range, make_bars
from logger import TradeLog
from simbroker import SimBroker, SimClock
from trader import Session, entry_cid

LOGGER = logging.getLogger("test-trader")


class FakeSource:
    def __init__(self, bars, shortable=(True, True)):
        self.bars = bars
        self.flags = shortable

    def get_bars(self, symbols, start, end):
        return {s: f[(f.index >= start) & (f.index < end)] for s, f in self.bars.items() if s in symbols}

    def session(self, day):
        return (OPEN, CLOSE) if day == OPEN.date() else None

    def trading_days(self, end, count):
        return [OPEN.date()]

    def asset_flags(self, symbol):
        return self.flags


def day_bars(after_breakout, breakout=(100.5, 101.2, 100.5, 101.0)):
    """Range 99.5-100.5, breakout bar at 09:45, given bars, then flat to the close."""
    rows = flat_range() + [breakout] + list(after_breakout)
    last = rows[-1][3]
    rows += [(last, last, last, last)] * (390 - len(rows))
    return make_bars(rows)


TP_DAY = [(101, 101.5, 100.8, 101.2), (101.2, 104.5, 101.1, 104.2)]
SL_DAY = [(101, 101.5, 100.8, 101.2), (101, 101.1, 99.0, 99.3)]
DRIFT_DAY = [(101, 101.5, 100.8, 101.2)] + [(100, 100.2, 99.8, 100)] * 5


def setup(tmp_path, cfg, bars, halt_path=None, **source_kw):
    clock = SimClock(OPEN - timedelta(minutes=5))
    broker = SimBroker(FakeSource(bars, **source_kw), clock, 100_000)
    log = TradeLog(tmp_path, "replay")
    make = lambda: Session(cfg, broker, clock, log, LOGGER, OPEN, CLOSE, "replay",  # noqa: E731
                           poll_seconds=30, halt_path=halt_path)
    return broker, clock, log, make


def trades(log):
    return log.trades(OPEN.date())


def test_take_profit_trade(tmp_path, cfg):
    broker, clock, log, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)})
    session = make().run()
    [t] = trades(log)
    assert (t["side"], t["exit_reason"], t["qty"]) == ("long", "tp", "198")
    assert float(t["entry_price"]) == 101 and float(t["exit_price"]) == 104
    assert float(t["pnl_usd"]) == pytest.approx(594)
    assert float(t["pnl_r"]) == pytest.approx(2.0)
    assert t["entry_minute_after_open"] == "16"
    assert (t["config_version"], t["strategy"], t["strategy_version"]) == ("1", "opening_range_breakout", "1")
    assert session.done and not broker.list_positions()
    summary = (tmp_path / "daily_summary.csv").read_text().splitlines()[1]
    assert summary.startswith("2026-09-15,1,1,0,594.0,100000.0,100594.0,False")
    events = [r["event"] for r in log.order_rows(OPEN.date())]
    assert events.count("submit") == 1 and "filled" in events


def test_stop_loss_trade(tmp_path, cfg):
    _, _, log, make = setup(tmp_path, cfg, {"SPY": day_bars(SL_DAY)})
    make().run()
    [t] = trades(log)
    assert t["exit_reason"] == "sl" and float(t["exit_price"]) == 99.5
    assert float(t["pnl_r"]) == pytest.approx(-1.0)


def test_time_exit(tmp_path, cfg):
    _, _, log, make = setup(tmp_path, cfg, {"SPY": day_bars([(101.5, 101.6, 101.4, 101.5)])})
    make().run()
    [t] = trades(log)
    assert t["exit_reason"] == "time"
    assert pd.Timestamp(t["exit_time"]).strftime("%H:%M") == "15:45"


def test_daily_loss_halt_flattens(tmp_path, cfg):
    cfg["risk"]["max_daily_loss_pct"] = 0.15
    broker, _, log, make = setup(tmp_path, cfg, {"SPY": day_bars(DRIFT_DAY)})
    session = make().run()
    [t] = trades(log)
    assert t["exit_reason"] == "halt"
    assert "daily loss" in session.halted
    assert not broker.list_positions()


def test_halt_file_blocks_entries(tmp_path, cfg):
    halt = tmp_path / "HALT"
    halt.write_text("")
    broker, _, log, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)}, halt_path=halt)
    session = make().run()
    assert session.halted == "HALT file present"
    assert broker.orders == [] and trades(log) == []


def test_restart_does_not_double_enter(tmp_path, cfg):
    broker, clock, log, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)})
    first = make()
    first.equity_start = 100_000
    first.recover()
    clock.sleep_until(OPEN + timedelta(minutes=16, seconds=10))
    first.tick()
    assert len(broker.orders) == 1  # entered, then the process "crashes"

    clock.sleep(20)
    second = make()
    second.run()
    assert "SPY" in second.entered
    assert [o.client_order_id for o in broker.orders] == [entry_cid(OPEN.date(), "SPY")]
    assert len(trades(log)) == 1


def test_carryover_position_flattened_at_open(tmp_path, cfg):
    bars = {"SPY": day_bars([(100, 100.2, 99.8, 100)]), "AAPL": make_bars([(181, 181, 181, 181)] * 390)}
    broker, _, log, make = setup(tmp_path, cfg, bars)
    broker.positions["AAPL"] = [50.0, 180.0]
    broker.cash -= 50 * 180
    session = make().run()
    carry = [t for t in trades(log) if t["symbol"] == "AAPL"]
    assert len(carry) == 1 and carry[0]["exit_reason"] == "carryover"
    assert float(carry[0]["pnl_usd"]) == pytest.approx(50)
    assert "AAPL" not in broker.list_positions()
    assert any("carryover" in n for n in session.notes)


def test_max_trades_per_day(tmp_path, cfg):
    cfg["risk"]["max_trades_per_day"] = 1
    bars = {"SPY": day_bars(TP_DAY), "QQQ": day_bars(TP_DAY)}
    broker, _, log, make = setup(tmp_path, cfg, bars)
    make().run()
    assert len(broker.orders) == 1 and len(trades(log)) == 1


def test_order_cap_halts(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(risk, "MAX_ORDERS_PER_DAY", 1)
    bars = {"SPY": day_bars(TP_DAY), "QQQ": day_bars(TP_DAY)}
    broker, _, log, make = setup(tmp_path, cfg, bars)
    session = make().run()
    assert session.halted == "order cap reached"
    assert len([o for o in broker.orders if o.legs]) == 1
    assert not broker.list_positions()


def test_short_skipped_when_not_borrowable(tmp_path, cfg):
    short_day = day_bars([(99, 99.2, 98.8, 99)], breakout=(99.6, 99.6, 99.0, 99.2))
    broker, _, log, make = setup(tmp_path, cfg, {"SPY": short_day}, shortable=(True, False))
    make().run()
    assert broker.orders == []
    skips = [r for r in log.order_rows(OPEN.date()) if r["event"] == "skip"]
    assert len(skips) == 1 and "borrow" in skips[0]["reason"]


def test_short_trade(tmp_path, cfg):
    short_day = day_bars([(99.1, 99.2, 96.0, 96.2)], breakout=(99.6, 99.6, 99.0, 99.2))
    _, _, log, make = setup(tmp_path, cfg, {"SPY": short_day})
    make().run()
    [t] = trades(log)
    assert (t["side"], t["exit_reason"]) == ("short", "tp")
    assert float(t["pnl_usd"]) > 0


def test_replay_cli(tmp_path, monkeypatch, capsys):
    import trader

    source = FakeSource({"SPY": day_bars(TP_DAY)})
    monkeypatch.setattr(trader, "make_market_data", lambda feed="iex": source)
    out = tmp_path / "replay"
    assert trader.main(["--replay", "2026-09-15", "2026-09-13", "--data-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 trades" in printed and "$594.00" in printed
    assert len(TradeLog(out, "replay").trades()) == 1


def test_replay_refuses_live_data_dir(monkeypatch):
    import trader

    with pytest.raises(SystemExit):
        trader.main(["--replay", "2026-09-15", "--data-dir", str(trader.LIVE_DATA)])


def test_backend_without_native_brackets_is_refused(tmp_path, cfg):
    from broker import BrokerError, Capabilities

    broker, _, _, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)})
    broker.capabilities = Capabilities(native_brackets=False)
    with pytest.raises(BrokerError, match="bracket"):
        make()
    assert broker.orders == []


def test_session_mirrors_every_write_into_the_database(tmp_path, cfg):
    import db

    _, _, _, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)})
    make().run()
    conn = db.connect(tmp_path / db.DB_NAME)
    assert db.compare(conn, "replay", *db.csv_rows(tmp_path)) == []
    assert conn.execute("SELECT exit_reason FROM trades").fetchone()[0] == "tp"


def test_dry_run_session_records_snapshots_and_order_states(tmp_path, cfg):
    import db

    clock = SimClock(OPEN - timedelta(minutes=5))
    broker = SimBroker(FakeSource({"SPY": day_bars(TP_DAY)}), clock, 100_000)
    log = TradeLog(tmp_path, "dry-run")
    Session(cfg, broker, clock, log, LOGGER, OPEN, CLOSE, "dry-run", poll_seconds=30, halt_path=None).run()
    conn = db.connect(tmp_path / db.DB_NAME)
    snaps = conn.execute("SELECT * FROM equity_snapshots ORDER BY ts").fetchall()
    times = [pd.Timestamp(s["ts"]) for s in snaps]
    assert len(snaps) > 10 and all(b - a >= pd.Timedelta(seconds=60) for a, b in zip(times[:-2], times[1:-1]))
    assert {s["book"] for s in snaps} == {"dry_run"}
    held = conn.execute("SELECT * FROM position_snapshots WHERE symbol = 'SPY'").fetchall()
    assert held and all(p["qty"] == 198 and p["avg_entry_price"] == 101 for p in held)
    first = conn.execute("SELECT * FROM equity_snapshots WHERE ts = ?", (held[0]["ts"],)).fetchone()
    assert first["long_value"] == held[0]["market_value"] and first["positions"] == 1
    assert first["cash"] == pytest.approx(first["equity"] - first["long_value"])
    last = snaps[-1]
    assert last["positions"] == 0 and last["equity"] == float(log.trades()[0]["pnl_usd"]) + 100_000
    orders = {o["order_type"]: o for o in conn.execute("SELECT * FROM orders")}
    entry = orders["market"]
    assert (entry["status"], entry["filled_qty"], entry["parent_order_id"]) == ("filled", 198, None)
    assert orders["limit"]["status"] == "filled" and orders["stop"]["status"] == "canceled"
    assert orders["limit"]["parent_order_id"] == orders["stop"]["parent_order_id"] == entry["order_id"]
    assert all(o.status == orders[o.order_type]["status"] for top in broker.orders for o in (top, *top.legs))


def test_replay_session_takes_no_snapshots(tmp_path, cfg):
    import db

    _, _, _, make = setup(tmp_path, cfg, {"SPY": day_bars(TP_DAY)})
    make().run()
    conn = db.connect(tmp_path / db.DB_NAME)
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


@pytest.mark.parametrize("mode, saved", [("live", True), ("dry-run", False)])
def test_live_session_saves_its_minute_bars(tmp_path, cfg, mode, saved):
    import db

    clock = SimClock(OPEN - timedelta(minutes=5))
    broker = SimBroker(FakeSource({"SPY": day_bars(TP_DAY), "QQQ": day_bars(DRIFT_DAY)}), clock, 100_000)
    log = TradeLog(tmp_path, mode)
    session = Session(cfg | {"watchlist": ["SPY", "QQQ"]}, broker, clock, log, LOGGER, OPEN, CLOSE, mode,
                      poll_seconds=30, halt_path=None).run()
    conn = db.connect(tmp_path / db.DB_NAME)
    rows = conn.execute("SELECT * FROM bars WHERE symbol = 'SPY' ORDER BY ts").fetchall()
    if not saved:
        assert rows == []
        return
    minutes = int((session.clock.now() - OPEN).total_seconds() // 60)
    assert {r["timeframe"] for r in rows} == {"1Min"} and abs(len(rows) - minutes) <= 1
    assert rows[0]["ts"] == OPEN.isoformat() and (rows[15]["open"], rows[15]["high"]) == (100.5, 101.2)
    assert conn.execute("SELECT COUNT(DISTINCT symbol) FROM bars").fetchone()[0] == 2
