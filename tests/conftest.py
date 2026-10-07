import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402


@pytest.fixture
def cfg():
    """config.json plus the watchlist symbols, as load_config returns it."""
    c = copy.deepcopy(config_mod.load_json(config_mod.CONFIG_PATH))
    c["watchlist"] = [e["symbol"] for e in config_mod.load_json(config_mod.WATCHLIST_PATH)["symbols"]]
    return c


@pytest.fixture
def watchlist():
    return copy.deepcopy(config_mod.load_json(config_mod.WATCHLIST_PATH))


@pytest.fixture
def bounds():
    return config_mod.load_json(config_mod.BOUNDS_PATH)
