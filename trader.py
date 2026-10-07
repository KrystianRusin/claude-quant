"""Run one trading session of the active strategy.

python trader.py                     live paper trading for today (logs to data/)
python trader.py --dry-run           live data, simulated fills (logs to data/dry_run/)
python trader.py --replay DATE ...   simulate past days from historical bars (logs to data/replay/)
python trader.py --replay-last 10    simulate the last 10 completed trading days
python trader.py --flatten           cancel all open orders and close all positions now
"""
import argparse
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import risk
import strategies
from broker import ET, DuplicateOrderError, make_broker
from config import ConfigError, load_config, parse_hhmm
from logger import TradeLog, setup_logging, trade_row
from simbroker import RealClock, ReplaySource, SimBroker, SimClock, completed

ROOT = Path(__file__).resolve().parent
HALT_PATH = ROOT / "HALT"
LIVE_DATA = ROOT / "data"
POLL_SECONDS = 30
REPLAY_POLL_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10
EXIT_WAIT_SECONDS = 180
FINAL_STATUSES = {"filled", "partially_filled", "canceled", "rejected", "expired"}


@dataclass
class Trade:
    """One position the session is responsible for, from entry order to exit fill."""
    symbol: str
    side: str
    qty: float
    stop: float | None = None
    take_profit: float | None = None
    range_pct: float = math.nan
    entry_cid: str = ""
    entry_order_id: str | None = None
    status: str = "pending"  # pending, open, closed or void
    entry_price: float | None = None
    entry_time: datetime | None = None
    exit_order_id: str | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    carryover: bool = False

    @property
    def exit_side(self):
        return "sell" if self.side == "long" else "buy"


def entry_cid(day, symbol):
    return f"pt-{day:%Y%m%d}-{symbol}"


def carryover_cid(day, symbol):
    return f"{entry_cid(day, symbol)}-c"


def at(day, hhmm):
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=ET)


