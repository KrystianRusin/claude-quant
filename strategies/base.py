"""The strategy interface: types shared by every strategy module.

A strategy module lives in <state dir>/strategies/<name>.py (data/strategies/ in production) and exposes:

    NAME: str                  the module name, used as config.strategy.name
    VERSION: int               must match config.strategy_version
    INTERFACE: int             the INTERFACE_VERSION below that the module was written against
    PARAMS: dict               bounds rules for its own config keys (see config_bounds.json
                               for the rule format); central rules in config_bounds.json win
    generate_signals(bars, state, params) -> list[Signal]
    validate_params(params) -> list[str]   optional extra checks

Strategies only return signals. They never place orders or do I/O.
"""
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

# Raise this whenever SessionState, Signal or the module contract above changes incompatibly.
# The registry refuses strategies written for another version.
INTERFACE_VERSION = 1


@dataclass(frozen=True)
class SessionState:
    session_open: datetime
    now: datetime
    entry_window_end: datetime
    entered: frozenset  # symbols already traded or skipped today


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str  # "long" or "short"
    bar_time: pd.Timestamp  # start of the bar that triggered the signal
    price: float  # reference entry price
    stop: float
    take_profit: float
    range_pct: float = float("nan")
