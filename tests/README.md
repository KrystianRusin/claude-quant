# Tests

Run from the repo root:

```bash
.venv/Scripts/python -m pytest -q
```

No network or credentials are needed. Bars are synthetic (`helpers.py`), and `test_trader.py`
drives full sessions through `simbroker.SimBroker` with a simulated clock.

`test_db.py` covers `trader.db`: migrations, the pinned shared core schemas, the CSV mirror, import,
check, backup and restore, plus order states, snapshots, runs and versions.

`test_broker_contract.py` is the contract every execution backend must pass (SimBroker, and Alpaca
on the in-memory client in `fake_alpaca.py`). A new backend adds a harness there.

Plain `pytest` runs the framework tests here plus the seed strategy's tests in `defaults/tests/`,
with `QUANT_STATE_DIR` pointed at `defaults/`, so the framework is always tested against the seeds and
never against what the agent has built. Shared fixtures are in `quant_testing.py` at the repo root.

Files named `test_safety_*` hold the hard limits (paper-only endpoint, the `ALLOW_LIVE` gate, order cap, bounds ceilings,
strategy purity, review guard rules). The nightly reviewer cannot edit anything here. It writes
tests for its own strategies in `data/tests/test_<strategy>.py`, which the guard runs together with
`test_safety_strategies.py` against the agent's state:

```bash
QUANT_STATE_DIR=data .venv/bin/python -m pytest -q -p quant_testing tests/test_safety_strategies.py data/tests
```