def _num(v, default=math.nan):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Session:
    def __init__(self, cfg, broker, clock, log, logger, session_open, session_close, mode,
                 poll_seconds=POLL_SECONDS, halt_path=HALT_PATH):
        self.cfg = cfg
        self.strategy = strategies.load(cfg["strategy"]["name"])
        self.broker = broker
        self.clock = clock
        self.log = log
        self.logger = logger
        self.mode = mode
        self.poll = poll_seconds
        self.halt_path = halt_path
        self.day = session_open.date()
        self.open = session_open
        self.close = session_close
        s = cfg["strategy"]
        self.force_exit = min(at(self.day, s["force_exit_time"]), session_close - timedelta(minutes=15))
        self.entry_end = min(at(self.day, s["entry_window_end"]), self.force_exit)
        self.trades = {}
        self.carry = []
        self.entered = set()
        self.orders_today = 0
        self.halted = None
        self.done = False
        self.errors = 0
        self.statuses = {}
        self.notes = []
        self.blocked_logged = set()
        self.equity_start = None

    # helpers

    def all_trades(self):
        return [*self.trades.values(), *self.carry]

    def active(self):
        return [t for t in self.all_trades() if t.status in ("pending", "open")]

    def trades_taken(self):
        return sum(1 for t in self.trades.values() if t.status != "void")

    def halt(self, reason):
        if not self.halted:
            self.halted = reason
            self.notes.append(f"halted: {reason}")
            self.logger.warning("halting for the day: %s", reason)

    def event(self, event, symbol, **fields):
        self.log.order_event(self.clock.now(), event, symbol, **fields)

    def _track(self, order):
        for o in (order, *order.legs):
            if o.status in FINAL_STATUSES and self.statuses.get(o.id) != o.status:
                self.statuses[o.id] = o.status
                self.event(o.status, o.symbol, side=o.side, qty=o.filled_qty or o.qty,
                           order_type=o.order_type, price=o.filled_avg_price or "",
                           order_id=o.id, client_order_id=o.client_order_id, status=o.status)

    # startup

    def recover(self):
        """Rebuild today's state from the broker and orders.csv so a restart never double enters."""
        prefix = f"pt-{self.day:%Y%m%d}-"
        rows = self.log.order_rows(self.day)
        submits = {r["client_order_id"]: r for r in rows if r["event"] == "submit" and r["client_order_id"]}
        for r in rows:
            if r["order_id"] and r["status"] in FINAL_STATUSES:
                self.statuses[r["order_id"]] = r["status"]
            if r["event"] == "skip":
                self.entered.add(r["symbol"])
        ours = [o for o in self.broker.list_orders(self.open - timedelta(hours=12))
                if o.client_order_id.startswith(prefix)]
        self.orders_today = len(ours)
        for o in ours:
            cid = o.client_order_id
            if cid.endswith(("-x", "-c")):
                continue
            legs = {leg.order_type: leg for leg in o.legs}
            self.trades[o.symbol] = Trade(
                o.symbol, "long" if o.side == "buy" else "short", o.qty,
                stop=getattr(legs.get("stop"), "stop_price", None),
                take_profit=getattr(legs.get("limit"), "limit_price", None),
                range_pct=_num(submits.get(cid, {}).get("range_pct")), entry_cid=cid, entry_order_id=o.id)
            self.entered.add(o.symbol)
        for o in ours:
            row = submits.get(o.client_order_id, {})
            if o.client_order_id.endswith("-x") and o.symbol in self.trades:
                t = self.trades[o.symbol]
                t.exit_order_id, t.exit_reason = o.id, row.get("reason") or "time"
            elif o.client_order_id.endswith("-c"):
                self.carry.append(Trade(o.symbol, row.get("side") or ("long" if o.side == "sell" else "short"),
                                        o.qty, entry_price=_num(row.get("price"), None), status="open",
                                        entry_cid=o.client_order_id, exit_order_id=o.id,
                                        exit_reason="carryover", carryover=True))
        if ours:
            self.logger.info("recovered %d orders for %s: entered %s", len(ours), self.day, sorted(self.entered))

    def handle_carryover(self):
        """Cancel orders and flatten positions left over from an earlier day."""
        today_start = datetime.combine(self.day, datetime.min.time(), tzinfo=ET)
        for o in self.broker.list_orders(today_start - timedelta(days=10)):
            for x in (o, *o.legs):
                if x.is_open and x.created_at and x.created_at < today_start:
                    self.broker.cancel_order(x.id)
                    self.event("cancel", x.symbol, order_id=x.id, client_order_id=x.client_order_id,
                               reason="carryover")
        known = {t.symbol for t in self.all_trades()}
        for symbol, p in self.broker.list_positions().items():
            if symbol in known:
                continue
            side = "long" if p.qty > 0 else "short"
            t = Trade(symbol, side, abs(p.qty), entry_price=p.avg_entry_price, status="open",
                      entry_cid=carryover_cid(self.day, symbol), exit_reason="carryover", carryover=True)
            self.logger.warning("carryover position %s %s %s: flattening", symbol, side, abs(p.qty))
            if self._submit_exit(t, "carryover", t.entry_cid):
                self.carry.append(t)
                self.notes.append(f"carryover {symbol}")

    # order handling

    def _submit_exit(self, t, reason, cid):
        try:
            order = self.broker.submit_market_order(t.symbol, t.exit_side, t.qty, cid)
        except DuplicateOrderError:
            order = self.broker.get_order_by_client_id(cid)
        except Exception:
            self.logger.exception("exit order for %s failed", t.symbol)
            return False
        self.orders_today += 1
        t.exit_order_id, t.exit_reason = order.id, reason
        self.event("submit", t.symbol, side=t.side, qty=t.qty, order_type="market",
                   price=t.entry_price if t.carryover else "", order_id=order.id,
                   client_order_id=cid, status=order.status, reason=reason)
        return True

    def _close(self, t, price, when, reason):
        t.status, t.exit_price, t.exit_time, t.exit_reason = "closed", price, when, reason
        row = trade_row(day=self.day, symbol=t.symbol, side=t.side, qty=t.qty, entry_time=t.entry_time,
                        entry_price=t.entry_price, exit_time=when, exit_price=price, exit_reason=reason,
                        stop=t.stop, range_pct=t.range_pct, session_open=self.open,
                        config_version=self.cfg["version"], strategy=self.cfg["strategy"]["name"],
                        strategy_version=self.cfg["strategy_version"])
        self.log.trade(row)
        self.logger.info("closed %s %s %s @ %.2f (%s) pnl $%s", t.side, t.qty, t.symbol, price, reason, row["pnl_usd"])

    def sync(self):
        """Pull order states from the broker; record entry fills and closed trades."""
        for t in self.active():
            order = None
            if t.entry_order_id:
                order = self.broker.get_order(t.entry_order_id)
                self._track(order)
                if t.status == "pending":
                    if order.status == "filled" or (order.filled_qty > 0 and not order.is_open):
                        t.status, t.qty = "open", order.filled_qty
                        t.entry_price, t.entry_time = order.filled_avg_price, order.filled_at
                        if order.status != "filled":
                            self.logger.warning("%s entry only partly filled (%s)", t.symbol, t.qty)
                    elif not order.is_open:
                        t.status = "void"
                if t.status == "open":
                    for leg in order.legs:
                        if leg.status == "filled":
                            self._close(t, leg.filled_avg_price, leg.filled_at,
                                        "tp" if leg.order_type == "limit" else "sl")
                            break
            if t.status == "open" and t.exit_order_id:
                x = self.broker.get_order(t.exit_order_id)
                self._track(x)
                if x.status == "filled":
                    if t.carryover:
                        t.entry_price = t.entry_price if t.entry_price is not None else x.filled_avg_price
                    self._close(t, x.filled_avg_price, x.filled_at, t.exit_reason)
                elif not x.is_open:
                    self.logger.warning("exit order for %s ended %s; will retry", t.symbol, x.status)
                    t.exit_order_id = None

    def flatten(self, t, reason):
        if t.status == "pending" and t.entry_order_id:
            try:
                self.broker.cancel_order(t.entry_order_id)
            except Exception:
                self.logger.exception("cancel of %s entry failed", t.symbol)
            return
        if t.status != "open" or t.exit_order_id:
            return
        if t.entry_order_id:
            order = self.broker.get_order(t.entry_order_id)
            for leg in order.legs:
                if leg.is_open:
                    try:
                        self.broker.cancel_order(leg.id)
                    except Exception:
                        self.logger.exception("cancel of %s leg failed", t.symbol)
            for _ in range(10):
                order = self.broker.get_order(t.entry_order_id)
                if not any(leg.is_open for leg in order.legs):
                    break
                self.clock.sleep(1)
            if any(leg.status == "filled" for leg in order.legs):
                return
        self._submit_exit(t, reason, f"{t.entry_cid}-x")

    def flatten_all(self, reason):
        for t in self.active():
            self.flatten(t, reason)

    # entries

    def enter_signals(self, now, account):
        bars = self.broker.get_bars(self.cfg["watchlist"], self.open, now)
        bars = {s: completed(f, now) for s, f in bars.items()}
        state = strategies.SessionState(self.open, now, self.entry_end, frozenset(self.entered))
        for sig in self.strategy.generate_signals(bars, state, self.cfg["strategy"]):
            if sig.symbol in self.cfg["watchlist"] and sig.symbol not in self.entered:
                self.try_enter(sig, account)

    def try_enter(self, sig, account):
        r = self.cfg["risk"]
        block = risk.entry_block_reason(self.trades_taken(), self.orders_today, r["max_trades_per_day"],
                                        bool(self.halted))
        if block:
            if block not in self.blocked_logged:
                self.blocked_logged.add(block)
                self.logger.info("no more entries today: %s", block)
            return
        self.entered.add(sig.symbol)
        problem = risk.level_error(sig.side, sig.price, sig.stop, sig.take_profit)
        if not problem and sig.side == "short":
            if not account.shorting_enabled:
                problem = "shorting disabled on account"
            elif not risk.short_allowed(*self.broker.asset_flags(sig.symbol)):
                problem = "not shortable or not easy to borrow"
        qty = 0
        if not problem:
            qty = risk.position_size(account.equity, sig.price, sig.stop, r["risk_per_trade_pct"],
                                     r["max_position_pct_of_equity"], account.buying_power)
            problem = None if qty > 0 else "size rounds to zero"
        if problem:
            self.event("skip", sig.symbol, side=sig.side, price=sig.price, reason=problem)
            return
        stop, tp, cid = round(sig.stop, 2), round(sig.take_profit, 2), entry_cid(self.day, sig.symbol)
        try:
            order = self.broker.submit_bracket_order(sig.symbol, sig.side, qty, stop, tp, cid)
        except DuplicateOrderError:
            order = self.broker.get_order_by_client_id(cid)
        except Exception as e:
            self.orders_today += 1
            self.logger.exception("entry for %s failed", sig.symbol)
            self.event("reject", sig.symbol, side=sig.side, qty=qty, client_order_id=cid, reason=str(e)[:200])
            return
        self.orders_today += 1
        self.trades[sig.symbol] = Trade(sig.symbol, sig.side, qty, stop, tp, sig.range_pct, cid, order.id)
        self.event("submit", sig.symbol, side=sig.side, qty=qty, order_type="bracket", price=sig.price,
                   stop_price=stop, take_profit=tp, range_pct=round(sig.range_pct, 4),
                   order_id=order.id, client_order_id=cid, status=order.status)
        self.logger.info("entered %s %s %s ~%.2f stop %.2f target %.2f", sig.side, qty, sig.symbol,
                         sig.price, stop, tp)

    # main loop

    def tick(self):
        now = self.clock.now()
        self.sync()
        if self.halt_path and self.halt_path.exists():
            self.halt("HALT file present")
        account = self.broker.get_account()
        if risk.daily_loss_breached(account.equity, self.equity_start, self.cfg["risk"]["max_daily_loss_pct"]):
            self.halt(f"daily loss limit ({risk.daily_pnl_pct(account.equity, self.equity_start):.2f}%)")
        if self.orders_today >= risk.MAX_ORDERS_PER_DAY:
            self.halt("order cap reached")
        if self.halted or now >= self.force_exit:
            self.flatten_all("halt" if self.halted else "time")
            self.done = not self.active()
        elif now < self.entry_end:
            if now >= self.open:
                self.enter_signals(now, account)
        elif not self.active():
            self.done = True

    def run(self):
        self.logger.info("%s session %s: strategy %s v%s, config v%s, entries until %s, exit at %s",
                         self.mode, self.day, self.cfg["strategy"]["name"], self.cfg["strategy_version"],
                         self.cfg["version"], f"{self.entry_end:%H:%M}", f"{self.force_exit:%H:%M}")
        try:
            self.equity_start = self.broker.get_account().last_equity
            self.recover()
            self.handle_carryover()
            self.clock.sleep_until(self.open)
            while not self.done:
                try:
                    self.tick()
                    self.errors = 0
                except Exception:
                    self.errors += 1
                    self.logger.exception("error in session loop (%d in a row)", self.errors)
                    if self.errors >= MAX_CONSECUTIVE_ERRORS:
                        self.halt("repeated errors")
                    if self.errors >= 3 * MAX_CONSECUTIVE_ERRORS:
                        break
                if not self.done:
                    self.clock.sleep(self.poll)
        finally:
            self.finalize()
        return self

    def finalize(self):
        """Flatten anything still open, make sure the account is flat, write the daily summary."""
        reason = "halt" if self.halted else "time"
        try:
            deadline = self.clock.now() + timedelta(seconds=EXIT_WAIT_SECONDS)
            while True:
                self.sync()
                self.flatten_all(reason)
                if not self.active() or self.clock.now() >= deadline:
                    break
                self.clock.sleep(5)
            leftovers = self.broker.list_positions()
            if leftovers:
                self.logger.error("positions still open after exit: %s; closing all", sorted(leftovers))
                self.notes.append(f"close_all safety net: {sorted(leftovers)}")
                self.broker.close_all()
        except Exception:
            self.logger.exception("finalize failed; check the account for open positions")
            self.notes.append("finalize error")
        self.write_summary()

    def write_summary(self):
        rows = self.log.trades(self.day)
        pnl = [float(r["pnl_usd"]) for r in rows]
        try:
            equity_end = round(self.broker.get_account().equity, 2)
        except Exception:
            self.logger.exception("could not read equity for summary")
            equity_end = ""
        self.log.daily_summary({
            "date": self.day.isoformat(), "trades": len(rows), "wins": sum(p > 0 for p in pnl),
            "losses": sum(p < 0 for p in pnl), "gross_pnl": round(sum(pnl), 2),
            "equity_start": round(self.equity_start, 2) if self.equity_start else "",
            "equity_end": equity_end, "halted": bool(self.halted),
            "notes": "; ".join([self.mode, *self.notes]),
        })
        self.logger.info("summary %s: %d trades, gross P&L $%.2f%s", self.day, len(rows), sum(pnl),
                         f", halted ({self.halted})" if self.halted else "")


