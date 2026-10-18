"""trader.db: migrations, the shared core schema, the CSV mirror, import, check, backup and restore."""
import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

import db
import logger
from helpers import OPEN
from logger import TradeLog
from broker import FILLED, NEW, Order, Position
from test_logger import row

# The core migrations are byte-identical in claude-sentiment-trading. Change both copies together,
# then update these hashes in both repos.
CORE_SHA256 = {
    "001_core.sql": "07ee2f1a050a5fca03d6c8bcd7ba56ced6fbb9f56addf6e799e4519142593581",
    "002_dashboard.sql": "859a3602ba3a4e4e6274d22450b791222cf56011b824198473bdcb2f59299b05",
    "003_bars.sql": "4ffdf41d3b4d2f948e9b31ba6767efa7462734a1f5903806bd2c7351177d53d3",
}


def test_core_schema_is_the_shared_one():
    core = {f.name for n, f in db.migrations() if n < 100}
    assert core == set(CORE_SHA256)
    for name, sha in CORE_SHA256.items():
        assert hashlib.sha256((db.MIGRATIONS / name).read_bytes()).hexdigest() == sha, name


def test_migration_numbers_are_unique_and_quant_owns_200_up():
    numbers = [n for n, _ in db.migrations()]
    assert len(numbers) == len(set(numbers)) and numbers[0] == 1
    assert all(n < 100 or n >= 200 for n in numbers)  # 1-99 core, 100-199 sentiment, 200+ quant


def test_migrations_are_additive():
    for _, f in db.migrations():
        sql = re.sub(r"--.*", "", f.read_text()).upper()
        assert not re.search(r"\bDROP\b|\bRENAME\b", sql), f.name


def test_migrate_creates_schema_once(tmp_path):
    conn = db.connect(tmp_path / "t.db", migrate_schema=False)
    assert db.schema_version(conn) == 0
    assert db.migrate(conn) == [n for n, _ in db.migrations()]
    assert db.migrate(conn) == []
    assert db.schema_version(conn) == db.migrations()[-1][0]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "order_events", "trades", "daily_summary", "orders", "equity_snapshots", "position_snapshots",
            "runs", "versions", "bars"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def fill_log(log):
    log.order_event(OPEN, "submit", "SPY", side="long", qty=5, order_type="bracket", price=100.5,
                    stop_price=99.5, take_profit=102.5, range_pct=0.8, order_id="abc", client_order_id="pt-1",
                    status="new")
    log.order_event(OPEN + timedelta(minutes=1), "skip", "QQQ", reason="size rounds to zero")
    log.trade(row())
    log.trade(row())  # duplicate, ignored in both
    log.trade(row(entry_time=OPEN + timedelta(minutes=50), exit_price=102.0))
    log.daily_summary({"date": "2026-09-15", "trades": 2, "gross_pnl": 1, "equity_end": 100_001, "halted": False})
    log.daily_summary({"date": "2026-09-15", "trades": 2, "gross_pnl": 10, "equity_end": 100_010, "halted": True})


