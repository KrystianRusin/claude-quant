"""SQLite storage: data/trader.db, its migrations, the CSV mirror, backups.

During the dual-write phase (ticket 005a) the CSV files stay the source of truth and every CSV write
is mirrored here; a database error is logged and never stops trading. `check` compares the two.

    python db.py migrate            create or upgrade every book's database; a new one imports its CSVs
    python db.py import             rebuild each book's rows from its CSV files (idempotent)
    python db.py check              compare the CSV files with the database; exit 1 on a mismatch
    python db.py backup [DEST]      consistent copy of the paper database (SQLite backup API) without the
                                    bars; without DEST, to data/backups/trader-<time>.db, keeping the newest 30
    python db.py purge              apply the retention rules (old bars, snapshots, runs) to every book
    python db.py restore SRC        replace the paper database with a backup
    python db.py run-start JOB [--started TS] [--sha SHA] [--pid PID]
                                    record a job as running; prints the run id (scripts/job.sh)
    python db.py run-end ID CODE [--output FILE]
                                    record how it ended; a failure keeps the end of FILE as the error
    python db.py versions [--reason WHY]
                                    record the config, strategies, watchlist, memory and review prompt
                                    if they changed

Each data directory has its own trader.db next to its CSVs: data/ is the paper book, and
data/dry_run/ and data/replay/ hold the simulated books (the shadow dry run uses data/shadow/).
Orders and snapshots (ticket 005b) have no CSV copy; runs and versions are kept in the paper
database only.
"""
import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
DB_NAME = "trader.db"
ET = ZoneInfo("America/New_York")
CSV_TABLES = ("order_events", "trades", "daily_summary")  # mirrored from the CSV files
BOOK_TABLES = CSV_TABLES + ("orders", "equity_snapshots", "position_snapshots")
ERROR_CHARS = 4000  # of a failed job's output kept in runs.error
REVIEW_PROMPT = ROOT / "review" / "REVIEW_PROMPT.md"
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


def applied_migrations(conn):
    """Numbers of the migrations this database has. Databases from before the list was kept (meta key
    'migrations') had only core migrations, so they have every number up to schema_version."""
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('schema_version', 'migrations')").fetchall())
    except sqlite3.OperationalError:
        return set()
    if "migrations" in meta:
        return {int(n) for n in meta["migrations"].split(",") if n}
    return set(range(1, int(meta.get("schema_version", 0)) + 1))


