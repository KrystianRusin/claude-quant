import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402


@pytest.fixture
def cfg():
    return copy.deepcopy(config_mod.load_json(config_mod.CONFIG_PATH))


@pytest.fixture
def bounds():
    return config_mod.load_json(config_mod.BOUNDS_PATH)