def test_trade_log_is_mirrored(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    conn = db.connect(tmp_path / db.DB_NAME)
    assert db.compare(conn, "paper", *db.csv_rows(tmp_path)) == []
    event = conn.execute("SELECT * FROM order_events WHERE event = 'submit'").fetchone()
    assert (event["book"], event["ts"], event["qty"], event["take_profit"]) == ("paper", OPEN.isoformat(), 5, 102.5)
    assert json.loads(event["extra"]) == {"range_pct": 0.8}
    trade = conn.execute("SELECT * FROM trades ORDER BY entry_time").fetchone()
    assert (trade["side"], trade["pnl_usd"], trade["pnl_r"], trade["entry_date"]) == ("short", 30.0, 2.0, "2026-09-15")
    assert json.loads(trade["extra"])["entry_minute_after_open"] == 20
    [summary] = conn.execute("SELECT * FROM daily_summary").fetchall()
    assert (summary["equity_end"], summary["halted"]) == (100_010, 1)


def test_live_write_and_csv_import_store_the_same_extra(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    conn = db.connect(tmp_path / db.DB_NAME)
    before = [r["extra"] for r in conn.execute("SELECT extra FROM trades ORDER BY id")]
    db.resync(conn, "paper", *db.csv_rows(tmp_path))
    assert [r["extra"] for r in conn.execute("SELECT extra FROM trades ORDER BY id")] == before


def test_book_names_follow_the_mode(tmp_path):
    TradeLog(tmp_path / "d", "dry-run").order_event(OPEN, "submit", "SPY")
    TradeLog(tmp_path / "r", "replay").order_event(OPEN, "submit", "SPY")
    assert db.connect(tmp_path / "d" / db.DB_NAME).execute("SELECT book FROM order_events").fetchone()[0] == "dry_run"
    assert db.connect(tmp_path / "r" / db.DB_NAME).execute("SELECT book FROM order_events").fetchone()[0] == "replay"


def test_clear_empties_the_book(tmp_path):
    log = TradeLog(tmp_path, "replay")
    fill_log(log)
    log.clear()
    conn = db.connect(tmp_path / db.DB_NAME)
    assert sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("order_events", "trades", "daily_summary")) == 0


def test_database_failure_never_stops_the_csv(tmp_path, caplog):
    (tmp_path / "blocker").write_text("not a directory")
    log = TradeLog(tmp_path, "live")
    log.db = db.Mirror(tmp_path / "blocker" / db.DB_NAME, "paper")
    log.order_event(OPEN, "submit", "SPY")
    log.order_event(OPEN, "fill", "SPY")
    assert len(log.order_rows(OPEN.date())) == 2
    assert sum("database write" in r.message for r in caplog.records) == 1  # warned once


def test_import_rebuilds_from_csv_and_check_finds_drift(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    conn = db.connect(tmp_path / db.DB_NAME)
    with conn:
        conn.execute("DELETE FROM trades")
        conn.execute("INSERT INTO order_events (book, ts, date, event) VALUES ('paper', 'x', 'x', 'stray')")
    problems = db.compare(conn, "paper", *db.csv_rows(tmp_path))
    assert any("trades differ" in p for p in problems) and any("order events" in p for p in problems)
    db.resync(conn, "paper", *db.csv_rows(tmp_path))
    db.resync(conn, "paper", *db.csv_rows(tmp_path))  # idempotent
    assert db.compare(conn, "paper", *db.csv_rows(tmp_path)) == []


def test_backup_and_restore(tmp_path):
    live = tmp_path / db.DB_NAME
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    db.backup(live, tmp_path / "backups" / "b.db")
    conn = db.connect(live)
    with conn:
        conn.execute("DELETE FROM trades")
    conn.close()
    db.restore(tmp_path / "backups" / "b.db", live)
    assert db.connect(live).execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
    with pytest.raises(FileNotFoundError):
        db.restore(tmp_path / "missing.db", live)


def test_cli_import_check_backup(tmp_path, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    fill_log(TradeLog(tmp_path, "live"))
    fill_log(TradeLog(tmp_path / "replay", "replay"))
    (tmp_path / db.DB_NAME).unlink()  # history written before the database existed
    for suffix in ("-wal", "-shm"):
        (tmp_path / (db.DB_NAME + suffix)).unlink(missing_ok=True)
    assert db.main(["check"]) == 1
    assert db.main(["import"]) == 0
    assert db.main(["check"]) == 0
    assert db.main(["backup", str(tmp_path / "b.db")]) == 0 and (tmp_path / "b.db").exists()
    out = capsys.readouterr().out
    assert "paper: imported 2 trades, 2 order events, 1 daily summaries" in out and "replay: OK" in out


def test_migrate_imports_history_into_a_new_database(tmp_path, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    fill_log(TradeLog(tmp_path, "live"))
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / (db.DB_NAME + suffix)).unlink(missing_ok=True)
    assert db.main(["migrate"]) == 0
    assert "paper: new database, imported 2 trades" in capsys.readouterr().out
    assert db.main(["migrate"]) == 0
    assert "imported" not in capsys.readouterr().out  # existing databases are only upgraded
    assert db.main(["check"]) == 0


def test_rotated_backups_keep_the_newest(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(5):
        (folder / f"trader-2026010{i}-030000.db").write_text("x")
    new = db.rotated_backup(folder, kept=3)
    assert sorted(p.name for p in folder.iterdir()) == ["trader-20260103-030000.db", "trader-20260104-030000.db"]
    assert new.parent == folder and new.name.startswith("trader-")


def test_first_mirrored_write_imports_the_csv_history(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    for suffix in ("", "-wal", "-shm"):  # history from before the database existed
        (tmp_path / (db.DB_NAME + suffix)).unlink(missing_ok=True)
    log.order_event(OPEN + timedelta(hours=1), "fill", "SPY", order_id="abc")
    log.trade(row(entry_time=OPEN + timedelta(minutes=90)))
    assert db.compare(db.connect(tmp_path / db.DB_NAME), "paper", *db.csv_rows(tmp_path)) == []


def order(status=NEW, **kw):
    stop = Order("o-stop", "pt-1-s", "SPY", "sell", 5, "stop", "held", stop_price=99.5)
    return Order(**({"id": "o-1", "client_order_id": "pt-1", "symbol": "SPY", "side": "buy", "qty": 5,
                     "order_type": "market", "status": status, "created_at": OPEN, "legs": (stop,)} | kw))


def test_order_states_are_upserted_only_when_they_change(tmp_path, monkeypatch):
    log = TradeLog(tmp_path, "live")
    writes = []
    real = log.db.order
    monkeypatch.setattr(log.db, "order", lambda r: (writes.append(r), real(r)))
    log.order_state(OPEN, order())
    log.order_state(OPEN + timedelta(seconds=30), order())  # unchanged: not written
    log.order_state(OPEN + timedelta(minutes=1), order(FILLED, filled_qty=5, filled_avg_price=100.5,
                                                        filled_at=OPEN + timedelta(minutes=1)))
    assert len(writes) == 3  # entry, leg, entry again
    conn = db.connect(tmp_path / db.DB_NAME)
    rows = {r["order_id"]: r for r in conn.execute("SELECT * FROM orders")}
    assert (rows["o-1"]["status"], rows["o-1"]["filled_avg_price"], rows["o-1"]["book"]) == ("filled", 100.5, "paper")
    assert rows["o-1"]["updated_at"] == (OPEN + timedelta(minutes=1)).isoformat()
    assert rows["o-1"]["created_at"] == OPEN.isoformat()
    assert (rows["o-stop"]["parent_order_id"], rows["o-stop"]["stop_price"]) == ("o-1", 99.5)
    log.order_state(OPEN, Order("o-stop", "pt-1-s", "SPY", "sell", 5, "stop", "canceled"))  # fetched alone
    stop = conn.execute("SELECT * FROM orders WHERE order_id = 'o-stop'").fetchone()
    assert (stop["status"], stop["parent_order_id"], stop["stop_price"]) == ("canceled", "o-1", 99.5)
    assert db.open_order_ids(conn, "paper") == []


def test_snapshot_rows():
    positions = {"SPY": Position("SPY", 10, 100.0, 101.0), "QQQ": Position("QQQ", -5, 200.0, 190.0),
                 "IWM": Position("IWM", 2, 50.0)}
    equity, rows = logger.snapshot_rows(OPEN, 100_000, positions)
    assert equity == {"ts": OPEN.isoformat(), "equity": 100_000, "cash": 100_000 - 1010 - 100 + 950,
                      "long_value": 1110, "short_value": -950, "positions": 3}
    by = {r["symbol"]: r for r in rows}
    assert by["QQQ"]["unrealized_pnl"] == 50 and by["SPY"]["unrealized_pnl"] == 10
    assert by["IWM"]["price"] is None and by["IWM"]["unrealized_pnl"] is None and by["IWM"]["market_value"] == 100


def test_import_keeps_database_only_rows_and_clear_removes_them(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    log.order_state(OPEN, order())
    log.snapshot(OPEN, 100_000, {"SPY": Position("SPY", 5, 100.0, 100.5)})
    conn = db.connect(tmp_path / db.DB_NAME)
    db.resync(conn, "paper", *db.csv_rows(tmp_path))
    counts = lambda: [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
                      for t in ("orders", "equity_snapshots", "position_snapshots")]
    assert counts() == [2, 1, 1]
    log.clear()
    assert counts() == [0, 0, 0]


def test_database_only_write_to_a_new_database_imports_history_and_keeps_the_write(tmp_path):
    log = TradeLog(tmp_path, "live")
    fill_log(log)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / (db.DB_NAME + suffix)).unlink(missing_ok=True)
    log.snapshot(OPEN, 100_000, {})
    conn = db.connect(tmp_path / db.DB_NAME)
    assert db.compare(conn, "paper", *db.csv_rows(tmp_path)) == []
    assert conn.execute("SELECT equity FROM equity_snapshots").fetchone()[0] == 100_000


def test_runs_start_end_and_lost(tmp_path):
    import subprocess

    conn = db.connect(tmp_path / db.DB_NAME)
    gone = subprocess.Popen(["true"])
    gone.wait()
    stale = db.run_start(conn, "trader", "2026-09-15T09:00:00-04:00", pid=gone.pid)
    quiet = db.run_start(conn, "manual", "2026-09-15T09:00:00-04:00")  # no pid: never marked lost
    ok = db.run_start(conn, "backup", "2026-09-16T03:00:00-04:00", "abc1234", "3", pid=os.getpid())
    db.run_end(conn, ok, 0, "2026-09-16T03:00:05-04:00", "all good")
    failed = db.run_start(conn, "review", "2026-09-16T17:00:00-04:00", pid=os.getpid())
    db.run_end(conn, failed, 2, "2026-09-16T17:01:00-04:00", "x" * 5000 + "Traceback: boom\n")
    runs = {r["id"]: r for r in conn.execute("SELECT * FROM runs")}
    assert runs[stale]["status"] == "lost" and runs[quiet]["status"] == "running"
    assert (runs[ok]["status"], runs[ok]["exit_code"], runs[ok]["error"], runs[ok]["git_sha"],
            runs[ok]["config_version"]) == ("ok", 0, None, "abc1234", "3")
    assert runs[failed]["status"] == "failed" and runs[failed]["error"].endswith("Traceback: boom")
    assert len(runs[failed]["error"]) <= db.ERROR_CHARS


def state_dir(tmp_path):
    import shutil

    d = tmp_path / "state"
    shutil.copytree(Path(db.ROOT) / "defaults", d, ignore=shutil.ignore_patterns("__pycache__"))
    return d


def test_versions_record_changes_only(tmp_path):
    d = state_dir(tmp_path)
    conn = db.connect(d / db.DB_NAME)
    first = db.record_versions(conn, db.current_versions(d), "deploy", "2026-09-15T08:00:00-04:00")
    assert {(k, n) for k, n, _ in first} == {("config", ""), ("strategy", "opening_range_breakout"),
                                            ("watchlist", ""), ("memory", ""), ("prompt", "review")}
    assert dict(((k, n), v) for k, n, v in first)[("strategy", "opening_range_breakout")] == "1"
    assert db.record_versions(conn, db.current_versions(d), "session", "2026-09-15T09:00:00-04:00") == []
    cfg = json.loads((d / "config.json").read_text())
    (d / "config.shadow.json").write_text(json.dumps(cfg | {"version": cfg["version"] + 1}))
    (d / "memory.md").write_text("# Memory\n\nchanged\n")
    added = db.record_versions(conn, db.current_versions(d), "review", "2026-09-16T17:30:00-04:00")
    assert sorted(added) == [("config", "shadow", str(cfg["version"] + 1)), ("memory", "", added[1][2])]
    (d / "config.shadow.json").unlink()
    assert db.record_versions(conn, db.current_versions(d), "review", "2026-09-17T17:30:00-04:00") == [
        ("config", "shadow", None)]
    removed = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchone()
    assert (removed["sha256"], removed["content"], removed["version"]) == ("", None, None)
    memory = conn.execute("SELECT * FROM versions WHERE kind = 'memory' ORDER BY id DESC").fetchone()
    assert memory["content"].endswith("changed\n") and memory["version"] == memory["sha256"][:12]
    assert memory["reason"] == "review" and memory["effective_from"] == "2026-09-16T17:30:00-04:00"


def test_cli_runs_and_versions(tmp_path, monkeypatch, capsys):
    import config

    d = state_dir(tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", d)
    assert db.main(["run-start", "trader", "--pid", str(os.getpid()), "--sha", "abc1234"]) == 0
    run_id = int(capsys.readouterr().out)
    (tmp_path / "out.log").write_text("starting\nTraceback: it broke\n")
    assert db.main(["run-end", str(run_id), "1", "--output", str(tmp_path / "out.log")]) == 0
    assert db.main(["versions", "--reason", "deploy"]) == 0
    assert "recorded strategy opening_range_breakout 1" in capsys.readouterr().out
    conn = db.connect(d / db.DB_NAME)
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["job"], run["status"], run["exit_code"], run["git_sha"]) == ("trader", "failed", 1, "abc1234")
    assert run["error"].endswith("it broke") and run["config_version"] == "1" and run["ended_at"]


def test_note_versions_never_raises(tmp_path, caplog):
    (tmp_path / "blocker").write_text("not a directory")
    assert db.note_versions(tmp_path / "blocker", "session") == []
    assert "recording versions" in caplog.text


def test_bar_rows_and_put_bars(tmp_path):
    import pandas as pd
    from datetime import date
    from helpers import make_bars

    minute = make_bars([(1, 2, 0.5, 1.5), (1.5, 2, 1, 1.8)])
    daily = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0],
                          "vwap": [float("nan")]}, index=[date(2026, 9, 15)])
    conn = db.connect(tmp_path / db.DB_NAME)
    db.put_bars(conn, {"SPY": minute, "EMPTY": minute.iloc[:0]}, "1Min")
    db.put_bars(conn, {"SPY": daily}, "1Day")
    db.put_bars(conn, {"SPY": minute}, "1Min")  # again: replaced, not duplicated
    rows = conn.execute("SELECT * FROM bars ORDER BY timeframe, ts").fetchall()
    assert [(r["timeframe"], r["ts"]) for r in rows] == [
        ("1Day", "2026-09-15"), ("1Min", OPEN.isoformat()), ("1Min", (OPEN + timedelta(minutes=1)).isoformat())]
    assert rows[0]["vwap"] is None and rows[2]["close"] == 1.8


def test_purge_follows_the_retention_rules(tmp_path):
    from datetime import date

    conn = db.connect(tmp_path / "p.db")
    today = date(2026, 10, 14)
    with conn:
        for sym, day in (("OLD", "2026-06-01"), ("TRADED", "2026-06-01"), ("NEW", "2026-10-01")):
            conn.execute("INSERT INTO bars (symbol, timeframe, ts) VALUES (?, '1Min', ?)", (sym, day + "T09:30:00-04:00"))
        for sym, day in (("OLD", "2024-01-02"), ("TRADED", "2024-01-02"), ("TRADED", "2023-06-01"), ("NEW", "2026-01-02")):
            conn.execute("INSERT INTO bars (symbol, timeframe, ts) VALUES (?, '1Day', ?)", (sym, day))
        conn.execute("INSERT INTO trades (book, date, symbol, side, qty, entry_date, entry_time) "
                     "VALUES ('paper', '2026-06-01', 'TRADED', 'long', 1, '2026-06-01', 'a')")
        conn.execute("INSERT INTO trades (book, date, symbol, side, qty, entry_date, entry_time) "
                     "VALUES ('paper', '2024-01-10', 'TRADED', 'long', 1, '2024-01-05', 'b')")
        for ts in ("2026-06-01T10:00:00-04:00", "2026-06-01T15:59:00-04:00", "2026-10-13T10:00:00-04:00",
                   "2026-10-13T15:59:00-04:00"):
            conn.execute("INSERT INTO equity_snapshots (book, ts, equity) VALUES ('paper', ?, 1)", (ts,))
            conn.execute("INSERT INTO position_snapshots (book, ts, symbol, qty) VALUES ('paper', ?, 'X', 1)", (ts,))
        conn.execute("INSERT INTO equity_snapshots (book, ts, equity) VALUES ('dry_run', '2026-06-01T10:00:00-04:00', 1)")
        for status, started in (("ok", "2026-06-01T03:00:00-04:00"), ("failed", "2026-06-01T03:00:00-04:00"),
                                ("lost", "2026-06-01T03:00:00-04:00"), ("ok", "2026-10-13T03:00:00-04:00")):
            conn.execute("INSERT INTO runs (job, started_at, status) VALUES ('j', ?, ?)", (started, status))
    assert db.purge(conn, today) == {"1Min bars": 1, "1Day bars": 2, "equity snapshots": 1,
                                     "position snapshots": 1, "ok runs": 1}
    bars = sorted((r[0], r[1], r[2][:10]) for r in conn.execute("SELECT symbol, timeframe, ts FROM bars"))
    assert bars == [("NEW", "1Day", "2026-01-02"), ("NEW", "1Min", "2026-10-01"),
                    ("TRADED", "1Day", "2024-01-02"), ("TRADED", "1Min", "2026-06-01")]
    kept = [r[0] for r in conn.execute("SELECT ts FROM equity_snapshots WHERE book = 'paper' ORDER BY ts")]
    assert kept == ["2026-06-01T15:59:00-04:00", "2026-10-13T10:00:00-04:00", "2026-10-13T15:59:00-04:00"]
    assert [r[0] for r in conn.execute("SELECT ts FROM position_snapshots ORDER BY ts")] == kept
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots WHERE book = 'dry_run'").fetchone()[0] == 1
    assert sorted(r[0] for r in conn.execute("SELECT status FROM runs")) == ["failed", "lost", "ok"]
    assert db.purge(conn, today) == dict.fromkeys(["1Min bars", "1Day bars", "equity snapshots",
                                                   "position snapshots", "ok runs"], 0)


def test_strip_bars_empties_only_the_bars(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    with conn:
        conn.executemany("INSERT INTO bars (symbol, timeframe, ts, close) VALUES ('SPY', '1Min', ?, 1)",
                         [(str(i),) for i in range(5000)])
        conn.execute("INSERT INTO runs (job, started_at, status) VALUES ('j', 't', 'ok')")
    conn.close()
    size = (tmp_path / "b.db").stat().st_size
    db.strip_bars(tmp_path / "b.db")
    conn = db.connect(tmp_path / "b.db")
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert (tmp_path / "b.db").stat().st_size < size
