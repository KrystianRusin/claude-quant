"""Position sizing and daily limits."""
import math

MAX_ORDERS_PER_DAY = 60
MIN_STOP_PCT = 0.05
MAX_STOP_PCT = 5.0


def position_size(equity, entry, stop, risk_per_trade_pct, max_position_pct, buying_power=None):
    """Whole shares risking `risk_per_trade_pct` of equity, capped by notional and buying power."""
    r_per_share = abs(entry - stop)
    if equity <= 0 or entry <= 0 or r_per_share <= 0:
        return 0
    qty = math.floor(equity * risk_per_trade_pct / 100 / r_per_share)
    notional_cap = equity * max_position_pct / 100
    if buying_power is not None:
        notional_cap = min(notional_cap, buying_power)
    return max(0, min(qty, math.floor(notional_cap / entry)))


def daily_pnl_pct(equity, start_equity):
    return (equity - start_equity) / start_equity * 100


def daily_loss_breached(equity, start_equity, max_daily_loss_pct):
    """True when realized plus unrealized P&L for the day is at or below -max_daily_loss_pct."""
    return daily_pnl_pct(equity, start_equity) <= -max_daily_loss_pct


def entry_block_reason(trades_today, orders_today, max_trades_per_day, halted):
    """Why no new entry may be placed, or None if entries are allowed."""
    if halted:
        return "halted"
    if orders_today >= MAX_ORDERS_PER_DAY:
        return "order cap"
    if trades_today >= max_trades_per_day:
        return "max trades"
    return None


def short_allowed(shortable, easy_to_borrow):
    return bool(shortable) and bool(easy_to_borrow)


def level_error(side, price, stop, take_profit):
    """Why a signal's stop/target are unacceptable, or None if they are fine."""
    if price <= 0:
        return "bad price"
    direction = 1 if side == "long" else -1
    if (price - stop) * direction <= 0:
        return "stop on wrong side of price"
    if (take_profit - price) * direction <= 0:
        return "take profit on wrong side of price"
    stop_pct = abs(price - stop) / price * 100
    if not MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT:
        return f"stop distance {stop_pct:.2f}% outside [{MIN_STOP_PCT}, {MAX_STOP_PCT}]%"
    return None
