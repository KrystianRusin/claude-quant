"""Alpaca backends: market data (free IEX feed) and paper-only order execution.

The only module that imports alpaca-py. It translates Alpaca's order statuses and API errors into
the broker-neutral ones in broker.py. Alpaca has no live brokerage for Canadian residents, so the
execution backend always uses the paper endpoint and refuses any other.
"""
import logging
import os
from datetime import timedelta

import pandas as pd
from dotenv import load_dotenv

import broker as b

LOG = logging.getLogger(__name__)

# Alpaca order status -> neutral status. tests/test_broker.py checks that every status alpaca-py
# knows is listed.
STATUS_MAP = {
    "new": b.NEW, "accepted": b.NEW, "pending_new": b.NEW, "accepted_for_bidding": b.NEW,
    "pending_replace": b.NEW, "pending_review": b.NEW, "done_for_day": b.NEW, "stopped": b.NEW,
    "held": b.HELD, "suspended": b.HELD,
    "partially_filled": b.PARTIALLY_FILLED, "pending_cancel": b.PENDING_CANCEL,
    "filled": b.FILLED, "canceled": b.CANCELED, "replaced": b.CANCELED, "expired": b.EXPIRED,
    "rejected": b.REJECTED,
    "calculated": None,  # done for the day, settlement pending: filled if fully filled, else expired
}


def assert_paper_url(url):
    if "paper-api" not in str(url):
        raise b.LiveAccountError(f"refusing to run: Alpaca endpoint is not the paper endpoint ({url})")


def load_credentials():
    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment or .env")
    return key, secret


def bars_to_frames(df):
    """Split alpaca's (symbol, timestamp) bar frame into {symbol: frame indexed by ET bar start}."""
    out = {}
    if df is None or df.empty:
        return out
    for symbol, g in df.groupby(level=0):
        g = g.droplevel(0)
        g.index = pd.DatetimeIndex(g.index).tz_convert(b.ET)
        out[symbol] = g[[c for c in b.BAR_COLUMNS if c in g.columns]].sort_index()
    return out


def _float(v):
    return None if v is None else float(v)


def _value(v):
    return getattr(v, "value", v)


def neutral_status(status, qty, filled_qty):
    status = _value(status)
    if status not in STATUS_MAP:
        LOG.warning("unknown Alpaca order status %r; treating the order as open", status)
        return b.NEW
    if STATUS_MAP[status] is None:
        return b.FILLED if qty and filled_qty >= qty else b.EXPIRED
    return STATUS_MAP[status]


def order_from_alpaca(o):
    qty, filled = float(o.qty or 0), float(o.filled_qty or 0)
    return b.Order(
        id=str(o.id), client_order_id=o.client_order_id, symbol=o.symbol,
        side=_value(o.side), qty=qty, order_type=_value(o.order_type or o.type),
        status=neutral_status(o.status, qty, filled), filled_qty=filled,
        filled_avg_price=_float(o.filled_avg_price), filled_at=b.to_et(o.filled_at),
        created_at=b.to_et(o.created_at), limit_price=_float(o.limit_price),
        stop_price=_float(o.stop_price),
        legs=tuple(order_from_alpaca(leg) for leg in (o.legs or [])),
    )


def _translate(e, client_order_id=None):
    """An alpaca APIError as a BrokerError subclass."""
    if client_order_id and "client_order_id must be unique" in str(e):
        return b.DuplicateOrderError(client_order_id)
    if client_order_id and e.status_code in (403, 422):
        return b.OrderRejectedError(str(e))
    return b.BrokerError(f"Alpaca API error ({e.status_code}): {e}")


