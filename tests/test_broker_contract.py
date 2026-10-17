"""The contract every execution backend must meet; trader.py relies on all of it.

Each backend runs through a harness that can settle (fill) working market orders:
- sim: SimBroker on flat 1-minute bars at 100, settled by advancing the clock
- alpaca: AlpacaExecution on tests/fake_alpaca.py, so the Alpaca status and error translation is
  tested without network
A new backend (Interactive Brokers) adds a harness here and must pass before it trades.
"""
from datetime import timedelta

import pytest

from alpaca_backend import AlpacaExecution
from broker import (FILLED, STATUSES, Account, Capabilities, DuplicateOrderError, ExecutionBroker, Position)
from fake_alpaca import FakeTrading
from helpers import OPEN, make_bars
from simbroker import SimBroker, SimClock


class FlatSource:
    def __init__(self):
        self.bars = make_bars([(100.0, 100.0, 100.0, 100.0)] * 390)

    def get_bars(self, symbols, start, end):
        f = self.bars[(self.bars.index >= start) & (self.bars.index < end)]
        return {s: f for s in symbols}

    def session(self, day):
        return OPEN, OPEN.replace(hour=16)

    def trading_days(self, end, count):
        return [OPEN.date()]

    def daily_bars(self, symbol, start, end):
        return None

    def asset_flags(self, symbol):
        return True, True


class SimHarness:
    def __init__(self):
        self.clock = SimClock(OPEN + timedelta(minutes=1))
        self.broker = SimBroker(FlatSource(), self.clock, 100_000)

    def now(self):
        return self.clock.now()

    def settle(self):
        self.clock.sleep(120)


class AlpacaHarness:
    def __init__(self):
        self.trading = FakeTrading()
        self.broker = AlpacaExecution(self.trading)

    def now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    def settle(self):
        self.trading.settle(100.0)


@pytest.fixture(params=[SimHarness, AlpacaHarness], ids=["sim", "alpaca"])
def h(request):
    return request.param()


def neutral(order):
    """Every status the backend reports, the order's and its legs', is a broker-neutral one."""
    for o in (order, *order.legs):
        assert o.status in STATUSES, o.status
    return order


def test_is_an_execution_broker_on_paper(h):
    assert isinstance(h.broker, ExecutionBroker)
    assert isinstance(h.broker.capabilities, Capabilities)
    assert h.broker.is_paper() is True


def test_account(h):
    a = h.broker.get_account()
    assert isinstance(a, Account) and a.equity > 0 and a.buying_power > 0


def test_market_order_fills_into_a_position(h):
    o = neutral(h.broker.submit_market_order("SPY", "buy", 10, "c-buy"))
    assert (o.symbol, o.side, o.qty, o.client_order_id, o.order_type) == ("SPY", "buy", 10, "c-buy", "market")
    assert o.is_open
    h.settle()
    filled = neutral(h.broker.get_order(o.id))
    assert filled.status == FILLED and filled.filled_qty == 10 and filled.filled_avg_price == pytest.approx(100)
    assert not filled.is_open and filled.filled_at is not None
    [p] = h.broker.list_positions().values()
    assert isinstance(p, Position) and (p.symbol, p.qty) == ("SPY", 10)
    assert h.broker.get_order_by_client_id("c-buy").id == o.id


def test_short_position_has_negative_qty(h):
    h.broker.submit_market_order("SPY", "sell", 5, "c-short")
    h.settle()
    assert h.broker.list_positions()["SPY"].qty == -5


def test_reused_client_order_id_raises_duplicate(h):
    h.broker.submit_market_order("SPY", "buy", 1, "c-dup")
    with pytest.raises(DuplicateOrderError):
        h.broker.submit_market_order("SPY", "buy", 1, "c-dup")
    with pytest.raises(DuplicateOrderError):
        h.broker.submit_bracket_order("SPY", "long", 1, 95.0, 105.0, "c-dup")


def test_unknown_client_order_id_is_none(h):
    assert h.broker.get_order_by_client_id("never-used") is None


def test_cancel_open_order_then_cancel_again_is_quiet(h):
    o = h.broker.submit_market_order("SPY", "buy", 10, "c-cancel")
    h.broker.cancel_order(o.id)
    h.broker.cancel_order(o.id)
    h.settle()
    after = neutral(h.broker.get_order(o.id))
    assert not after.is_open and after.status != FILLED and after.filled_qty == 0
    assert h.broker.list_positions() == {}


def test_cancel_filled_order_is_quiet(h):
    o = h.broker.submit_market_order("SPY", "buy", 10, "c-filled")
    h.settle()
    h.broker.cancel_order(o.id)
    assert h.broker.get_order(o.id).status == FILLED


def test_bracket_legs_and_oco_cancel(h):
    if not h.broker.capabilities.native_brackets:
        pytest.skip("no native brackets")
    o = neutral(h.broker.submit_bracket_order("SPY", "long", 10, 95.0, 105.0, "c-bracket"))
    legs = {leg.order_type: leg for leg in o.legs}
    assert set(legs) == {"limit", "stop"}
    assert legs["limit"].limit_price == 105.0 and legs["stop"].stop_price == 95.0
    assert all(leg.side == "sell" and leg.qty == 10 for leg in o.legs)
    h.settle()
    o = neutral(h.broker.get_order(o.id))
    assert o.status == FILLED and all(leg.is_open for leg in o.legs)
    assert h.broker.list_positions()["SPY"].qty == 10
    stop = next(leg for leg in o.legs if leg.order_type == "stop")
    tp = next(leg for leg in o.legs if leg.order_type == "limit")
    h.broker.cancel_order(stop.id)
    h.broker.cancel_order(tp.id)  # usually already canceled with its sibling: must be quiet
    o = neutral(h.broker.get_order(o.id))
    assert not any(leg.is_open for leg in o.legs)
    assert neutral(h.broker.get_order(stop.id)).id == stop.id  # legs can be fetched by id


def test_short_bracket_legs_buy_back(h):
    if not h.broker.capabilities.native_brackets:
        pytest.skip("no native brackets")
    o = h.broker.submit_bracket_order("SPY", "short", 10, 105.0, 95.0, "c-short-bracket")
    assert o.side == "sell" and all(leg.side == "buy" for leg in o.legs)


def test_list_orders_returns_top_level_orders_with_legs(h):
    start = h.now() - timedelta(hours=1)
    m = h.broker.submit_market_order("SPY", "buy", 1, "c-list-1")
    b = h.broker.submit_bracket_order("SPY", "long", 1, 95.0, 105.0, "c-list-2")
    orders = {o.id: neutral(o) for o in h.broker.list_orders(start)}
    assert set(orders) == {m.id, b.id}
    assert len(orders[b.id].legs) == 2 and orders[m.id].legs == ()


def test_close_all_cancels_orders_and_flattens(h):
    h.broker.submit_market_order("SPY", "buy", 10, "c-hold")
    h.settle()
    working = h.broker.submit_bracket_order("QQQ", "long", 5, 95.0, 105.0, "c-working")
    start = h.now() - timedelta(hours=1)
    h.broker.close_all()
    h.settle()
    assert h.broker.list_positions() == {}
    assert not neutral(h.broker.get_order(working.id)).is_open
    assert not any(x.is_open for o in h.broker.list_orders(start) for x in (o, *o.legs))
