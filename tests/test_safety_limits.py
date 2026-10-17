"""Hard safety limits: paper-only Alpaca, the live-account gate, order cap, and the ceilings in config_bounds.json."""
import pytest

import broker
import risk
from alpaca_backend import AlpacaExecution, assert_paper_url
from broker import LiveAccountError


def test_live_alpaca_endpoint_refused():
    with pytest.raises(LiveAccountError):
        assert_paper_url("https://api.alpaca.markets")
    assert_paper_url("https://paper-api.alpaca.markets")


def test_alpaca_client_is_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    alpaca = AlpacaExecution()
    assert alpaca.base_url == "https://paper-api.alpaca.markets" and alpaca.is_paper()


class LiveBackend:
    def is_paper(self):
        return False


def test_live_account_refused_without_allow_live(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    with pytest.raises(LiveAccountError):
        broker.check_live(LiveBackend())
    monkeypatch.setenv("ALLOW_LIVE", "yes")  # only exactly "1" counts
    with pytest.raises(LiveAccountError):
        broker.check_live(LiveBackend())


def test_live_account_allowed_with_allow_live(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE", "1")
    backend = LiveBackend()
    assert broker.check_live(backend) is backend


def test_make_execution_applies_the_live_gate(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    monkeypatch.setattr(broker, "load_dotenv", lambda: None)  # ignore a local .env
    monkeypatch.setitem(broker.EXECUTION_BACKENDS, "fake-live", LiveBackend)
    monkeypatch.setenv("BROKER_BACKEND", "fake-live")
    with pytest.raises(LiveAccountError):
        broker.make_execution()


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
