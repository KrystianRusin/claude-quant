import pytest

from risk import (MAX_ORDERS_PER_DAY, daily_loss_breached, daily_pnl_pct,
                  entry_block_reason, level_error, position_size, short_allowed)


@pytest.mark.parametrize("stop,expected", [
    (99, 200),   # 500 shares by risk, capped at 20% notional = 200
    (98, 200),   # 250 by risk, capped at 200
    (95, 100),   # 100 by risk
    (97.5, 200),
])
def test_position_size(stop, expected):
    assert position_size(100_000, 100, stop, 0.5, 20) == expected


def test_position_size_short_side():
    assert position_size(100_000, 100, 105, 0.5, 20) == 100


def test_position_size_rounds_down_and_skips_zero():
    assert position_size(100_000, 100, 97, 0.5, 20) == 166
    assert position_size(1_000, 100, 90, 0.5, 20) == 0
    assert position_size(100_000, 100, 100, 0.5, 20) == 0


def test_position_size_buying_power_cap():
    assert position_size(100_000, 100, 95, 0.5, 20, buying_power=5_000) == 50


def test_daily_loss():
    assert daily_pnl_pct(98_500, 100_000) == pytest.approx(-1.5)
    assert daily_loss_breached(98_500, 100_000, 1.5)
    assert not daily_loss_breached(98_600, 100_000, 1.5)
    assert not daily_loss_breached(101_000, 100_000, 1.5)


def test_entry_block_reason():
    assert entry_block_reason(0, 0, 4, halted=False) is None
    assert entry_block_reason(4, 4, 4, halted=False) == "max trades"
    assert entry_block_reason(0, MAX_ORDERS_PER_DAY, 4, halted=False) == "order cap"
    assert entry_block_reason(0, 0, 4, halted=True) == "halted"


def test_short_allowed():
    assert short_allowed(True, True)
    assert not short_allowed(True, False)
    assert not short_allowed(False, True)


def test_level_error():
    assert level_error("long", 100, 99, 102) is None
    assert level_error("short", 100, 101, 98) is None
    assert "wrong side" in level_error("long", 100, 101, 102)
    assert "wrong side" in level_error("short", 100, 101, 102)
    assert "outside" in level_error("long", 100, 99.99, 102)
    assert "outside" in level_error("long", 100, 90, 120)
    assert "below" in level_error("long", 4, 3.9, 4.2)
