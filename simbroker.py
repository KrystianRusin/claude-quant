"""Simulated broker and clocks for --dry-run and --replay.

SimBroker implements the Broker interface on top of any bar source, filling
orders from 1-minute bars:
- market orders fill at the open of the bar containing the submit time;
- bracket legs are checked on every bar after the entry fills, including the
  fill bar; if one bar touches both stop and target, the stop wins;
- a bar that opens beyond a stop or target fills at the open.
Fills are optimistic compared with real markets: no slippage, spread or partial fills.
"""
import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from broker import ET, Account, Broker, DuplicateOrderError, Order, Position

BAR = timedelta(minutes=1)


class RealClock:
    def now(self):
        return datetime.now(ET)

    def sleep(self, seconds):
        time.sleep(seconds)

    def sleep_until(self, t):
        while (delta := (t - self.now()).total_seconds()) > 0:
            time.sleep(min(delta, 60))


class SimClock:
    def __init__(self, start):
        self._now = start

    def now(self):
        return self._now

    def sleep(self, seconds):
        self._now += timedelta(seconds=seconds)

    def sleep_until(self, t):
        self._now = max(self._now, t)


def completed(frame, now):
    """Bars that have fully closed by `now`."""
    return frame[frame.index + BAR <= now]


class ReplaySource:
    """One day's bars fetched once from a real broker, served in slices."""

    def __init__(self, broker, symbols, session_open, session_close):
        self.broker = broker
        self.bars = broker.get_bars(symbols, session_open, session_close)

    def get_bars(self, symbols, start, end):
        return {s: f[(f.index >= start) & (f.index < end)]
                for s, f in self.bars.items() if s in symbols}

    def session(self, day):
        return self.broker.session(day)

    def trading_days(self, end, count):
        return self.broker.trading_days(end, count)

    def asset_flags(self, symbol):
        return True, True


@dataclass
class _SimOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    order_type: str
    status: str
    created_at: datetime
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: datetime | None = None
    legs: list = field(default_factory=list)

    def snapshot(self):
        return Order(self.id, self.client_order_id, self.symbol, self.side, self.qty, self.order_type,
                     self.status, self.filled_qty, self.filled_avg_price, self.filled_at, self.created_at,
                     self.limit_price, self.stop_price, tuple(leg.snapshot() for leg in self.legs))


