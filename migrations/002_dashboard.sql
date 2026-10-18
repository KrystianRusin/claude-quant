-- Facts for the dashboard (ticket 005b), shared by claude-quant and claude-sentiment-trading. Keep this
-- file byte-identical in both repos (tests pin its sha256), like 001_core.sql.
-- None of these tables has a CSV copy: `db.py import` leaves them alone.

-- Account value through the day: claude-quant every minute of its session, claude-sentiment-trading
-- every 5 minutes from `trader.py snapshot`, and both once more at the close.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    book TEXT NOT NULL,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL,                      -- equity minus the market value of the positions
    long_value REAL,
    short_value REAL,               -- zero or negative
    positions INTEGER,
    PRIMARY KEY (book, ts)
);

-- The positions behind each equity snapshot, at the same ts.
CREATE TABLE IF NOT EXISTS position_snapshots (
    book TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,              -- negative for shorts
    avg_entry_price REAL,
    price REAL,                     -- NULL when the broker gave no current price
    market_value REAL,
    unrealized_pnl REAL,
    PRIMARY KEY (book, ts, symbol)
);

-- The latest known state of every order, updated whenever a bot sees it change. order_events keeps
-- the history.
CREATE TABLE IF NOT EXISTS orders (
    book TEXT NOT NULL,
    order_id TEXT NOT NULL,
    client_order_id TEXT,
    parent_order_id TEXT,           -- the entry order, for a bracket leg or an attached stop
    symbol TEXT NOT NULL,
    side TEXT,                      -- buy or sell
    qty REAL,
    order_type TEXT,                -- market, limit or stop
    status TEXT NOT NULL,           -- new, held, partially_filled, pending_cancel, filled, canceled, rejected, expired
    filled_qty REAL,
    filled_avg_price REAL,
    limit_price REAL,
    stop_price REAL,
    created_at TEXT,
    filled_at TEXT,
    updated_at TEXT NOT NULL,       -- when the bot last saw a change
    PRIMARY KEY (book, order_id)
);
CREATE INDEX IF NOT EXISTS orders_symbol ON orders (symbol, updated_at);
CREATE INDEX IF NOT EXISTS orders_status ON orders (book, status);

-- Every scheduled job (scripts/job.sh) and every deploy (job = 'deploy').
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,           -- running, ok, failed, or lost (it stopped without reporting back)
    exit_code INTEGER,
    error TEXT,                     -- the end of the job's output, when it failed
    git_sha TEXT,
    config_version TEXT,
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS runs_job ON runs (job, started_at);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);

-- What drove the results, one row each time it changed. version is the value results carry
-- (config_version, strategy_version, prompt_sha256, memory_sha256), or the start of sha256 when results
-- carry nothing. A row with sha256 '' and no content records that the thing was removed.
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,             -- config, strategy, watchlist, prompt or memory
    name TEXT NOT NULL DEFAULT '',  -- which one, for kinds with several (a strategy, a prompt, the shadow config)
    version TEXT,
    sha256 TEXT NOT NULL,           -- of content
    effective_from TEXT NOT NULL,
    reason TEXT,                    -- what recorded it: deploy, review, session, config upgrade, memory save, ...
    content TEXT
);
CREATE INDEX IF NOT EXISTS versions_kind ON versions (kind, name, id);
