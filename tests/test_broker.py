import pytest
import requests
from alpaca.common.exceptions import APIError

from broker import AlpacaBroker


def api_error(status, code, message):
    response = requests.Response()
    response.status_code = status
    return APIError(f'{{"code":{code},"message":"{message}"}}', requests.HTTPError(response=response))


class FakeTrading:
    def __init__(self, error=None):
        self.error = error
        self.canceled = []

    def cancel_order_by_id(self, order_id):
        self.canceled.append(order_id)
        if self.error:
            raise self.error


def alpaca_with(trading):
    broker = AlpacaBroker.__new__(AlpacaBroker)  # skip __init__: no credentials or network
    broker.trading = trading
    return broker


def test_cancel_order_sends_cancel():
    trading = FakeTrading()
    alpaca_with(trading).cancel_order("abc")
    assert trading.canceled == ["abc"]


def test_cancel_of_order_already_pending_cancel_is_quiet():
    # Cancelling one bracket leg cancels its OCO sibling, so Alpaca rejects the second cancel.
    trading = FakeTrading(api_error(422, 42210000, "order pending cancel"))
    alpaca_with(trading).cancel_order("abc")
    assert trading.canceled == ["abc"]


def test_cancel_of_filled_order_is_quiet():
    trading = FakeTrading(api_error(422, 42210000, "order is already in filled state"))
    alpaca_with(trading).cancel_order("abc")


@pytest.mark.parametrize("status", [404, 500])
def test_other_cancel_errors_still_raise(status):
    trading = FakeTrading(api_error(status, 50010000, "boom"))
    with pytest.raises(APIError):
        alpaca_with(trading).cancel_order("abc")