class SimBroker(Broker):
    def __init__(self, source, clock, starting_equity=100_000.0):
        self.source = source
        self.clock = clock
        self.start_equity = float(starting_equity)
        self.cash = float(starting_equity)
        self.positions = {}  # symbol -> [signed qty, avg price]
        self.orders = []  # top-level orders in submit order
        self.by_cid = {}
        self.marks = {}
        self.last_bar = {}  # symbol -> start of the last processed bar
        self._ids = itertools.count(1)
        self._synced_at = None

    # data and calendar

    def session(self, day):
        return self.source.session(day)

    def trading_days(self, end, count):
        return self.source.trading_days(end, count)

    def asset_flags(self, symbol):
        return self.source.asset_flags(symbol)

    def is_market_open(self):
        now = self.clock.now()
        s = self.session(now.date())
        return bool(s) and s[0] <= now < s[1]

    def get_bars(self, symbols, start, end):
        now = self.clock.now()
        frames = self.source.get_bars(symbols, start, min(end, now))
        return {s: completed(f, now) for s, f in frames.items()}

    # account

    def get_account(self):
        self._sync()
        equity = self.cash + sum(q * self.marks.get(s, avg) for s, (q, avg) in self.positions.items())
        return Account(equity=equity, last_equity=self.start_equity,
                       buying_power=equity * 4, shorting_enabled=True)

    def list_positions(self):
        self._sync()
        return {s: Position(s, q, avg) for s, (q, avg) in self.positions.items()}

    # orders

    def _new(self, symbol, side, qty, order_type, cid, status="new", limit=None, stop=None):
        return _SimOrder(f"sim-{next(self._ids)}", cid, symbol, side, float(qty), order_type, status,
                         self.clock.now(), limit_price=limit, stop_price=stop)

    def _register(self, order):
        if order.client_order_id in self.by_cid:
            raise DuplicateOrderError(order.client_order_id)
        self.orders.append(order)
        self.by_cid[order.client_order_id] = order
        return order.snapshot()

    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id):
        buy = side == "long"
        parent = self._new(symbol, "buy" if buy else "sell", qty, "market", client_order_id)
        exit_side = "sell" if buy else "buy"
        parent.legs = [
            self._new(symbol, exit_side, qty, "limit", f"{client_order_id}-tp", "held", limit=take_profit),
            self._new(symbol, exit_side, qty, "stop", f"{client_order_id}-sl", "held", stop=stop_price),
        ]
        mark = self.marks.get(symbol)
        wrong_side = mark is not None and ((stop_price >= mark or take_profit <= mark) if buy
                                           else (stop_price <= mark or take_profit >= mark))
        if qty <= 0 or wrong_side:
            parent.status = "rejected"
            for leg in parent.legs:
                leg.status = "canceled"
        return self._register(parent)

    def submit_market_order(self, symbol, side, qty, client_order_id):
        order = self._new(symbol, side, qty, "market", client_order_id)
        if qty <= 0:
            order.status = "rejected"
        return self._register(order)

    def _find(self, order_id):
        for o in self.orders:
            if o.id == order_id:
                return o, None
            for leg in o.legs:
                if leg.id == order_id:
                    return leg, o
        raise KeyError(order_id)

    def get_order(self, order_id):
        self._sync()
        return self._find(order_id)[0].snapshot()

    def get_order_by_client_id(self, client_order_id):
        self._sync()
        o = self.by_cid.get(client_order_id)
        return o.snapshot() if o else None

    def list_orders(self, after):
        self._sync()
        return [o.snapshot() for o in self.orders if o.created_at >= after]

    def cancel_order(self, order_id):
        order, parent = self._find(order_id)
        siblings = parent.legs if parent else [order, *order.legs]
        for o in siblings:
            if o.status in ("new", "held", "accepted"):
                o.status = "canceled"

    def close_all(self):
        self._sync()
        for o in self.orders:
            for x in (o, *o.legs):
                if x.status in ("new", "held", "accepted"):
                    x.status = "canceled"
        for symbol, (qty, _) in list(self.positions.items()):
            cid = f"close-all-{symbol}-{next(self._ids)}"
            self._register(self._new(symbol, "sell" if qty > 0 else "buy", abs(qty), "market", cid))

    # fill engine

    def _fill(self, order, price, when):
        order.status, order.filled_qty, order.filled_avg_price, order.filled_at = "filled", order.qty, price, when
        signed = order.qty if order.side == "buy" else -order.qty
        self.cash -= signed * price
        qty, avg = self.positions.get(order.symbol, [0.0, 0.0])
        new_qty = qty + signed
        if abs(new_qty) < 1e-9:
            self.positions.pop(order.symbol, None)
        elif qty == 0 or (qty > 0) != (new_qty > 0):
            self.positions[order.symbol] = [new_qty, price]
        elif abs(new_qty) > abs(qty):
            self.positions[order.symbol] = [new_qty, (qty * avg + signed * price) / new_qty]
        else:
            self.positions[order.symbol] = [new_qty, avg]

    def _check_legs(self, parent, ts, bar):
        tp, sl = parent.legs
        if sl.status != "new":
            return
        long_ = parent.side == "buy"
        o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if long_:
            gap_stop, gap_tp = o <= sl.stop_price, o >= tp.limit_price
            hit_stop, hit_tp = lo <= sl.stop_price, h >= tp.limit_price
        else:
            gap_stop, gap_tp = o >= sl.stop_price, o <= tp.limit_price
            hit_stop, hit_tp = h >= sl.stop_price, lo <= tp.limit_price
        if gap_stop:
            leg, price = sl, o
        elif gap_tp:
            leg, price = tp, o
        elif hit_stop:
            leg, price = sl, sl.stop_price
        elif hit_tp:
            leg, price = tp, tp.limit_price
        else:
            return
        self._fill(leg, price, max(ts, parent.filled_at))
        (sl if leg is tp else tp).status = "canceled"

    def _sync(self):
        """Process every completed bar since the last sync for symbols with orders or positions."""
        now = self.clock.now()
        if now == self._synced_at:
            return
        self._synced_at = now
        active = [o for o in self.orders
                  if o.status == "new" or any(leg.status in ("new", "held") for leg in o.legs)]
        symbols = {o.symbol for o in active} | set(self.positions)
        if not symbols:
            return
        floor = lambda t: t.replace(second=0, microsecond=0)  # noqa: E731
        starts = [floor(o.created_at) for o in active] + [self.last_bar[s] + BAR for s in symbols if s in self.last_bar]
        frames = self.get_bars(sorted(symbols), min(starts) if starts else now - timedelta(days=1), now)
        for symbol in sorted(symbols):
            frame = frames.get(symbol)
            if frame is None:
                continue
            last = self.last_bar.get(symbol)
            for ts, bar in (frame[frame.index > last] if last is not None else frame).iterrows():
                for order in [o for o in self.orders if o.symbol == symbol]:
                    if order.status == "new" and floor(order.created_at) <= ts:
                        self._fill(order, float(bar["open"]), max(ts, order.created_at))
                        for leg in order.legs:
                            if leg.status == "held":
                                leg.status = "new"
                    if order.legs and order.status == "filled":
                        self._check_legs(order, ts, bar)
                self.marks[symbol] = float(bar["close"])
                self.last_bar[symbol] = ts
