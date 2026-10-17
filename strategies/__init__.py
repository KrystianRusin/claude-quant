"""Strategy registry. Strategies are runtime state: modules in <state dir>/strategies/, keyed by file name.

The framework keeps only this loader and the interface in base.py. The state directory is added to
this package's path, so a strategy module imports as strategies.<name> wherever it lives.
"""
import importlib
import pkgutil
import re

from config import STATE_DIR
from strategies.base import INTERFACE_VERSION, SessionState, Signal

__all__ = ["INTERFACE_VERSION", "STRATEGY_DIR", "SessionState", "Signal", "available", "load"]

STRATEGY_DIR = STATE_DIR / "strategies"
__path__.append(str(STRATEGY_DIR))

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_REQUIRED = ("NAME", "VERSION", "INTERFACE", "PARAMS", "generate_signals")


def available():
    return sorted(m.name for m in pkgutil.iter_modules([str(STRATEGY_DIR)])
                  if not m.ispkg and _NAME.match(m.name))


def load(name):
    """Import <state dir>/strategies/<name>.py and check it exposes the current strategy interface."""
    if name not in available():
        raise ValueError(f"unknown strategy {name!r}; available in {STRATEGY_DIR}: {available()}")
    module = importlib.import_module(f"strategies.{name}")
    missing = [attr for attr in _REQUIRED if not hasattr(module, attr)]
    if missing:
        raise ValueError(f"strategy {name!r} is missing {missing}")
    if module.NAME != name:
        raise ValueError(f"strategy module {name!r} declares NAME={module.NAME!r}")
    if module.INTERFACE != INTERFACE_VERSION:
        raise ValueError(f"strategy {name!r} targets interface {module.INTERFACE!r}, but the framework "
                         f"provides interface {INTERFACE_VERSION}; update the strategy to the new interface")
    return module
