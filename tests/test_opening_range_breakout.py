from datetime import timedelta

import pytest

from helpers import OPEN, flat_range, make_bars
from strategies import SessionState, load
from strategies.opening_range_breakout import (OpeningRange, find_signal, generate_signals,
                                               opening_range, session_vwap, stop_and_target)

WINDOW_END = OPEN.replace(hour=11, minute=0)


@pytest.fixture
def s(cfg):
    return cfg["strategy"]


def test_opening_range_high_low():
    bars = make_bars(flat_range() + [(100, 105, 95, 100)])
    rng = opening_range(bars, OPEN, 15)
    assert (rng.high, rng.low) == (100.5, 99.5)
    assert rng.pct == pytest.approx(1.0)


def test_opening_range_needs_enough_bars():
    assert opening_range(make_bars(flat_range(5)), OPEN, 15) is None


def test_session_vwap_is_volume_weighted():
    bars = make_bars([(10, 10, 10, 10, 100, 10), (20, 20, 20, 20, 300, 20)])
    assert list(session_vwap(bars)) == [10, pytest.approx(17.5)]


def test_long_breakout(s):
    bars = make_bars(flat_range() + [(100.4, 100.6, 100.3, 100.5), (100.5, 101.2, 100.5, 101.0)])
    sig = find_signal("SPY", bars, OPEN, s, WINDOW_END)
    assert sig.side == "long"
    assert sig.bar_time == OPEN + timedelta(minutes=16)
    assert sig.price == 101.0
    assert sig.stop == pytest.approx(99.5)
    assert sig.take_profit == pytest.approx(101.0 + 2 * 1.5)


def test_short_breakout_and_allow_shorts(s):
    bars = make_bars(flat_range() + [(99.6, 99.6, 99.0, 99.2)])
    sig = find_signal("SPY", bars, OPEN, s, WINDOW_END)
    assert sig.side == "short"
    assert sig.stop == pytest.approx(100.5)
    assert sig.take_profit == pytest.approx(99.2 - 2 * 1.3)

    s["allow_shorts"] = False
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END) is None


def test_vwap_filter_blocks_long_below_vwap(s):
    spike = (100.5, 125, 100.5, 101.0, 1_000_000, 120.0)
    bars = make_bars(flat_range() + [spike])
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END) is None

    s["require_above_vwap_for_long"] = False
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END).side == "long"


def test_vwap_filter_mirrors_for_shorts(s):
    dump = (99.5, 99.5, 80, 99.0, 1_000_000, 85.0)
    bars = make_bars(flat_range() + [dump])
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END) is None


def test_range_pct_filter(s):
    tight = make_bars(flat_range(low=99.95, high=100.05) + [(100, 101, 100, 101)])
    assert find_signal("SPY", tight, OPEN, s, WINDOW_END) is None
    wide = make_bars(flat_range(low=97, high=103) + [(103, 104, 103, 104)])
    assert find_signal("SPY", wide, OPEN, s, WINDOW_END) is None


def test_bars_inside_range_ignored_and_window_end_respected(s):
    inside = [(100, 100.4, 99.6, 100)] * 30
    breakout = [(100.5, 101.2, 100.5, 101.0)]
    bars = make_bars(flat_range() + inside + breakout)
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END).bar_time == OPEN + timedelta(minutes=45)
    early_end = OPEN + timedelta(minutes=45)
    assert find_signal("SPY", bars, OPEN, s, early_end) is None


def test_first_breakout_only(s):
    bars = make_bars(flat_range() + [(100.5, 101.2, 100.5, 101.0), (99.6, 99.6, 98.0, 98.5)])
    assert find_signal("SPY", bars, OPEN, s, WINDOW_END).side == "long"


def test_stop_loss_r_scales_stop():
    rng = OpeningRange(high=101, low=99)
    stop, tp = stop_and_target("long", 102, rng, 0.5, 2.0)
    assert (stop, tp) == (pytest.approx(100.5), pytest.approx(105))
    stop, tp = stop_and_target("short", 98, rng, 1.0, 3.0)
    assert (stop, tp) == (pytest.approx(101), pytest.approx(89))


def test_generate_signals_one_entry_per_ticker_and_freshness(s):
    bars = make_bars(flat_range() + [(100.5, 101.2, 100.5, 101.0)])
    by_symbol = {"SPY": bars, "QQQ": bars.copy()}
    now = OPEN + timedelta(minutes=17, seconds=30)

    def state(at, entered=()):
        return SessionState(OPEN, at, WINDOW_END, frozenset(entered))

    sigs = generate_signals(by_symbol, state(now), s)
    assert [x.symbol for x in sigs] == ["QQQ", "SPY"]
    assert sigs[0].range_pct == pytest.approx(1.0)

    assert [x.symbol for x in generate_signals(by_symbol, state(now, {"SPY"}), s)] == ["QQQ"]
    assert generate_signals(by_symbol, state(now + timedelta(minutes=10)), s) == []


def test_registered():
    module = load("opening_range_breakout")
    assert module.generate_signals is generate_signals
