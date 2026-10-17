"""Broker interface: broker-neutral types, the two backend roles, and the backend registry.

Run `python broker.py` to check credentials, the account, the calendar and bar data.

A backend fills one or both roles:
- `MarketData`: calendar, bars and asset flags (Alpaca's free IEX data today).
- `ExecutionBroker`: account, orders and positions.
trader.py sees one `Broker` that combines the two, so orders can go to one broker while data comes
from another. `BROKER_BACKEND` picks the execution backend and `DATA_BACKEND` the data backend
(both default to "alpaca"). Backends translate their own order statuses and errors into the ones
below; nothing outside a backend module imports a broker SDK.

Live accounts: every execution backend reports `is_paper()`. A backend connected to a live account
is refused unless ALLOW_LIVE=1 is set in the environment or .env.
"""
import importlib
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "vwap"]

# Order statuses. Backends map theirs onto these.
NEW = "new"  # working at the broker
HELD = "held"  # a bracket leg waiting for its entry to fill
PARTIALLY_FILLED = "partially_filled"
PENDING_CANCEL = "pending_cancel"
FILLED = "filled"
CANCELED = "canceled"
REJECTED = "rejected"
EXPIRED = "expired"
OPEN_STATUSES = frozenset({NEW, HELD, PARTIALLY_FILLED, PENDING_CANCEL})
FINAL_STATUSES = frozenset({FILLED, CANCELED, REJECTED, EXPIRED})
STATUSES = OPEN_STATUSES | FINAL_STATUSES


class BrokerError(RuntimeError):
    """A broker refused or failed a request."""


class DuplicateOrderError(BrokerError):
    """An order with this client_order_id already exists."""


class OrderRejectedError(BrokerError):
    """The broker rejected the order (buying power, invalid prices, symbol not tradable, ...)."""


class LiveAccountError(BrokerError):
    """The backend is connected to a live account and ALLOW_LIVE=1 is not set."""


@dataclass(frozen=True)
class Capabilities:
    native_brackets: bool  # entry + stop + take-profit as one order whose exit legs cancel each other


@dataclass(frozen=True)
class Account:
    equity: float
    last_equity: float  # equity at the previous close
    buying_power: float
    shorting_enabled: bool


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # negative for shorts
    avg_entry_price: float


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    order_type: str  # "market", "limit" or "stop"
    status: str  # one of STATUSES
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: datetime | None = None
    created_at: datetime | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    legs: tuple = field(default_factory=tuple)

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES


class MarketData(ABC):
    """Calendar and price data. All datetimes are ET-aware."""

    @abstractmethod
    def is_market_open(self) -> bool: ...

    @abstractmethod
    def session(self, day):
        """(open, close) datetimes for `day`, or None if the market is closed."""

    @abstractmethod
    def trading_days(self, end, count):
        """The last `count` trading dates on or before `end`."""

    @abstractmethod
    def get_bars(self, symbols, start, end):
        """{symbol: DataFrame of 1-minute bars indexed by ET bar start}."""

    @abstractmethod
    def daily_bars(self, symbol, start, end):
        """Daily bar frame for one symbol, or None."""

    @abstractmethod
    def asset_flags(self, symbol):
        """(shortable, easy_to_borrow)."""

    def daily_bars_many(self, symbols, start, end):
        """{symbol: daily bar frame}; backends with a batch endpoint override this."""
        frames = {s: self.daily_bars(s, start, end) for s in symbols}
        return {s: f for s, f in frames.items() if f is not None}

    def list_equities(self):
        """Active, tradable US equities and ETFs as dicts (symbol, name, exchange, shortable, easy_to_borrow)."""
        raise NotImplementedError(f"{type(self).__name__} cannot list the equity universe")


class ExecutionBroker(ABC):
    """Account, orders and positions. Statuses are the ones above; errors are BrokerError subclasses."""

    capabilities = Capabilities(native_brackets=False)

    @abstractmethod
    def is_paper(self) -> bool:
        """True when connected to a paper (simulated) account."""

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id) -> Order:
        """Market entry with stop and take-profit legs; side is "long" or "short".

        Only for backends with `capabilities.native_brackets`. The legs come back in `Order.legs` (a
        "limit" take-profit and a "stop"), and filling or canceling one cancels the other.
        Raises DuplicateOrderError if the client_order_id was used before."""

    @abstractmethod
    def submit_market_order(self, symbol, side, qty, client_order_id) -> Order:
        """Plain DAY market order; side is "buy" or "sell". Raises DuplicateOrderError on a reused id."""

    @abstractmethod
    def get_order(self, order_id) -> Order:
        """The order with its legs."""

    @abstractmethod
    def get_order_by_client_id(self, client_order_id) -> Order | None:
        """The order with its legs, or None if no order has this id."""

    @abstractmethod
    def list_orders(self, after) -> list:
        """All top-level orders created after `after`, any status, with legs."""

    @abstractmethod
    def cancel_order(self, order_id):
        """Request a cancel. Returns quietly if the order is already closed or being canceled.

        Canceling one bracket leg may cancel its sibling (an OCO pair), and a cancel can race a fill,
        so a second cancel failing that way is expected. Other failures raise BrokerError."""

    @abstractmethod
    def list_positions(self) -> dict:
        """{symbol: Position}."""

    @abstractmethod
    def close_all(self):
        """Cancel every open order and close every position at market."""