class InstanceLock:
    """Exclusive OS-level lock on a file; released automatically if the process dies."""

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError(f"another instance holds {self.path}; refusing to start")
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.file.close()


def run_day(cfg, broker, clock, log, logger, day, mode, poll, halt_path=HALT_PATH):
    times = broker.session(day)
    if times is None:
        logger.info("market closed on %s; nothing to do", day)
        return None
    if mode != "replay" and clock.now() >= times[1]:
        logger.info("session for %s is already over; nothing to do", day)
        return None
    return Session(cfg, broker, clock, log, logger, *times, mode=mode, poll_seconds=poll,
                   halt_path=halt_path).run()


def run_live(args):
    cfg = load_config()
    logger = setup_logging(LIVE_DATA)
    with InstanceLock(LIVE_DATA / "trader.lock"):
        broker = make_broker(cfg["data_feed"])
        broker.get_account()
        clock = RealClock()
        run_day(cfg, broker, clock, TradeLog(LIVE_DATA, "live"), logger, clock.now().date(), "live", POLL_SECONDS)
    return 0


def run_dry(args):
    cfg = load_config(args.config)
    data_dir = Path(args.data_dir or LIVE_DATA / "dry_run")
    logger = setup_logging(data_dir)
    with InstanceLock(data_dir / "trader.lock"):
        real = make_broker(cfg["data_feed"])
        clock = RealClock()
        sim = SimBroker(real, clock, starting_equity=real.get_account().equity)
        run_day(cfg, sim, clock, TradeLog(data_dir, "dry-run"), logger, clock.now().date(), "dry-run", POLL_SECONDS)
    return 0


