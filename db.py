"""SQLite storage: data/trader.db, its migrations, the CSV mirror, backups.

During the dual-write phase (ticket 005a) the CSV files stay the source of truth and every CSV write
is mirrored here; a database error is logged and never stops trading. `check` compares the two.

    python db.py migrate            create or upgrade every book's database; a new one imports its CSVs
    python db.py import             rebuild each book's rows from its CSV files (idempotent)
    python db.py check              compare the CSV files with the database; exit 1 on a mismatch
    python db.py backup [DEST]      consistent copy of the paper database (SQLite backup API); without
                                    DEST, to data/backups/trader-<time>.db, keeping the newest 30
    python db.py restore SRC        replace the paper database with a backup

Each data directory has its own trader.db next to its CSVs: data/ is the paper book, and
data/dry_run/ and data/replay/ hold the simulated books.
"""
import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
DB_NAME = "trader.db"
BOOKS = {"live": "paper", "dry-run": "dry_run", "replay": "replay"}  # TradeLog mode -> book
RENAMES = {"timestamp": "ts"}  # CSV column -> table column
DROPPED = {"mode"}  # CSV columns the book column replaces
BACKUPS_KEPT = 30
LOG = logging.getLogger("db")


def connect(path, migrate_schema=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if migrate_schema:
        migrate(conn)
    return conn


def migrations():
    """[(number, path)] in order; files are NNN_name.sql."""
    found = []
    for f in MIGRATIONS.glob("*.sql"):
        m = re.match(r"(\d+)_", f.name)
        if m:
            found.append((int(m.group(1)), f))
    return sorted(found)


def schema_version(conn):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def migrate(conn):
    """Apply the migrations newer than the database, each in one transaction. Returns their numbers."""
    current, applied = schema_version(conn), []
    for number, f in migrations():
        if number <= current:
            continue
        conn.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\n"
                           f"INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '{number}');\n"
                           "COMMIT;")
        applied.append(number)
    return applied


# ---- rows

