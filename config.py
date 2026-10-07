"""Load config.json and watchlist.json and validate them against config_bounds.json.

Run `python config.py` to validate the current config and watchlist from the command line.
"""
import json
import re
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BOUNDS_PATH = ROOT / "config_bounds.json"
WATCHLIST_PATH = ROOT / "watchlist.json"
MARKET_OPEN = time(9, 30)
SYMBOL = re.compile(r"^[A-Z]{1,5}$")
NOT_CONFIG = ("strategy", "strategy_params", "watchlist_rules", "universe")


class ConfigError(ValueError):
    pass


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_hhmm(value):
    return datetime.strptime(value, "%H:%M").time()


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_leaf(path, value, rule):
    kind = rule["type"]
    errors = []

    def in_range(v, lo, hi):
        if lo is not None and v < lo:
            errors.append(f"{path}: {value!r} is below min {rule['min']!r}")
        if hi is not None and v > hi:
            errors.append(f"{path}: {value!r} is above max {rule['max']!r}")

    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path}: expected int, got {value!r}"]
        in_range(value, rule.get("min"), rule.get("max"))
    elif kind == "float":
        if not _is_number(value):
            return [f"{path}: expected number, got {value!r}"]
        in_range(value, rule.get("min"), rule.get("max"))
    elif kind == "bool":
        if not isinstance(value, bool):
            return [f"{path}: expected true/false, got {value!r}"]
    elif kind == "time":
        try:
            t = parse_hhmm(value)
        except (TypeError, ValueError):
            return [f"{path}: expected HH:MM, got {value!r}"]
        lo = parse_hhmm(rule["min"]) if "min" in rule else None
        hi = parse_hhmm(rule["max"]) if "max" in rule else None
        in_range(t, lo, hi)
    elif kind == "choice":
        if value not in rule["allowed"]:
            errors.append(f"{path}: {value!r} not in {rule['allowed']}")
    else:
        errors.append(f"{path}: unknown bounds type {kind!r}")
    return errors


def _walk(config, bounds, prefix=""):
    if not isinstance(config, dict):
        return [f"{prefix.rstrip('.') or 'config'}: expected an object"]
    errors = []
    for key in sorted(set(config) - set(bounds)):
        errors.append(f"{prefix}{key}: unknown key (not in config_bounds.json)")
    for key, rule in bounds.items():
        path = f"{prefix}{key}"
        if key not in config:
            errors.append(f"{path}: missing")
        elif "type" in rule:
            errors.extend(_check_leaf(path, config[key], rule))
        else:
            errors.extend(_walk(config[key], rule, prefix=f"{path}."))
    return errors


def _strategy_errors(config, bounds):
    """Validate the strategy section: common keys from bounds, the rest declared by the strategy module."""
    import strategies

    section = config.get("strategy")
    if not isinstance(section, dict):
        return ["strategy: expected an object"]
    name = section.get("name")
    try:
        module = strategies.load(name)
    except ValueError as e:
        return [f"strategy.name: {e}"]
    common = {k: v for k, v in bounds["strategy"].items() if k != "name"}
    params = {k: bounds["strategy_params"].get(k, rule) for k, rule in module.PARAMS.items()}
    errors = _walk({k: v for k, v in section.items() if k != "name"}, {**common, **params}, "strategy.")
    if not isinstance(config.get("strategy_version"), int) or config["strategy_version"] != module.VERSION:
        errors.append(f"strategy_version: config says {config.get('strategy_version')!r} "
                      f"but strategies/{name}.py has VERSION = {module.VERSION}")
    if not errors:
        if parse_hhmm(section["entry_window_end"]) <= MARKET_OPEN:
            errors.append("strategy: entry_window_end must be after the market open")
        if parse_hhmm(section["force_exit_time"]) <= parse_hhmm(section["entry_window_end"]):
            errors.append("strategy: force_exit_time must be after entry_window_end")
        if hasattr(module, "validate_params"):
            errors.extend(module.validate_params(section))
    return errors


def validate(config, bounds):
    """Return a list of human-readable errors; empty means valid."""
    if not isinstance(config, dict):
        return ["config: expected an object"]
    top = {k: v for k, v in bounds.items() if k not in NOT_CONFIG}
    errors = _walk({k: v for k, v in config.items() if k not in ("strategy", "watchlist")}, top)
    if "strategy" not in config:
        return errors + ["strategy: missing"]
    return errors + _strategy_errors(config, bounds)


def _is_date(v):
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def validate_watchlist(data, rules):
    """Return a list of errors for watchlist.json; empty means valid."""
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list) \
            or not isinstance(data.get("removed"), list):
        return ['watchlist.json: expected {"symbols": [...], "removed": [...]}']
    errors = []
    symbols = []
    for i, entry in enumerate(data["symbols"]):
        sym = entry.get("symbol") if isinstance(entry, dict) else None
        if not isinstance(sym, str) or not SYMBOL.match(sym):
            errors.append(f"watchlist.symbols[{i}]: bad symbol {sym!r}")
            continue
        symbols.append(sym)
        if not _is_date(entry.get("added")) or not str(entry.get("reason", "")).strip():
            errors.append(f"watchlist {sym}: needs an 'added' date (YYYY-MM-DD) and a 'reason'")
    if len(set(symbols)) != len(symbols):
        errors.append("watchlist: duplicate symbols")
    if not rules["min_symbols"] <= len(symbols) <= rules["max_symbols"]:
        errors.append(f"watchlist: {len(symbols)} symbols, must be {rules['min_symbols']}-{rules['max_symbols']}")
    for i, entry in enumerate(data["removed"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str) \
                or not _is_date(entry.get("removed")) or not str(entry.get("reason", "")).strip():
            errors.append(f"watchlist.removed[{i}]: needs 'symbol', 'removed' date and 'reason'")
    return errors


def load_config(config_path=CONFIG_PATH, bounds_path=BOUNDS_PATH, watchlist_path=WATCHLIST_PATH):
    """Load and validate the config and watchlist; the watchlist's symbols land in config["watchlist"].

    Raises ConfigError listing every problem.
    """
    config = load_json(config_path)
    bounds = load_json(bounds_path)
    errors = validate(config, bounds)
    if "watchlist" in config:
        errors.append("watchlist: lives in watchlist.json, not config.json")
    watchlist = load_json(watchlist_path)
    errors += validate_watchlist(watchlist, bounds["watchlist_rules"])
    if errors:
        raise ConfigError("config is invalid:\n  " + "\n  ".join(errors))
    config["watchlist"] = [e["symbol"] for e in watchlist["symbols"]]
    return config


if __name__ == "__main__":
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        sys.exit(1)
    print(f"config.json and watchlist.json are valid (version {cfg['version']}, "
          f"{len(cfg['watchlist'])} symbols: {' '.join(cfg['watchlist'])})")
