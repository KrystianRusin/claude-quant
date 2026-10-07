from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 9, 15, 9, 30, tzinfo=ET)
CLOSE = datetime(2026, 9, 15, 16, 0, tzinfo=ET)


def make_bars(rows, start=OPEN):
    """rows: (open, high, low, close[, volume[, vwap]]) per consecutive minute from `start`."""
    records = []
    for r in rows:
        o, h, lo, c = r[:4]
        v = r[4] if len(r) > 4 else 1000
        vw = r[5] if len(r) > 5 else (h + lo + c) / 3
        records.append({"open": o, "high": h, "low": lo, "close": c, "volume": v, "vwap": vw})
    index = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(len(rows))])
    return pd.DataFrame(records, index=index)


def flat_range(n=15, low=99.5, high=100.5):
    """n range bars oscillating inside [low, high], touching both edges."""
    mid = (low + high) / 2
    rows = [(mid, high, low, mid)]
    q = (high - low) / 4
    rows += [(mid, mid + q, mid - q, mid)] * (n - 1)
    return rows
