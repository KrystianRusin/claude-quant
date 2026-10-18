-- Core schema shared by claude-quant and claude-sentiment-trading. Keep this file byte-identical in
-- both repos (tests pin its sha256); the dashboard relies on these tables meaning the same thing.
-- Migrations are additive only: never drop or rename what released code reads.
-- book: 'paper' (the paper account), 'dry_run' or 'replay'. Times are ISO 8601 with the ET offset.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY,
    book TEXT NOT NULL,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    event TEXT NOT NULL,            -- submit, fill, cancel, skip, reject, adopt, or an order status
    symbol TEXT NOT NULL DEFAULT '',
    side TEXT,
    qty REAL,
    order_type TEXT,
    price REAL,
    stop_price REAL,
    take_profit REAL,
    order_id TEXT,
    client_order_id TEXT,
    status TEXT,
    reason TEXT,
    extra TEXT                      -- JSON object of bot-specific fields
);
CREATE INDEX IF NOT EXISTS order_events_book_date ON order_events (book, date);
CREATE INDEX IF NOT EXISTS order_events_symbol ON order_events (symbol, date);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    book TEXT NOT NULL,
    date TEXT NOT NULL,             -- the exit date
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,             -- long or short
    qty REAL NOT NULL,
    entry_date TEXT,
    entry_time TEXT,
    entry_price REAL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    stop_price REAL,
    pnl_usd REAL,
    pnl_r REAL,
    pnl_pct REAL,
    hold_days REAL,
    strategy TEXT,
    strategy_version TEXT,
    config_version TEXT,
    extra TEXT,                     -- JSON object of bot-specific fields
    UNIQUE (book, symbol, side, entry_time, date)
);
CREATE INDEX IF NOT EXISTS trades_book_date ON trades (book, date);
CREATE INDEX IF NOT EXISTS trades_symbol ON trades (symbol, date);

CREATE TABLE IF NOT EXISTS daily_summary (
    book TEXT NOT NULL,
    date TEXT NOT NULL,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    gross_pnl REAL,
    equity_start REAL,
    equity_end REAL,
    halted INTEGER,                 -- 0 or 1
    notes TEXT,
    day_pnl REAL,
    unrealized_pnl REAL,
    positions INTEGER,
    gross_exposure REAL,
    net_exposure REAL,
    entries INTEGER,
    extra TEXT,                     -- JSON object of bot-specific fields
    PRIMARY KEY (book, date)
);