class Broker(ExecutionBroker, MarketData):
    """Everything trader.py needs: one execution role and one data role."""


class CombinedBroker(Broker):
    """A Broker made of an execution backend and a separate data backend."""

    def __init__(self, execution, data):
        self.execution = execution
        self.data = data
        self.capabilities = execution.capabilities

    # data
    def is_market_open(self):
        return self.data.is_market_open()

    def session(self, day):
        return self.data.session(day)

    def trading_days(self, end, count):
        return self.data.trading_days(end, count)

    def get_bars(self, symbols, start, end):
        return self.data.get_bars(symbols, start, end)

    def daily_bars(self, symbol, start, end):
        return self.data.daily_bars(symbol, start, end)

    def daily_bars_many(self, symbols, start, end):
        return self.data.daily_bars_many(symbols, start, end)

    def asset_flags(self, symbol):
        return self.data.asset_flags(symbol)

    def list_equities(self):
        return self.data.list_equities()

    # execution
    def is_paper(self):
        return self.execution.is_paper()

    def get_account(self):
        return self.execution.get_account()

    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id):
        return self.execution.submit_bracket_order(symbol, side, qty, stop_price, take_profit, client_order_id)

    def submit_market_order(self, symbol, side, qty, client_order_id):
        return self.execution.submit_market_order(symbol, side, qty, client_order_id)

    def get_order(self, order_id):
        return self.execution.get_order(order_id)

    def get_order_by_client_id(self, client_order_id):
        return self.execution.get_order_by_client_id(client_order_id)

    def list_orders(self, after):
        return self.execution.list_orders(after)

    def cancel_order(self, order_id):
        return self.execution.cancel_order(order_id)

    def list_positions(self):
        return self.execution.list_positions()

    def close_all(self):
        return self.execution.close_all()


def to_et(ts):
    """Make a datetime timezone-aware in ET; naive values are taken as ET."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


# Backends are "module:Class" so that importing broker never imports a broker SDK.
EXECUTION_BACKENDS = {"alpaca": "alpaca_backend:AlpacaExecution", "ibkr": "ibkr_backend:IBKRExecution"}
DATA_BACKENDS = {"alpaca": "alpaca_backend:AlpacaMarketData"}


def _backend(registry, variable):
    name = os.environ.get(variable, "alpaca")
    if name not in registry:
        raise RuntimeError(f"unknown {variable} {name!r}; available: {sorted(registry)}")
    target = registry[name]
    if isinstance(target, str):
        module, _, cls = target.partition(":")
        target = getattr(importlib.import_module(module), cls)
    return target


def check_live(execution):
    """Refuse a live account unless ALLOW_LIVE=1."""
    if not execution.is_paper() and os.environ.get("ALLOW_LIVE") != "1":
        raise LiveAccountError(f"refusing to run: {type(execution).__name__} is connected to a live account "
                               "and ALLOW_LIVE=1 is not set")
    return execution


def make_execution():
    """The execution backend named by BROKER_BACKEND, after the live-account check."""
    load_dotenv()
    return check_live(_backend(EXECUTION_BACKENDS, "BROKER_BACKEND")())


def make_market_data(feed="iex"):
    """The data backend named by DATA_BACKEND. Needs no trading access."""
    load_dotenv()
    return _backend(DATA_BACKENDS, "DATA_BACKEND")(feed=feed)


def make_broker(feed="iex"):
    return CombinedBroker(make_execution(), make_market_data(feed))


def main():
    broker = make_broker()
    acct = broker.get_account()
    print(f"{type(broker.execution).__name__} {'paper' if broker.is_paper() else 'LIVE'} account OK: "
          f"equity={acct.equity} last_equity={acct.last_equity} buying_power={acct.buying_power} "
          f"shorting_enabled={acct.shorting_enabled}")
    print(f"data from {type(broker.data).__name__}; native brackets: {broker.capabilities.native_brackets}")
    print(f"market open now: {broker.is_market_open()}")
    now = datetime.now(ET)
    days = broker.trading_days(now.date(), 3)
    print(f"recent trading days: {days}")
    for day in reversed(days):
        open_, _ = broker.session(day)
        if open_ + timedelta(minutes=5) <= now:
            break
    spy = broker.get_bars(["SPY"], open_, open_ + timedelta(minutes=5)).get("SPY")
    print(f"SPY 1-minute bars on {day} from {open_:%H:%M}:")
    print(spy if spy is not None else "  none returned")
    print(f"SPY (shortable, easy_to_borrow): {broker.asset_flags('SPY')}")
    print(f"open positions: {list(broker.list_positions())}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(e)
        sys.exit(1)