def migrate(conn):
    """Apply every migration the database does not have yet, each in one transaction, in number order.
    Returns their numbers. A core migration added after a repo's own (100+) ones is still applied, as
    the list of applied numbers is kept, not just the highest. schema_version stays the highest."""
    done, applied = applied_migrations(conn), []
    for number, f in migrations():
        if number in done:
            continue
        done.add(number)
        listed = ",".join(str(n) for n in sorted(done))
        conn.executescript(f"BEGIN;\n{f.read_text(encoding='utf-8')}\n"
                           f"INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '{max(done)}');\n"
                           f"INSERT OR REPLACE INTO meta (key, value) VALUES ('migrations', '{listed}');\n"
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


def put_order(conn, book, record):
    """Insert or update one order's current state; a field missing from `record` keeps its stored value."""
    row = to_row(conn, "orders", record, book)
    row.pop("extra", None)
    keys = list(row)
    updates = ", ".join(f"{k} = COALESCE(excluded.{k}, orders.{k})" for k in keys if k not in ("book", "order_id"))
    conn.execute(f"INSERT INTO orders ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))}) "
                 f"ON CONFLICT (book, order_id) DO UPDATE SET {updates}", [row[k] for k in keys])


def put_snapshot(conn, book, equity, positions):
    """One equity_snapshots row and its position_snapshots rows, all at equity['ts']."""
    _insert(conn, "equity_snapshots", to_row(conn, "equity_snapshots", equity, book), "INSERT OR REPLACE")
    for p in positions:
        _insert(conn, "position_snapshots", to_row(conn, "position_snapshots", p | {"ts": equity["ts"]}, book),
                "INSERT OR REPLACE")


def open_order_ids(conn, book):
    return [r[0] for r in conn.execute(
        "SELECT order_id FROM orders WHERE book = ? AND status IN ('new', 'held', 'partially_filled', 'pending_cancel')",
        (book,))]


def bar_rows(frames, timeframe):
    """bars-table rows from {symbol: frame of BAR_COLUMNS}, indexed by bar start time or by date."""
    rows = []
    for symbol, df in frames.items():
        if df is None or df.empty:
            continue
        for ts, r in zip(df.index, df.to_dict("records")):
            values = [r.get(c) for c in ("open", "high", "low", "close", "volume", "vwap")]
            rows.append((symbol, timeframe, ts.isoformat(),
                         *(None if v is None or v != v else float(v) for v in values)))  # NaN -> NULL
    return rows


def put_bars(conn, frames, timeframe):
    conn.executemany("INSERT OR REPLACE INTO bars (symbol, timeframe, ts, open, high, low, close, volume, vwap) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", bar_rows(frames, timeframe))


def clear_book(conn, book, tables=BOOK_TABLES):
    for table in tables:
        conn.execute(f"DELETE FROM {table} WHERE book = ?", (book,))


def resync(conn, book, trades, orders, summaries):
    """Replace a book's CSV-mirrored rows with the given CSV rows, in one transaction."""
    with conn:
        clear_book(conn, book, CSV_TABLES)
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


def open_db(path, sources):
    """(connection, new): the database at `path`, migrated. One that did not exist yet first imports
    the CSV history of every book in `sources` ([(book, directory)])."""
    new = not Path(path).exists()
    conn = connect(path)
    if new:
        for book, directory in sources:
            resync(conn, book, *csv_rows(directory))
    return conn, new


class Mirror:
    """Mirrors one book's CSV writes into a database, and writes the facts that only it holds (orders,
    snapshots). The write that creates the database imports the CSV history of every book in `sources`
    ([(book, directory)], default: this book, next to the database) first. Errors are logged once per
    process and never raised."""

    def __init__(self, path, book, sources=None):
        self.path, self.book = Path(path), book
        self.sources = sources or [(book, self.path.parent)]
        self.warned = False

    def _run(self, fn, *args, in_csv=True):
        try:
            conn, new = open_db(self.path, self.sources)
            try:
                if new and in_csv:  # the imported history already includes this row
                    return None
                with conn:
                    return fn(conn, self.book, *args)
            finally:
                conn.close()
        except Exception:
            if not self.warned:
                self.warned = True
                LOG.warning("database write to %s failed; the CSV files are unaffected", self.path, exc_info=True)
            return None

    def order_event(self, record):
        self._run(add_order_event, record)

    def trade(self, record):
        self._run(add_trade, record)

    def summary(self, record):
        self._run(put_summary, record)

    def order(self, record):
        self._run(put_order, record, in_csv=False)

    def snapshot(self, equity, positions):
        self._run(put_snapshot, equity, positions, in_csv=False)

    def open_order_ids(self):
        return self._run(open_order_ids, in_csv=False) or []

    def bars(self, frames, timeframe):
        """Bars are not per book; they go into this book's database for the dashboard's charts."""
        self._run(lambda conn, book: put_bars(conn, frames, timeframe), in_csv=False)

    def clear(self):
        self._run(clear_book)


# ---- runs and versions (not per book)

def now_iso():
    return datetime.now(ET).isoformat(timespec="seconds")


def _alive(pid):
    if os.name == "nt":  # os.kill would end the process there
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True


def run_start(conn, job, started_at, git_sha=None, config_version=None, pid=None):
    """Record a running job and return its id. Running rows whose process is gone become lost."""
    with conn:
        for r in conn.execute("SELECT id, pid FROM runs WHERE status = 'running' AND pid IS NOT NULL").fetchall():
            if not _alive(r["pid"]):
                conn.execute("UPDATE runs SET status = 'lost' WHERE id = ?", (r["id"],))
        return conn.execute("INSERT INTO runs (job, started_at, status, git_sha, config_version, pid) "
                            "VALUES (?, ?, 'running', ?, ?, ?)",
                            (job, started_at, git_sha, config_version, pid)).lastrowid


def run_end(conn, run_id, exit_code, ended_at, output=""):
    """Record how a run ended; a failure keeps the end of its output as the error."""
    error = (output[-ERROR_CHARS:].strip() or None) if exit_code else None
    with conn:
        conn.execute("UPDATE runs SET ended_at = ?, exit_code = ?, status = ?, error = ? WHERE id = ?",
                     (ended_at, exit_code, "ok" if exit_code == 0 else "failed", error, run_id))


def git_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_versions(conn, items, reason, at):
    """Add a versions row for each (kind, name, version, content) whose content differs from its latest
    row, and a removal row for each (kind, name) that has rows but is no longer among `items`.
    version None means "results carry nothing": the start of the sha256 is used. Returns the rows added
    as (kind, name, version)."""
    latest = {(r["kind"], r["name"]): r["sha256"] for r in conn.execute(
        "SELECT kind, name, sha256 FROM versions WHERE id IN (SELECT MAX(id) FROM versions GROUP BY kind, name)")}
    added, current = [], set()

    def add(kind, name, version, sha, content):
        conn.execute("INSERT INTO versions (kind, name, version, sha256, effective_from, reason, content) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, name, version, sha, at, reason, content))
        added.append((kind, name, version))

    with conn:
        for kind, name, version, content in items:
            current.add((kind, name))
            sha = _sha256(content)
            if latest.get((kind, name)) != sha:
                add(kind, name, sha[:12] if version is None else str(version), sha, content)
        for (kind, name), sha in sorted(latest.items()):
            if (kind, name) not in current and sha != "":
                add(kind, name, None, "", None)
    return added


def _read_text(path):
    return Path(path).read_text(encoding="utf-8") if Path(path).exists() else None


def _json_version(text):
    try:
        return str(json.loads(text)["version"])
    except (TypeError, ValueError, KeyError):
        return None


def config_version(state_dir):
    return _json_version(_read_text(Path(state_dir) / "config.json"))


def current_versions(state_dir):
    """[(kind, name, version, content)] for what drives the results: the config and the shadow config
    (version = config_version), each strategy (version = VERSION), the watchlist, the memory and the
    review prompt."""
    d = Path(state_dir)
    items = []
    for name, file in (("", "config.json"), ("shadow", "config.shadow.json")):
        text = _read_text(d / file)
        if text is not None:
            items.append(("config", name, _json_version(text), text))
    for f in sorted((d / "strategies").glob("*.py")):
        if not f.name.startswith("_"):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"^VERSION\s*=\s*(\d+)", text, re.MULTILINE)
            items.append(("strategy", f.stem, m.group(1) if m else None, text))
    for kind, name, path in (("watchlist", "", d / "watchlist.json"), ("memory", "", d / "memory.md"),
                             ("prompt", "review", REVIEW_PROMPT)):
        text = _read_text(path)
        if text is not None:
            items.append((kind, name, None, text))
    return items


def note_versions(state_dir, reason):
    """record_versions for the state in `state_dir` into its paper database. Never raises; returns the
    rows added."""
    path = Path(state_dir) / DB_NAME
    try:
        conn, _ = open_db(path, [("paper", Path(state_dir))])
        try:
            return record_versions(conn, current_versions(state_dir), reason, now_iso())
        finally:
            conn.close()
    except Exception:
        LOG.warning("recording versions in %s failed", path, exc_info=True)
        return []


# ---- retention (db.py purge, nightly). Trades, order events, daily summaries, orders and versions are
# kept forever; they are small and the bots learn from them.
MINUTE_BARS_DAYS = 90  # 1Min bars older than this go, except on days a trade in this database held the symbol
DAILY_BARS_DAYS = 730  # 1Day bars older than this go, except from 90 days before to 30 after a trade
SNAPSHOT_DAYS = 90  # older snapshots are thinned to the last one of each day
RUNS_DAYS = 90  # older runs that ended ok go; failed and lost runs are kept


def purge(conn, today):
    """Delete what the retention rules above let go. Returns {what: rows deleted}.

    Freed pages are reused by later writes, so the file stops growing; it is not vacuumed."""
    from datetime import timedelta

    def cut(days):
        return (today - timedelta(days=days)).isoformat()

    steps = {
        "1Min bars": ("DELETE FROM bars WHERE timeframe = '1Min' AND substr(ts, 1, 10) < ? AND NOT EXISTS ("
                      "SELECT 1 FROM trades t WHERE t.symbol = bars.symbol "
                      "AND substr(bars.ts, 1, 10) BETWEEN COALESCE(t.entry_date, t.date) AND t.date)",
                      cut(MINUTE_BARS_DAYS)),
        "1Day bars": ("DELETE FROM bars WHERE timeframe = '1Day' AND ts < ? AND NOT EXISTS ("
                      "SELECT 1 FROM trades t WHERE t.symbol = bars.symbol "
                      "AND bars.ts BETWEEN date(COALESCE(t.entry_date, t.date), '-90 days') AND date(t.date, '+30 days'))",
                      cut(DAILY_BARS_DAYS)),
        "equity snapshots": ("DELETE FROM equity_snapshots WHERE substr(ts, 1, 10) < ? AND ts < ("
                             "SELECT MAX(e.ts) FROM equity_snapshots e WHERE e.book = equity_snapshots.book "
                             "AND substr(e.ts, 1, 10) = substr(equity_snapshots.ts, 1, 10))", cut(SNAPSHOT_DAYS)),
        "position snapshots": ("DELETE FROM position_snapshots WHERE substr(ts, 1, 10) < ? AND NOT EXISTS ("
                               "SELECT 1 FROM equity_snapshots e WHERE e.book = position_snapshots.book "
                               "AND e.ts = position_snapshots.ts)", cut(SNAPSHOT_DAYS)),
        "ok runs": ("DELETE FROM runs WHERE status = 'ok' AND substr(started_at, 1, 10) < ?", cut(RUNS_DAYS)),
    }
    deleted = {}
    with conn:
        for what, (sql, before) in steps.items():  # equity before position snapshots: positions follow equity
            deleted[what] = conn.execute(sql, (before,)).rowcount
    return deleted


def strip_bars(path):
    """Remove the bars from a backup copy and compact it: bars are the bulk of the database, and the
    bots fetch them again (claude-sentiment-trading also keeps them in its bar cache)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM bars")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


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
    found = [(data, "paper"), (data / "dry_run", "dry_run"), (data / "shadow", "dry_run"),
             (data / "replay", "replay")]
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
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("migrate", "import", "check", "purge"):
        sub.add_parser(name)
    for name in ("backup", "restore"):
        sub.add_parser(name).add_argument("path", nargs="?", help="destination for backup, source for restore")
    s = sub.add_parser("run-start")
    s.add_argument("job")
    s.add_argument("--started", help="ISO time it started (default now)")
    s.add_argument("--sha", help="git commit (default the checkout's HEAD)")
    s.add_argument("--pid", type=int, help="process to watch: a run whose process is gone becomes lost")
    e = sub.add_parser("run-end")
    e.add_argument("id", type=int)
    e.add_argument("code", type=int)
    e.add_argument("--output", help="file holding the job's output")
    v = sub.add_parser("versions")
    v.add_argument("--reason", default="manual")
    args = ap.parse_args(argv)
    import config

    state = Path(config.STATE_DIR)
    live = state / DB_NAME
    if args.command in ("run-start", "run-end", "versions"):
        conn, _ = open_db(live, [("paper", state)])
        try:
            if args.command == "run-start":
                print(run_start(conn, args.job, args.started or now_iso(), args.sha or git_sha(),
                                config_version(state), args.pid))
            elif args.command == "run-end":
                output = ""
                if args.code and args.output and Path(args.output).exists():
                    output = Path(args.output).read_bytes()[-ERROR_CHARS:].decode("utf-8", "replace")
                run_end(conn, args.id, args.code, now_iso(), output)
            else:
                for kind, name, version in record_versions(conn, current_versions(state), args.reason, now_iso()):
                    print(f"recorded {kind}{' ' + name if name else ''} {version or '(removed)'}")
        finally:
            conn.close()
        return 0
    if args.command == "backup":
        connect(live).close()
        dest = Path(args.path) if args.path else rotated_backup(Path(config.STATE_DIR) / "backups")
        backup(live, dest)
        strip_bars(dest)
        print(f"backed up {live} to {dest} (without bars)")
        return 0
    if args.command == "restore":
        if not args.path:
            ap.error("restore needs the backup to restore from")
        restore(args.path, live)
        print(f"restored {live} from {args.path}")
        return 0
    if args.command == "purge":
        for directory, book in book_dirs():
            if (directory / DB_NAME).exists():
                conn = connect(directory / DB_NAME)
                try:
                    deleted = purge(conn, datetime.now(ET).date())
                finally:
                    conn.close()
                print(f"{directory / DB_NAME}: " + ", ".join(f"{n} {what}" for what, n in deleted.items()))
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
