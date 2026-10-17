"""Strategy modules may only compute signals: no broker access, no I/O, no network.

Runs against the seed strategies in CI, and against the agent's strategies (QUANT_STATE_DIR=data)
when the review guard checks a review.
"""
import ast
import sys

import pytest

import strategies

STRATEGY_DIR = strategies.STRATEGY_DIR
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
    assert STRATEGY_FILES, f"no strategies in {STRATEGY_DIR}"


@pytest.mark.parametrize("path", STRATEGY_FILES, ids=lambda p: p.name)
def test_strategy_module_is_pure(path):
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


def test_registry_refuses_other_interface_versions(tmp_path, monkeypatch):
    source = "NAME = 'old_one'\nVERSION = 1\nINTERFACE = {}\nPARAMS = {{}}\n\ndef generate_signals(b, s, p):\n    return []\n"
    (tmp_path / "old_one.py").write_text(source.format(strategies.INTERFACE_VERSION - 1))
    (tmp_path / "current.py").write_text(source.format(strategies.INTERFACE_VERSION).replace("old_one", "current"))
    monkeypatch.setattr(strategies, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(strategies, "__path__", [*strategies.__path__, str(tmp_path)])
    for name in ("old_one", "current"):
        monkeypatch.delitem(sys.modules, f"strategies.{name}", raising=False)
    assert strategies.available() == ["current", "old_one"]
    assert strategies.load("current").NAME == "current"
    with pytest.raises(ValueError, match="targets interface"):
        strategies.load("old_one")
