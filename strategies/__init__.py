"""Strategy registry: every module in this package except base is a strategy, keyed by file name."""
import importlib
import pkgutil
import re

from strategies.base import SessionState, Signal

__all__ = ["SessionState", "Signal", "available", "load"]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_REQUIRED = ("NAME", "VERSION", "PARAMS", "generate_signals")


def available():
    return sorted(m.name for m in pkgutil.iter_modules(__path__)
                  if m.name != "base" and _NAME.match(m.name))


def load(name):
    """Import strategies/<name>.py and check it exposes the strategy interface."""
    if name not in available():
        raise ValueError(f"unknown strategy {name!r}; available: {available()}")
    module = importlib.import_module(f"strategies.{name}")
    missing = [attr for attr in _REQUIRED if not hasattr(module, attr)]
    if missing:
        raise ValueError(f"strategy {name!r} is missing {missing}")
    if module.NAME != name:
        raise ValueError(f"strategy module {name!r} declares NAME={module.NAME!r}")
    return module