class AlpacaMarketData(b.MarketData):
    def __init__(self, feed="iex"):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key, secret = load_credentials()
        self.trading = TradingClient(key, secret, paper=True)  # calendar, clock and assets
        self.data = StockHistoricalDataClient(key, secret)
        self.feed = feed

    def is_market_open(self):
        return bool(self.trading.get_clock().is_open)

    def session(self, day):
        from alpaca.trading.requests import GetCalendarRequest

        days = [d for d in self.trading.get_calendar(GetCalendarRequest(start=day, end=day)) if d.date == day]
        if not days:
            return None
        return b.to_et(days[0].open), b.to_et(days[0].close)

    def trading_days(self, end, count):
        from alpaca.trading.requests import GetCalendarRequest

        days = self.trading.get_calendar(GetCalendarRequest(start=end - timedelta(days=count * 2 + 10), end=end))
        return [d.date for d in days][-count:]

    def _bars(self, symbols, timeframe, start, end):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest

        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=timeframe, start=start, end=end,
                               feed=DataFeed(self.feed))
        return bars_to_frames(self.data.get_stock_bars(req).df)

    def get_bars(self, symbols, start, end):
        from alpaca.data.timeframe import TimeFrame

        return self._bars(list(symbols), TimeFrame.Minute, start, end)

    def daily_bars(self, symbol, start, end):
        from alpaca.data.timeframe import TimeFrame

        return self._bars(symbol, TimeFrame.Day, start, end).get(symbol)

    def daily_bars_many(self, symbols, start, end, batch=1000):
        from alpaca.data.timeframe import TimeFrame

        out = {}
        symbols = list(symbols)
        for i in range(0, len(symbols), batch):
            out.update(self._bars(symbols[i:i + batch], TimeFrame.Day, start, end))
        return out

    def asset_flags(self, symbol):
        asset = self.trading.get_asset(symbol)
        return asset.shortable, asset.easy_to_borrow

    def list_equities(self):
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        assets = self.trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE,
                                                              asset_class=AssetClass.US_EQUITY))
        return [{"symbol": a.symbol, "name": a.name or "", "exchange": _value(a.exchange),
                 "shortable": bool(a.shortable), "easy_to_borrow": bool(a.easy_to_borrow)}
                for a in assets if a.tradable]


class AlpacaExecution(b.ExecutionBroker):
    capabilities = b.Capabilities(native_brackets=True)

    def __init__(self, trading=None):
        if trading is None:
            from alpaca.trading.client import TradingClient

            key, secret = load_credentials()
            trading = TradingClient(key, secret, paper=True)
        self.trading = trading
        assert_paper_url(self.base_url)

    @property
    def base_url(self):
        url = self.trading._base_url
        return str(getattr(url, "value", url))

    def is_paper(self):
        return "paper-api" in self.base_url

    def _call(self, fn, *args, client_order_id=None, **kwargs):
        from alpaca.common.exceptions import APIError

        try:
            return fn(*args, **kwargs)
        except APIError as e:
            raise _translate(e, client_order_id) from e

    def get_account(self):
        assert_paper_url(self.base_url)
        a = self._call(self.trading.get_account)
        if a.trading_blocked or a.account_blocked:
            raise b.BrokerError("paper account is blocked from trading")
        return b.Account(equity=float(a.equity), last_equity=float(a.last_equity),
                         buying_power=float(a.buying_power), shorting_enabled=bool(a.shorting_enabled))

    def _submit(self, req):
        return order_from_alpaca(self._call(self.trading.submit_order, req, client_order_id=req.client_order_id))

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

        return order_from_alpaca(self._call(self.trading.get_order_by_id, order_id,
                                            filter=GetOrderByIdRequest(nested=True)))

    def get_order_by_client_id(self, client_order_id):
        from alpaca.common.exceptions import APIError

        try:
            o = self.trading.get_order_by_client_id(client_order_id)
        except APIError as e:
            if e.status_code == 404:
                return None
            raise _translate(e) from e
        return self.get_order(o.id)

    def list_orders(self, after):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._call(self.trading.get_orders, GetOrdersRequest(status=QueryOrderStatus.ALL, after=after,
                                                                       nested=True, limit=500))
        return [order_from_alpaca(o) for o in orders]

    def cancel_order(self, order_id):
        from alpaca.common.exceptions import APIError

        try:
            self.trading.cancel_order_by_id(order_id)
        except APIError as e:
            # 422: already pending cancel, canceled or filled. Cancelling one bracket leg
            # cancels its OCO sibling, so this is the normal result for the second leg.
            if e.status_code != 422:
                raise _translate(e) from e

    def list_positions(self):
        return {p.symbol: b.Position(p.symbol, float(p.qty), float(p.avg_entry_price))
                for p in self._call(self.trading.get_all_positions)}

    def close_all(self):
        self._call(self.trading.close_all_positions, cancel_orders=True)