def _columns(conn, table):
    return {r["name"]: r["type"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _cast(value, kind):
    if value is None or value == "":
        return None
    if kind in ("REAL", "INTEGER"):
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return int(value.lower() == "true")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if kind == "INTEGER" else number
    return str(value)


def _plain(value):
    """An extra field as JSON would hold it, the same whether it came from a live write or a CSV import."""
    if isinstance(value, str):
        if value in ("True", "False"):
            return value == "True"
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() and "." not in value else number
    return value


def to_row(conn, table, record, book):
    """A CSV-style dict as {column: value}: typed core columns, everything else in `extra` (JSON)."""
    columns = _columns(conn, table)
    row, extra = {"book": book}, {}
    for key, value in record.items():
        key = RENAMES.get(key, key)
        if key in DROPPED or key == "book":
            continue
        if key in columns and key not in ("id", "extra"):
            row[key] = _cast(value, columns[key])
        elif value not in ("", None):
            extra[key] = _plain(value)
    if "extra" in columns:
        row["extra"] = json.dumps(extra, sort_keys=True, default=str) if extra else None
    return row


def _insert(conn, table, row, verb="INSERT"):
    keys = list(row)
    conn.execute(f"{verb} INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                 [row[k] for k in keys])


def add_order_event(conn, book, record):
    _insert(conn, "order_events", to_row(conn, "order_events", record, book))


def add_trade(conn, book, record):
    row = to_row(conn, "trades", record, book)
    row.setdefault("entry_date", row.get("date"))  # intraday trades enter and exit on the same date
    for key in ("entry_time", "side", "symbol", "date"):  # unique key columns: never NULL
        row[key] = row.get(key) or ""
    _insert(conn, "trades", row, "INSERT OR IGNORE")


def put_summary(conn, book, record):
    _insert(conn, "daily_summary", to_row(conn, "daily_summary", record, book), "INSERT OR REPLACE")


def clear_book(conn, book):
    for table in ("order_events", "trades", "daily_summary"):
        conn.execute(f"DELETE FROM {table} WHERE book = ?", (book,))


def resync(conn, book, trades, orders, summaries):
    """Replace a book's rows with the given CSV rows, in one transaction."""
    with conn:
        clear_book(conn, book)
        for r in orders:
            add_order_event(conn, book, r)
        for r in trades:
            add_trade(conn, book, r)
        for r in summaries:
            put_summary(conn, book, r)


def compare(conn, book, trades, orders, summaries):
    """Differences between CSV rows and the database for one book, as readable strings."""
    problems = []

    def one(sql):
        return conn.execute(sql, (book,)).fetchone()[0]

    def total(rows, key):
        return round(sum(float(r[key]) for r in rows if r.get(key) not in ("", None)), 2)

    checks = [("order events", len(orders), one("SELECT COUNT(*) FROM order_events WHERE book = ?")),
              ("trades", len(trades), one("SELECT COUNT(*) FROM trades WHERE book = ?")),
              ("trade P&L", total(trades, "pnl_usd"),
               round(one("SELECT COALESCE(SUM(pnl_usd), 0) FROM trades WHERE book = ?"), 2)),
              ("daily summaries", len({r["date"] for r in summaries}),
               one("SELECT COUNT(*) FROM daily_summary WHERE book = ?")),
              ("summary equity_end", total({r["date"]: r for r in summaries}.values(), "equity_end"),
               round(one("SELECT COALESCE(SUM(equity_end), 0) FROM daily_summary WHERE book = ?"), 2))]
    for name, in_csv, in_db in checks:
        if in_csv != in_db:
            problems.append(f"{book}: {name} differ (CSV {in_csv}, database {in_db})")
    return problems


class Mirror:
    """Mirrors one book's CSV writes into a database. The write that creates the database imports the
    CSV history of every book in `sources` ([(book, directory)], default: this book, next to the
    database) instead. Errors are logged once per process and never raised."""

    def __init__(self, path, book, sources=None):
        self.path, self.book = Path(path), book
        self.sources = sources or [(book, self.path.parent)]
        self.warned = False

    def _run(self, fn, *args):
        try:
            new = not self.path.exists()
            conn = connect(self.path)
            try:
                if new:  # the first write: bring in the history, which already includes this row
                    for book, directory in self.sources:
                        resync(conn, book, *csv_rows(directory))
                    return
                with conn:
                    fn(conn, self.book, *args)
            finally:
                conn.close()
        except Exception:
            if not self.warned:
                self.warned = True
                LOG.warning("database write to %s failed; the CSV files are unaffected", self.path, exc_info=True)

    def order_event(self, record):
        self._run(add_order_event, record)

    def trade(self, record):
        self._run(add_trade, record)

    def summary(self, record):
        self._run(put_summary, record)

    def clear(self):
        self._run(clear_book)


def _copy(src, dest):
    s, d = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def backup(src, dest):
    """Consistent copy of a live database, safe while it is being written."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _copy(src, dest)


def restore(src, dest):
    """Overwrite `dest` with the contents of backup `src` (through SQLite, so its WAL stays consistent)."""
    if not Path(src).exists():
        raise FileNotFoundError(src)
    _copy(src, dest)


# ---- the CSV books on disk

def _read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def book_dirs():
    """[(data directory, book)] that exist."""
    import config

    data = Path(config.STATE_DIR)
    found = [(data, "paper"), (data / "dry_run", "dry_run"), (data / "replay", "replay")]
    return [(d, b) for d, b in found if d.is_dir()]


def csv_rows(directory):
    return (_read_csv(directory / "trades.csv"), _read_csv(directory / "orders.csv"),
            _read_csv(directory / "daily_summary.csv"))


def rotated_backup(folder, kept=BACKUPS_KEPT):
    """A new timestamped backup path in `folder`, after deleting all but the newest kept - 1 old ones."""
    from datetime import datetime

    folder.mkdir(parents=True, exist_ok=True)
    for old in sorted(folder.glob("trader-*.db"))[:-(kept - 1) or None]:
        old.unlink()
    return folder / f"trader-{datetime.now():%Y%m%d-%H%M%S}.db"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["migrate", "import", "check", "backup", "restore"])
    ap.add_argument("path", nargs="?", help="destination for backup, source for restore")
    args = ap.parse_args(argv)
    import config

    live = Path(config.STATE_DIR) / DB_NAME
    if args.command == "backup":
        connect(live).close()
        dest = Path(args.path) if args.path else rotated_backup(Path(config.STATE_DIR) / "backups")
        backup(live, dest)
        print(f"backed up {live} to {dest}")
        return 0
    if args.command == "restore":
        if not args.path:
            ap.error("restore needs the backup to restore from")
        restore(args.path, live)
        print(f"restored {live} from {args.path}")
        return 0
    problems = []
    for directory, book in book_dirs():
        new = not (directory / DB_NAME).exists()
        conn = connect(directory / DB_NAME)
        try:
            if args.command == "migrate":
                print(f"{directory / DB_NAME}: schema version {schema_version(conn)}")
                if new:
                    trades, orders, summaries = csv_rows(directory)
                    resync(conn, book, trades, orders, summaries)
                    print(f"{book}: new database, imported {len(trades)} trades, {len(orders)} order events, "
                          f"{len(summaries)} daily summaries")
            elif args.command == "import":
                trades, orders, summaries = csv_rows(directory)
                resync(conn, book, trades, orders, summaries)
                print(f"{book}: imported {len(trades)} trades, {len(orders)} order events, "
                      f"{len(summaries)} daily summaries")
            else:
                found = compare(conn, book, *csv_rows(directory))
                problems += found
                print(f"{book}: {'OK' if not found else 'MISMATCH'}")
        finally:
            conn.close()
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
