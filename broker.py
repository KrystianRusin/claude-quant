"""Broker interface plus the Alpaca paper backend. Paper trading only.

Run `python broker.py` to check credentials, the account, the calendar and bar data.

Other backends (another broker's paper API, or a simulator fed by free data)
implement `Broker` and return the plain types below. trader.py, risk.py and the
strategies only ever see this interface.
"""
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "vwap"]
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held",
                 "accepted_for_bidding", "pending_cancel", "pending_replace", "done_for_day"}


class LiveEndpointError(RuntimeError):
    pass


class DuplicateOrderError(RuntimeError):
    """An order with this client_order_id already exists."""


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
    status: str
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


class Broker(ABC):
    """What trader.py needs from a broker backend. All datetimes are ET-aware."""

    @abstractmethod
    def get_account(self) -> Account: ...

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
    def asset_flags(self, symbol):
        """(shortable, easy_to_borrow)."""

    @abstractmethod
    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id) -> Order:
        """Market entry with stop and take-profit legs; side is "long" or "short"."""

    @abstractmethod
    def submit_market_order(self, symbol, side, qty, client_order_id) -> Order:
        """Plain market order; side is "buy" or "sell"."""

    @abstractmethod
    def get_order(self, order_id) -> Order: ...

    @abstractmethod
    def get_order_by_client_id(self, client_order_id) -> Order | None: ...

    @abstractmethod
    def list_orders(self, after) -> list:
        """All orders created after `after`, any status, with legs."""

    @abstractmethod
    def cancel_order(self, order_id): ...

    @abstractmethod
    def list_positions(self) -> dict:
        """{symbol: Position}."""

    @abstractmethod
    def close_all(self):
        """Cancel every open order and close every position at market."""


def assert_paper_url(url):
    if "paper-api" not in str(url):
        raise LiveEndpointError(f"refusing to run: trading endpoint is not a paper endpoint ({url})")


def load_credentials():
    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment or .env")
    return key, secret


def to_et(ts):
    """Make a datetime timezone-aware in ET; naive values are taken as ET."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


def bars_to_frames(df):
    """Split alpaca's (symbol, timestamp) bar frame into {symbol: frame indexed by ET bar start}."""
    out = {}
    if df is None or df.empty:
        return out
    for symbol, g in df.groupby(level=0):
        g = g.droplevel(0)
        g.index = pd.DatetimeIndex(g.index).tz_convert(ET)
        out[symbol] = g[[c for c in BAR_COLUMNS if c in g.columns]].sort_index()
    return out


def _float(v):
    return None if v is None else float(v)


def _value(v):
    return getattr(v, "value", v)


def order_from_alpaca(o):
    return Order(
        id=str(o.id), client_order_id=o.client_order_id, symbol=o.symbol,
        side=_value(o.side), qty=float(o.qty or 0), order_type=_value(o.order_type or o.type),
        status=_value(o.status), filled_qty=float(o.filled_qty or 0),
        filled_avg_price=_float(o.filled_avg_price), filled_at=to_et(o.filled_at),
        created_at=to_et(o.created_at), limit_price=_float(o.limit_price),
        stop_price=_float(o.stop_price),
        legs=tuple(order_from_alpaca(leg) for leg in (o.legs or [])),
    )


