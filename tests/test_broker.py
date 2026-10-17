"""Alpaca translation details and backend selection. The shared behaviour is in test_broker_contract.py."""
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderStatus

import broker
from alpaca_backend import STATUS_MAP, AlpacaExecution, neutral_status
from broker import STATUSES, BrokerError, OrderRejectedError
from fake_alpaca import FakeTrading, api_error


class CancelTrading(FakeTrading):
    def __init__(self, error=None):
        super().__init__()
        self.error = error
        self.canceled = []

    def cancel_order_by_id(self, order_id):
        self.canceled.append(order_id)
        if self.error:
            raise self.error


def test_cancel_order_sends_cancel():
    trading = CancelTrading()
    AlpacaExecution(trading).cancel_order("abc")
    assert trading.canceled == ["abc"]


def test_cancel_of_order_already_pending_cancel_is_quiet():
    # Cancelling one bracket leg cancels its OCO sibling, so Alpaca rejects the second cancel.
    trading = CancelTrading(api_error(422, 42210000, "order pending cancel"))
    AlpacaExecution(trading).cancel_order("abc")
    assert trading.canceled == ["abc"]


def test_cancel_of_filled_order_is_quiet():
    trading = CancelTrading(api_error(422, 42210000, "order is already in filled state"))
    AlpacaExecution(trading).cancel_order("abc")


@pytest.mark.parametrize("status", [404, 500])
def test_other_cancel_errors_raise_broker_error(status):
    trading = CancelTrading(api_error(status, 50010000, "boom"))
    with pytest.raises(BrokerError):
        AlpacaExecution(trading).cancel_order("abc")


def test_every_alpaca_status_is_mapped():
    assert {s.value for s in OrderStatus} <= set(STATUS_MAP)
    assert {v for v in STATUS_MAP.values() if v} <= STATUSES


def test_calculated_status_depends_on_the_fill():
    assert neutral_status(OrderStatus.CALCULATED, 10, 10) == broker.FILLED
    assert neutral_status(OrderStatus.CALCULATED, 10, 4) == broker.EXPIRED


def test_unknown_status_is_treated_as_open():
    assert neutral_status("some_new_status", 10, 0) == broker.NEW


def test_rejected_submit_raises_order_rejected():
    trading = FakeTrading()
    trading.submit_order = lambda req: (_ for _ in ()).throw(api_error(403, 40310000, "insufficient buying power"))
    with pytest.raises(OrderRejectedError):
        AlpacaExecution(trading).submit_market_order("SPY", "buy", 10, "c-1")


def test_client_id_lookup_errors_other_than_404_raise():
    trading = FakeTrading()
    trading.get_order_by_client_id = lambda cid: (_ for _ in ()).throw(api_error(500, 50010000, "boom"))
    with pytest.raises(BrokerError):
        AlpacaExecution(trading).get_order_by_client_id("c-1")


def test_live_alpaca_url_refused():
    with pytest.raises(broker.LiveAccountError):
        AlpacaExecution(FakeTrading(base_url="https://api.alpaca.markets"))


@pytest.fixture
def no_dotenv(monkeypatch):
    monkeypatch.setattr(broker, "load_dotenv", lambda: None)
    for name in ("BROKER_BACKEND", "DATA_BACKEND", "ALLOW_LIVE", "IBKR_ACCOUNT", "IBKR_PORT"):
        monkeypatch.delenv(name, raising=False)


def test_unknown_backend_is_refused(no_dotenv, monkeypatch):
    monkeypatch.setenv("BROKER_BACKEND", "nope")
    with pytest.raises(RuntimeError, match="unknown BROKER_BACKEND"):
        broker.make_execution()


def test_ibkr_stub_can_be_selected(no_dotenv, monkeypatch):
    from ibkr_backend import IBKRExecution

    monkeypatch.setenv("BROKER_BACKEND", "ibkr")
    ib = broker.make_execution()
    assert isinstance(ib, IBKRExecution) and ib.is_paper() and ib.capabilities.native_brackets
    with pytest.raises(NotImplementedError):
        ib.get_account()


@pytest.mark.parametrize("env", [{"IBKR_ACCOUNT": "U1234567"}, {"IBKR_PORT": "4001"}])
def test_ibkr_live_account_needs_allow_live(no_dotenv, monkeypatch, env):
    monkeypatch.setenv("BROKER_BACKEND", "ibkr")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(broker.LiveAccountError):
        broker.make_execution()
    monkeypatch.setenv("ALLOW_LIVE", "1")
    assert not broker.make_execution().is_paper()


def test_combined_broker_routes_each_role():
    data = SimpleNamespace(session=lambda day: "from data", asset_flags=lambda s: (True, False))
    execution = AlpacaExecution(FakeTrading())
    combined = broker.CombinedBroker(execution, data)
    assert combined.session(None) == "from data" and combined.asset_flags("X") == (True, False)
    assert combined.get_account().equity == 100_000 and combined.capabilities.native_brackets


def test_broker_module_does_not_import_alpaca():
    import ast
    from pathlib import Path

    root = Path(broker.__file__).parent
    for name in ("broker.py", "trader.py", "risk.py", "simbroker.py", "analyze.py", "ibkr_backend.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in modules if m.split(".")[0] in ("alpaca", "alpaca_backend")}, name
