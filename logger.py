"""CSV trade/order/summary logs and the error log, all under one data directory."""
import csv
import logging
import math
from pathlib import Path

import pandas as pd

TRADE_FIELDS = ["date", "symbol", "side", "qty", "entry_time", "entry_price", "exit_time",
                "exit_price", "exit_reason", "pnl_usd", "pnl_r", "range_pct",
                "entry_minute_after_open", "config_version", "strategy", "strategy_version"]
ORDER_FIELDS = ["timestamp", "date", "mode", "event", "symbol", "side", "qty", "order_type",
                "price", "stop_price", "take_profit", "range_pct", "order_id", "client_order_id",
                "status", "reason"]
SUMMARY_FIELDS = ["date", "trades", "wins", "losses", "gross_pnl", "equity_start", "equity_end",
                  "halted", "notes"]
EXIT_REASONS = ("tp", "sl", "time", "halt", "carryover")


def _fmt_time(ts):
    return ts.isoformat(timespec="seconds") if ts is not None else ""


def trade_row(*, day, symbol, side, qty, entry_time, entry_price, exit_time, exit_price,
              exit_reason, stop, range_pct, session_open, config_version, strategy, strategy_version):
    """Build one trades.csv row; pnl_r is P&L per share over the planned risk per share."""
    direction = 1 if side == "long" else -1
    pnl_per_share = (exit_price - entry_price) * direction
    risk = abs(entry_price - stop) if stop is not None else 0
    qty = int(qty) if float(qty).is_integer() else qty
    minute = int((entry_time - session_open).total_seconds() // 60) if entry_time else ""
    return {
        "date": day.isoformat(), "symbol": symbol, "side": side, "qty": qty,
        "entry_time": _fmt_time(entry_time), "entry_price": round(entry_price, 4),
        "exit_time": _fmt_time(exit_time), "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason, "pnl_usd": round(pnl_per_share * qty, 2),
        "pnl_r": round(pnl_per_share / risk, 4) if risk > 0 else "",
        "range_pct": "" if range_pct is None or math.isnan(range_pct) else round(range_pct, 4),
        "entry_minute_after_open": minute, "config_version": config_version,
        "strategy": strategy, "strategy_version": strategy_version,
    }


class TradeLog:
    def __init__(self, data_dir, mode):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.trades_path = self.dir / "trades.csv"
        self.orders_path = self.dir / "orders.csv"
        self.summary_path = self.dir / "daily_summary.csv"

    def _append(self, path, fields, row):
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def _read(self, path):
        if not path.exists():
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def order_event(self, now, event, symbol="", **fields):
        row = {"timestamp": _fmt_time(now), "date": now.date().isoformat(), "mode": self.mode,
               "event": event, "symbol": symbol, **fields}
        self._append(self.orders_path, ORDER_FIELDS, row)

    def order_rows(self, day):
        return [r for r in self._read(self.orders_path) if r["date"] == day.isoformat()]

    def trade(self, row):
        """Append a closed trade unless the same (date, symbol, entry_time) is already logged."""
        key = (row["date"], row["symbol"], row["entry_time"])
        if any((r["date"], r["symbol"], r["entry_time"]) == key for r in self._read(self.trades_path)):
            return False
        self._append(self.trades_path, TRADE_FIELDS, row)
        return True

    def trades(self, day=None):
        rows = self._read(self.trades_path)
        return [r for r in rows if day is None or r["date"] == day.isoformat()]

    def daily_summary(self, row):
        """Write the summary row for row['date'], replacing any earlier row for that date."""
        rows = [r for r in self._read(self.summary_path) if r["date"] != row["date"]] + [row]
        rows.sort(key=lambda r: r["date"])
        with open(self.summary_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def clear(self):
        for path in (self.trades_path, self.orders_path, self.summary_path):
            path.unlink(missing_ok=True)


def setup_logging(data_dir, name="trader"):
    """Console logging plus data_dir/trader.log for warnings, errors and tracebacks."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    file = logging.FileHandler(Path(data_dir) / "trader.log", encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.INFO)
    log.addHandler(file)
    log.propagate = False
    return log


def read_trades_frame(path):
    """trades.csv as a DataFrame with parsed times; empty frame with the right columns if missing."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=TRADE_FIELDS)
    df = pd.read_csv(path)
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.tz_convert("America/New_York")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df

