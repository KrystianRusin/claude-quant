-- Price bars the bots fetched (ticket 005c), so the dashboard draws its charts without calling the
-- broker. Shared by claude-quant and claude-sentiment-trading; keep this file byte-identical in both
-- repos (tests pin its sha256), like 001_core.sql. Not per book: bars are the same market for all.

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,        -- 1Min (claude-quant's sessions) or 1Day (claude-sentiment-trading)
    ts TEXT NOT NULL,               -- 1Min: the bar's start, ISO 8601 with the ET offset; 1Day: YYYY-MM-DD
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    vwap REAL,
    PRIMARY KEY (symbol, timeframe, ts)
);
