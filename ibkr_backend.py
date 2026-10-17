"""Interactive Brokers execution backend: a stub that can be selected but does not trade yet.

`BROKER_BACKEND=ibkr` selects it. It reads its connection settings and reports paper or live, so
the live-account gate in broker.py already applies to it, but every account and order call raises
NotImplementedError. Market data stays with DATA_BACKEND (Alpaca by default).

The real backend will talk to IB Gateway (or TWS) running on this machine, for example through the
ib_async package:
- paper accounts have ids starting "DU"; Gateway listens on 4002 for paper and 4001 for live
- brackets are native (a parent order with take-profit and stop children sharing an OCA group)
- the client_order_id goes in orderRef, and a reused orderRef must raise DuplicateOrderError
- order statuses map PendingSubmit/PreSubmitted/Submitted -> new, Filled -> filled,
  Cancelled/ApiCancelled -> canceled, Inactive -> rejected, PendingCancel -> pending_cancel
It has to pass tests/test_broker_contract.py before it trades.

Settings, from the environment or .env:
  IBKR_HOST        default 127.0.0.1
  IBKR_PORT        default 4002 (IB Gateway paper)
  IBKR_CLIENT_ID   default 17 (the sentiment trader uses 18; each connection needs its own)
  IBKR_ACCOUNT     the account id (DU... for paper); when set it decides paper vs live
"""
import os

import broker as b

PAPER_PORTS = {4002, 7497}  # IB Gateway and TWS paper ports; 4001 and 7496 are live
STUB = "the IBKR backend is a stub and cannot {} yet; use BROKER_BACKEND=alpaca"


class IBKRExecution(b.ExecutionBroker):
    capabilities = b.Capabilities(native_brackets=True)

    def __init__(self):
        self.host = os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = int(os.environ.get("IBKR_PORT", "4002"))
        self.client_id = int(os.environ.get("IBKR_CLIENT_ID", "17"))
        self.account = os.environ.get("IBKR_ACCOUNT", "")

    def is_paper(self):
        if self.account:
            return self.account.upper().startswith("DU")
        return self.port in PAPER_PORTS

    def get_account(self):
        raise NotImplementedError(STUB.format("read the account"))

    def submit_bracket_order(self, symbol, side, qty, stop_price, take_profit, client_order_id):
        raise NotImplementedError(STUB.format("place orders"))

    def submit_market_order(self, symbol, side, qty, client_order_id):
        raise NotImplementedError(STUB.format("place orders"))

    def get_order(self, order_id):
        raise NotImplementedError(STUB.format("read orders"))

    def get_order_by_client_id(self, client_order_id):
        raise NotImplementedError(STUB.format("read orders"))

    def list_orders(self, after):
        raise NotImplementedError(STUB.format("read orders"))

    def cancel_order(self, order_id):
        raise NotImplementedError(STUB.format("cancel orders"))

    def list_positions(self):
        raise NotImplementedError(STUB.format("read positions"))

    def close_all(self):
        raise NotImplementedError(STUB.format("close positions"))
