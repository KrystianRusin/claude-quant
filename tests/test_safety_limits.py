"""Hard safety limits: paper-only endpoint, order cap, and the ceilings in config_bounds.json."""
import pytest

import risk
from broker import AlpacaBroker, LiveEndpointError, assert_paper_url


def test_live_endpoint_refused():
    with pytest.raises(LiveEndpointError):
        assert_paper_url("https://api.alpaca.markets")
    assert_paper_url("https://paper-api.alpaca.markets")


def test_alpaca_client_is_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    assert AlpacaBroker().base_url == "https://paper-api.alpaca.markets"


def test_order_cap():
    assert 0 < risk.MAX_ORDERS_PER_DAY <= 60


def test_stop_distance_limits():
    assert 0 < risk.MIN_STOP_PCT < risk.MAX_STOP_PCT <= 5.0


def test_bounds_ceilings(bounds):
    r = bounds["risk"]
    assert r["risk_per_trade_pct"]["max"] <= 1.0
    assert r["max_daily_loss_pct"]["max"] <= 3.0
    assert r["max_position_pct_of_equity"]["max"] <= 25
    assert r["max_trades_per_day"]["max"] <= 12
    assert 1.0 <= bounds["strategy_params"]["take_profit_r"]["min"]
    assert bounds["strategy_params"]["take_profit_r"]["max"] <= 4.0
    assert bounds["watchlist_rules"]["max_symbols"] <= 25
    assert bounds["watchlist_rules"]["max_adds_per_review"] <= 3
    assert bounds["watchlist_rules"]["max_removes_per_review"] <= 3
    assert bounds["universe"]["min_price"] >= risk.MIN_PRICE >= 5
    assert bounds["universe"]["min_avg_dollar_volume"] >= 100_000_000
    assert "TQQQ" in bounds["universe"]["exclude_symbols"]
    assert "leveraged" in bounds["universe"]["exclude_name_patterns"]
    assert bounds["data_feed"]["allowed"] == ["iex"]
    assert bounds["strategy"]["force_exit_time"]["max"] <= "15:50"


def test_position_size_never_exceeds_caps():
    for stop in (99.99, 99.9, 99, 95, 80):
        qty = risk.position_size(100_000, 100, stop, 1.0, 25)
        assert qty * 100 <= 25_000
        assert qty * (100 - stop) <= 1_000 + 1e-6