class AlpacaBroker(Broker):
    def __init__(self, feed="iex"):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key, secret = load_credentials()
        self.trading = TradingClient(key, secret, paper=True)
        assert_paper_url(self.base_url)
        self.data = StockHistoricalDataClient(key, secret)
        self.feed = feed

    @property
    def base_url(self):
        url = self.trading._base_url
        return str(getattr(url, "value", url))

    def get_account(self):
        assert_paper_url(self.base_url)
        a = self.trading.get_account()
        if a.trading_blocked or a.account_blocked:
            raise RuntimeError("paper account is blocked from trading")
        return Account(equity=float(a.equity), last_equity=float(a.last_equity),
                       buying_power=float(a.buying_power), shorting_enabled=bool(a.shorting_enabled))

    def is_market_open(self):
        return bool(self.trading.get_clock().is_open)

    def session(self, day):
        from alpaca.trading.requests import GetCalendarRequest

        days = [d for d in self.trading.get_calendar(GetCalendarRequest(start=day, end=day)) if d.date == day]
        if not days:
            return None
        return to_et(days[0].open), to_et(days[0].close)

    def trading_days(self, end, count):
        from alpaca.trading.requests import GetCalendarRequest

        days = self.trading.get_calendar(GetCalendarRequest(start=end - timedelta(days=count * 2 + 10), end=end))
        return [d.date for d in days][-count:]

    def get_bars(self, symbols, start, end):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=DataFeed(self.feed))
        return bars_to_frames(self.data.get_stock_bars(req).df)

    def daily_bars(self, symbol, start, end):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed(self.feed))
        return bars_to_frames(self.data.get_stock_bars(req).df).get(symbol)

    def asset_flags(self, symbol):
        asset = self.trading.get_asset(symbol)
        return asset.shortable, asset.easy_to_borrow

    def list_equities(self):
        """Active, tradable US equities and ETFs as dicts."""
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        assets = self.trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE,
                                                              asset_class=AssetClass.US_EQUITY))
        return [{"symbol": a.symbol, "name": a.name or "", "exchange": _value(a.exchange),
                 "shortable": bool(a.shortable), "easy_to_borrow": bool(a.easy_to_borrow)}
                for a in assets if a.tradable]

    def daily_bars_many(self, symbols, start, end, batch=1000):
        """{symbol: daily bar frame} for many symbols, fetched in batches."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        out = {}
        symbols = list(symbols)
        for i in range(0, len(symbols), batch):
            req = StockBarsRequest(symbol_or_symbols=symbols[i:i + batch], timeframe=TimeFrame.Day,
                                   start=start, end=end, feed=DataFeed(self.feed))
            out.update(bars_to_frames(self.data.get_stock_bars(req).df))
        return out

    def _submit(self, req):
        from alpaca.common.exceptions import APIError

        try:
            return order_from_alpaca(self.trading.submit_order(req))
        except APIError as e:
            if "client_order_id must be unique" in str(e):
                raise DuplicateOrderError(req.client_order_id) from e
            raise

    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        return self._submit(MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=OrderSide.BUY if side == "long" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            client_order_id=client_order_id,
            take_profit=TakeProfitRequest(limit_price=take_profit),
            stop_loss=StopLossRequest(stop_price=stop_price),
        ))

    def submit_market_order(self, symbol, side, qty, client_order_id):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        return self._submit(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide(side),
                                               time_in_force=TimeInForce.DAY,
                                               client_order_id=client_order_id))

    def get_order(self, order_id):
        from alpaca.trading.requests import GetOrderByIdRequest

        return order_from_alpaca(self.trading.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True)))

    def get_order_by_client_id(self, client_order_id):
        from alpaca.common.exceptions import APIError

        try:
            o = self.trading.get_order_by_client_id(client_order_id)
        except APIError:
            return None
        return self.get_order(o.id)

    def list_orders(self, after):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after,
                                                          nested=True, limit=500))
        return [order_from_alpaca(o) for o in orders]

    def cancel_order(self, order_id):
        self.trading.cancel_order_by_id(order_id)

    def list_positions(self):
        return {p.symbol: Position(p.symbol, float(p.qty), float(p.avg_entry_price))
                for p in self.trading.get_all_positions()}

    def close_all(self):
        self.trading.close_all_positions(cancel_orders=True)


BACKENDS = {"alpaca": AlpacaBroker}


def make_broker(feed="iex"):
    """Build the backend named by BROKER_BACKEND (default "alpaca")."""
    load_dotenv()
    name = os.environ.get("BROKER_BACKEND", "alpaca")
    if name not in BACKENDS:
        raise RuntimeError(f"unknown BROKER_BACKEND {name!r}; available: {sorted(BACKENDS)}")
    return BACKENDS[name](feed=feed)


def main():
    broker = make_broker()
    acct = broker.get_account()
    print(f"paper account OK: equity={acct.equity} last_equity={acct.last_equity} "
          f"buying_power={acct.buying_power} shorting_enabled={acct.shorting_enabled}")
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
    except (LiveEndpointError, RuntimeError) as e:
        print(e)
        sys.exit(1)
