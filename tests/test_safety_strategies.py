"""Strategy modules may only compute signals: no broker access, no I/O, no network."""
import ast
from pathlib import Path

import pytest

import strategies

STRATEGY_DIR = Path(strategies.__file__).parent
ALLOWED_IMPORTS = {"__future__", "abc", "collections", "dataclasses", "datetime", "enum", "functools",
                   "itertools", "math", "numpy", "pandas", "statistics", "strategies", "typing"}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
                   "globals", "setattr", "delattr"}
FORBIDDEN_ATTR_PREFIXES = ("read_", "to_csv", "to_json", "to_pickle", "to_parquet", "to_sql",
                           "to_excel", "to_feather", "to_hdf", "to_html", "to_clipboard", "tofile",
                           "fromfile", "loadtxt", "savetxt", "genfromtxt", "save", "load", "system", "popen")
STRATEGY_FILES = sorted(STRATEGY_DIR.glob("*.py"))


def violations(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            found.append(f"call {node.func.id}()")
        elif isinstance(node, ast.Attribute) and node.attr.startswith(FORBIDDEN_ATTR_PREFIXES):
            found.append(f"attribute .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "importlib"):
            found.append(f"name {node.id}")
    return found


def test_strategy_files_found():
    assert STRATEGY_DIR / "opening_range_breakout.py" in STRATEGY_FILES


@pytest.mark.parametrize("path", STRATEGY_FILES, ids=lambda p: p.name)
def test_strategy_module_is_pure(path):
    if path.name == "__init__.py":
        pytest.skip("registry loader, not a strategy")
    assert violations(path) == []


@pytest.mark.parametrize("source,expected", [
    ("import os", "import os"),
    ("import broker", "import broker"),
    ("from broker import AlpacaBroker", "from broker import ..."),
    ("import subprocess", "import subprocess"),
    ("import requests", "import requests"),
    ("from urllib import request", "from urllib import ..."),
    ("import socket", "import socket"),
    ("open('x')", "call open()"),
    ("import pandas as pd\npd.read_csv('x')", "attribute .read_csv"),
])
def test_checker_catches(tmp_path, source, expected):
    f = tmp_path / "bad.py"
    f.write_text(source)
    assert expected in violations(f)


@pytest.mark.parametrize("name", [n for n in strategies.available()])
def test_every_strategy_implements_interface(name):
    module = strategies.load(name)
    assert isinstance(module.VERSION, int) and module.VERSION >= 1
    assert isinstance(module.PARAMS, dict)
    assert callable(module.generate_signals)
