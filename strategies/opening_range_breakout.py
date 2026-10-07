"""Opening range breakout (ORB). Pure functions: bars in, signals out.

Bars are pandas DataFrames indexed by the ET bar start time, with columns
open, high, low, close, volume and optionally vwap (the bar's own VWAP).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from strategies.base import Signal

NAME = "opening_range_breakout"
VERSION = 1
PARAMS = {
    "opening_range_minutes": {"type": "int", "min": 5, "max": 60},
    "stop_loss_r": {"type": "float", "min": 0.25, "max": 1.5},
    "take_profit_r": {"type": "float", "min": 1.0, "max": 4.0},
    "min_range_pct": {"type": "float", "min": 0.05, "max": 1.0},
    "max_range_pct": {"type": "float", "min": 0.5, "max": 5.0},
    "require_above_vwap_for_long": {"type": "bool"},
    "allow_shorts": {"type": "bool"},
}

BAR = timedelta(minutes=1)
MAX_SIGNAL_AGE = timedelta(minutes=2)


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float

    @property
    def pct(self):
        """Range width as a percent of its midpoint price."""
        mid = (self.high + self.low) / 2
        return (self.high - self.low) / mid * 100


def validate_params(params):
    errors = []
    if params["min_range_pct"] >= params["max_range_pct"]:
        errors.append("strategy: min_range_pct must be below max_range_pct")
    range_end = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=params["opening_range_minutes"])).time()
    entry_end = datetime.strptime(params["entry_window_end"], "%H:%M").time()
    if entry_end <= range_end:
        errors.append(f"strategy: entry_window_end must be after the opening range ends ({range_end:%H:%M})")
    return errors


def opening_range(bars, session_open, minutes):
    """High and low of the bars in [open, open + minutes), or None if too few bars."""
    end = session_open + timedelta(minutes=minutes)
    window = bars[(bars.index >= session_open) & (bars.index < end)]
    if len(window) < max(1, minutes // 2):
        return None
    return OpeningRange(high=float(window["high"].max()), low=float(window["low"].min()))


def session_vwap(bars):
    """Cumulative session VWAP at each bar close, using the bar VWAP when present."""
    price = bars["vwap"] if "vwap" in bars else (bars["high"] + bars["low"] + bars["close"]) / 3
    volume = bars["volume"].astype(float)
    cum_vol = volume.cumsum()
    return (price * volume).cumsum() / cum_vol.where(cum_vol > 0)


def stop_and_target(side, entry, rng, stop_loss_r, take_profit_r):
    """Stop sits `stop_loss_r` of the way to the far side of the range; target is `take_profit_r` x R."""
    if side == "long":
        stop = entry - stop_loss_r * (entry - rng.low)
        return stop, entry + take_profit_r * (entry - stop)
    stop = entry + stop_loss_r * (rng.high - entry)
    return stop, entry - take_profit_r * (stop - entry)


def find_signal(symbol, bars, session_open, params, entry_window_end):
    """The first breakout of the day for one symbol, or None."""
    p = params
    bars = bars[bars.index >= session_open]
    rng = opening_range(bars, session_open, p["opening_range_minutes"])
    if rng is None or not (p["min_range_pct"] <= rng.pct <= p["max_range_pct"]):
        return None
    vwap = session_vwap(bars)
    range_end = session_open + timedelta(minutes=p["opening_range_minutes"])
    candidates = bars[(bars.index >= range_end) & (bars.index + BAR <= entry_window_end)]
    use_vwap = p["require_above_vwap_for_long"]
    for ts, bar in candidates.iterrows():
        close, v = float(bar["close"]), float(vwap.loc[ts])
        side = None
        if close > rng.high and (not use_vwap or close > v):
            side = "long"
        elif p["allow_shorts"] and close < rng.low and (not use_vwap or close < v):
            side = "short"
        if side:
            stop, tp = stop_and_target(side, close, rng, p["stop_loss_r"], p["take_profit_r"])
            return Signal(symbol, side, ts, close, stop, tp, rng.pct)
    return None


def generate_signals(bars, state, params):
    """Fresh first-breakout signals for symbols not yet entered today.

    A signal is fresh if its bar closed within MAX_SIGNAL_AGE of `state.now`,
    so a restart late in the morning does not chase an old breakout.
    """
    signals = []
    for symbol, frame in bars.items():
        if symbol in state.entered or frame is None or frame.empty:
            continue
        sig = find_signal(symbol, frame, state.session_open, params, state.entry_window_end)
        if sig and state.now - (sig.bar_time + BAR) <= MAX_SIGNAL_AGE:
            signals.append(sig)
    return sorted(signals, key=lambda x: (x.bar_time, x.symbol))