def run_replay(args):
    cfg = load_config(args.config)
    data_dir = Path(args.data_dir or LIVE_DATA / "replay")
    if data_dir.resolve() == LIVE_DATA.resolve():
        raise SystemExit("replay must not write into the live data directory")
    logger = setup_logging(data_dir)
    log = TradeLog(data_dir, "replay")
    log.clear()
    real = make_broker(cfg["data_feed"])
    if args.replay:
        days = [datetime.strptime(d, "%Y-%m-%d").date() for d in args.replay]
    else:
        now = datetime.now(ET)
        last = now.date()
        times = real.session(last)
        if times is None or now < times[1]:
            last -= timedelta(days=1)
        days = real.trading_days(last, args.replay_last)
    equity, failures = args.equity, 0
    for day in days:
        times = real.session(day)
        if times is None:
            logger.info("market closed on %s; skipping", day)
            continue
        clock = SimClock(times[0] - timedelta(minutes=5))
        sim = SimBroker(ReplaySource(real, cfg["watchlist"], *times), clock, starting_equity=equity)
        session = Session(cfg, sim, clock, log, logger, *times, mode="replay",
                          poll_seconds=REPLAY_POLL_SECONDS, halt_path=None).run()
        failures += session.errors > 0 or any("error" in n for n in session.notes)
        equity = sim.get_account().equity
    rows = log.trades()
    pnl = sum(float(r["pnl_usd"]) for r in rows)
    print(f"replayed {len(days)} days: {len(rows)} trades, net P&L ${pnl:,.2f}, "
          f"end equity ${equity:,.2f}, days with errors: {failures}")
    print(f"details: {data_dir}  (python analyze.py --data-dir {data_dir} --no-baseline --out '')")
    return 1 if failures else 0


def run_flatten(args):
    broker = make_broker()
    positions = broker.list_positions()
    broker.close_all()
    print(f"canceled open orders and closed positions: {sorted(positions) or 'none'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--replay", nargs="+", metavar="YYYY-MM-DD")
    mode.add_argument("--replay-last", type=int, metavar="N")
    mode.add_argument("--flatten", action="store_true")
    ap.add_argument("--config", default=ROOT / "config.json",
                    help="config file for --dry-run/--replay (live always uses config.json)")
    ap.add_argument("--data-dir", help="output directory for --dry-run/--replay")
    ap.add_argument("--equity", type=float, default=100_000.0, help="starting equity for --replay")
    args = ap.parse_args(argv)
    live = not (args.dry_run or args.replay or args.replay_last or args.flatten)
    if live and (Path(args.config).resolve() != (ROOT / "config.json").resolve() or args.data_dir):
        ap.error("--config and --data-dir are only allowed with --dry-run or --replay")
    try:
        if args.flatten:
            return run_flatten(args)
        if args.dry_run:
            return run_dry(args)
        if args.replay or args.replay_last:
            return run_replay(args)
        return run_live(args)
    except ConfigError as e:
        print(e)
        return 2
    except RuntimeError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
