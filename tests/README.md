# Tests

Run from the repo root:

```bash
.venv/Scripts/python -m pytest -q
```

No network or credentials are needed. Bars are synthetic (`helpers.py`), and `test_trader.py`
drives full sessions through `simbroker.SimBroker` with a simulated clock.

Files named `test_safety_*` hold the hard limits (paper-only endpoint, order cap, bounds ceilings,
strategy purity, review guard rules). The nightly reviewer may not edit them, `conftest.py` or
`helpers.py`. It may add tests for new strategies as `test_<strategy>.py`.
