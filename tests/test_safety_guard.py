"""Rules the nightly review guard enforces on config and file changes."""
import copy
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
import guard  # noqa: E402


def make_trades(n, days, pnl=10.0, version=1, strategy="opening_range_breakout", strategy_version=1):
    start = date(2026, 6, 1)
    return pd.DataFrame({
        "date": [start + timedelta(days=i % days) for i in range(n)],
        "pnl_usd": [pnl] * n,
        "config_version": [version] * n,
        "strategy": [strategy] * n,
        "strategy_version": [strategy_version] * n,
    })


def bumped(cfg, **changes):
    new = copy.deepcopy(cfg)
    new["version"] += 1
    for path, value in changes.items():
        section, _, key = path.rpartition(".")
        (new[section] if section else new)[key] = value
    return new


@pytest.mark.parametrize("path,allowed", [
    ("config.json", True),
    ("data/changelog.md", True),
    ("data/reports/report_2026-10-06.md", True),
    ("strategies/orb_v2.py", True),
    ("tests/test_orb_v2.py", True),
    ("config.shadow.json", True),
    ("strategies/__init__.py", False),
    ("strategies/base.py", False),
    ("tests/test_safety_strategies.py", False),
    ("tests/conftest.py", False),
    ("trader.py", False),
    ("broker.py", False),
    ("risk.py", False),
    ("analyze.py", False),
    ("logger.py", False),
    ("config.py", False),
    ("simbroker.py", False),
    ("config_bounds.json", False),
    (".env", False),
    ("review/guard.py", False),
    ("review/REVIEW_PROMPT.md", False),
    ("data/trades.csv", False),
])
def test_path_allowlist(path, allowed):
    assert guard.path_allowed(path) is allowed


def test_no_change_is_fine(cfg):
    assert guard.config_change_errors(cfg, copy.deepcopy(cfg), make_trades(0, 1)) == []


def test_single_change_with_enough_evidence(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    assert guard.config_change_errors(cfg, new, make_trades(30, 10)) == []


def test_needs_trades_and_days(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    assert any("needs 30+ trades" in e for e in guard.config_change_errors(cfg, new, make_trades(29, 10)))
    assert any("10+ days" in e for e in guard.config_change_errors(cfg, new, make_trades(60, 9)))
    other_version = make_trades(40, 12, version=7)
    assert guard.config_change_errors(cfg, new, other_version)


def test_version_must_increment_by_one(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    new["version"] += 1
    assert any("version must go" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10)))


def test_only_one_change(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5, "strategy.stop_loss_r": 0.8})
    assert any("exactly one" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10)))
    new = bumped(cfg, watchlist=cfg["watchlist"] + ["IWM", "DIA"])
    assert any("exactly one" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10)))
    new = bumped(cfg, watchlist=cfg["watchlist"][1:])
    assert guard.config_change_errors(cfg, new, make_trades(30, 10)) == []


def test_risk_increase_needs_profit(cfg):
    new = bumped(cfg, **{"risk.risk_per_trade_pct": 0.75})
    assert any("net-profitable" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10, pnl=-5)))
    assert guard.config_change_errors(cfg, new, make_trades(30, 10, pnl=5)) == []
    lower = bumped(cfg, **{"risk.risk_per_trade_pct": 0.25})
    assert guard.config_change_errors(cfg, lower, make_trades(30, 10, pnl=-5)) == []


def test_strategy_switch_pacing(cfg):
    new = bumped(cfg, strategy_version=2)
    assert any("trading days" in e for e in guard.config_change_errors(cfg, new, make_trades(50, 9)))
    assert guard.config_change_errors(cfg, new, make_trades(10, 10)) == []
    same = bumped(cfg, **{"strategy.name": "opening_range_breakout"})
    same["strategy_version"] = 1
    assert guard.config_change_errors(cfg, same, make_trades(30, 10))  # no-op version bump is a param change of zero


def test_module_version():
    assert guard.module_version("NAME = 'x'\nVERSION = 3\n") == 3
    assert guard.module_version("x = 1") is None
