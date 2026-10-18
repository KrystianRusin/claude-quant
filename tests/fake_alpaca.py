"""An in-memory stand-in for alpaca-py's TradingClient, for testing AlpacaExecution without network.

It returns objects shaped like alpaca-py's models (enum fields, string quantities, nested legs) and
raises alpaca-py's APIError the way the paper API does, so the tests exercise the real translation
code in alpaca_backend.py. `settle(price)` fills every working market order at `price`.
"""
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType

PAPER_URL = "https://paper-api.alpaca.markets"
WORKING = {OrderStatus.ACCEPTED, OrderStatus.NEW, OrderStatus.HELD}


def api_error(status, code, message):
    response = requests.Response()
    response.status_code = status
    return APIError(f'{{"code":{code},"message":"{message}"}}', requests.HTTPError(response=response))


class FakeTrading:
    def __init__(self, base_url=PAPER_URL):
        self._base_url = base_url
        self.orders = []  # top-level orders
        self.positions = {}  # symbol -> [qty, avg]
        self._ids = itertools.count(1)

    def _order(self, req, side, order_type, status, limit=None, stop=None, cid=None):
        return SimpleNamespace(
            id=f"alp-{next(self._ids)}", client_order_id=cid or f"leg-{next(self._ids)}", symbol=req.symbol,
            side=side, qty=str(req.qty), order_type=order_type, type=order_type, status=status,
            filled_qty="0", filled_avg_price=None, filled_at=None, created_at=datetime.now(timezone.utc),
            limit_price=None if limit is None else str(limit), stop_price=None if stop is None else str(stop),
            legs=None)

    def _all(self):
        for o in self.orders:
            yield o, None
            for leg in o.legs or []:
                yield leg, o

    def _find(self, order_id):
        for o, parent in self._all():
            if o.id == order_id:
                return o, parent
        raise api_error(404, 40410000, "order not found")

    # orders

    def submit_order(self, req):
        if any(o.client_order_id == req.client_order_id for o, _ in self._all()):
            raise api_error(422, 40010001, "client_order_id must be unique")
        if req.qty <= 0:
            raise api_error(422, 40010001, "qty must be > 0")
        order = self._order(req, req.side, OrderType.MARKET, OrderStatus.ACCEPTED, cid=req.client_order_id)
        if req.order_class == OrderClass.BRACKET:
            exit_side = OrderSide.SELL if req.side == OrderSide.BUY else OrderSide.BUY
            order.legs = [self._order(req, exit_side, OrderType.LIMIT, OrderStatus.HELD,
                                      limit=req.take_profit.limit_price),
                          self._order(req, exit_side, OrderType.STOP, OrderStatus.HELD,
                                      stop=req.stop_loss.stop_price)]
        self.orders.append(order)
        return order

    def get_order_by_id(self, order_id, filter=None):
        return self._find(order_id)[0]

    def get_order_by_client_id(self, client_order_id):
        for o, _ in self._all():
            if o.client_order_id == client_order_id:
                return o
        raise api_error(404, 40410000, "order not found")

    def get_orders(self, req):
        return [o for o in self.orders if o.created_at > req.after]

    def cancel_order_by_id(self, order_id):
        order, parent = self._find(order_id)
        if order.status not in WORKING:
            raise api_error(422, 42210000, f"order is already in {order.status.value} state")
        for o in (parent.legs if parent else [order, *(order.legs or [])]):
            if o.status in WORKING:  # canceling one leg cancels its OCO sibling
                o.status = OrderStatus.CANCELED

    def close_all_positions(self, cancel_orders=False):
        if cancel_orders:
            for o, _ in self._all():
                if o.status in WORKING:
                    o.status = OrderStatus.CANCELED
        self.positions.clear()

    # account

    def get_account(self):
        return SimpleNamespace(equity="100000", last_equity="100000", buying_power="400000",
                               shorting_enabled=True, trading_blocked=False, account_blocked=False)

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, qty=str(q), avg_entry_price=str(avg), current_price=str(avg))
                for s, (q, avg) in self.positions.items()]

    # test control

    def settle(self, price=100.0):
        for o in self.orders:
            if o.status == OrderStatus.ACCEPTED:
                o.status, o.filled_qty, o.filled_avg_price = OrderStatus.FILLED, o.qty, str(price)
                o.filled_at = datetime.now(timezone.utc)
                signed = float(o.qty) * (1 if o.side == OrderSide.BUY else -1)
                qty, _ = self.positions.get(o.symbol, [0.0, price])
                if qty + signed:
                    self.positions[o.symbol] = [qty + signed, price]
                else:
                    self.positions.pop(o.symbol, None)
                for leg in o.legs or []:
                    if leg.status == OrderStatus.HELD:
                        leg.status = OrderStatus.NEW
