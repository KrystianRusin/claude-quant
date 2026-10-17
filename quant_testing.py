"""Pytest plugin shared by the framework tests (tests/), the seed strategy tests (defaults/tests/) and
the agent's tests (data/tests/). conftest.py loads it; review/guard.py passes it with -p, so it also
works when the state directory is outside the repo.

Without QUANT_STATE_DIR the tests run against the seeds in defaults/, so the framework is tested on
its own and never against whatever the agent has built. The review guard sets QUANT_STATE_DIR=data
to test the agent's strategies.
"""
import copy
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("QUANT_STATE_DIR", str(ROOT / "defaults"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))  # helpers.py

import config as config_mod  # noqa: E402


@pytest.fixture
def cfg():
    """The state's config.json plus the watchlist symbols, as load_config returns it."""
    c = copy.deepcopy(config_mod.load_json(config_mod.CONFIG_PATH))
    c["watchlist"] = [e["symbol"] for e in config_mod.load_json(config_mod.WATCHLIST_PATH)["symbols"]]
    return c


@pytest.fixture
def watchlist():
    return copy.deepcopy(config_mod.load_json(config_mod.WATCHLIST_PATH))


@pytest.fixture
def bounds():
    return config_mod.load_json(config_mod.BOUNDS_PATH)
