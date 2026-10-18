"""trader.db: migrations, the shared core schema, the CSV mirror, import, check, backup and restore."""
import hashlib
import json
import re
from datetime import timedelta

import pytest

import db
from helpers import OPEN
from logger import TradeLog
from test_logger import row

# The core schema is byte-identical in claude-sentiment-trading. Change both copies together, then
# update this hash in both repos.
CORE_SHA256 = "07ee2f1a050a5fca03d6c8bcd7ba56ced6fbb9f56addf6e799e4519142593581"


def test_core_schema_is_the_shared_one():
    assert hashlib.sha256((db.MIGRATIONS / "001_core.sql").read_bytes()).hexdigest() == CORE_SHA256


def test_migration_numbers_are_unique_and_quant_owns_200_up():
    numbers = [n for n, _ in db.migrations()]
    assert len(numbers) == len(set(numbers)) and numbers[0] == 1
    assert all(n == 1 or n >= 200 for n in numbers)  # 2-99 core, 100-199 sentiment, 200+ quant


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
    assert {"meta", "order_events", "trades", "daily_summary"} <= tables
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
