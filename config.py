"""Load the config and watchlist and validate them against config_bounds.json.

The framework (this repo) ships seeds in defaults/. Everything the agent writes lives in the state
directory, data/ by default (QUANT_STATE_DIR overrides it; the tests point it at defaults/).
It is gitignored and never committed.

Run `python config.py` to create missing state from the defaults, merge config keys added by a
framework update, and validate the config and watchlist.
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULTS_DIR = ROOT / "defaults"
STATE_DIR = Path(os.environ.get("QUANT_STATE_DIR", ROOT / "data")).resolve()
CONFIG_PATH = STATE_DIR / "config.json"
WATCHLIST_PATH = STATE_DIR / "watchlist.json"
SHADOW_CONFIG_PATH = STATE_DIR / "config.shadow.json"
BOUNDS_PATH = ROOT / "config_bounds.json"
SEED_FILES = ("config.json", "watchlist.json", "memory.md", "changelog.md")
SEED_DIRS = ("strategies", "tests")
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
                      f"but {module.__file__} has VERSION = {module.VERSION}")
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


def merge_new_keys(current, default):
    """Add keys the defaults have and `current` lacks, drop keys the defaults no longer have.

    Recurses into sections. The strategy section belongs to the strategy module, so it is left alone.
    """
    out = {}
    for key, value in default.items():
        if key not in current:
            out[key] = value
        elif key != "strategy" and isinstance(value, dict) and isinstance(current[key], dict):
            out[key] = merge_new_keys(current[key], value)
        else:
            out[key] = current[key]
    if "strategy" in current:
        out["strategy"] = current["strategy"]
    return out


def ensure_state(state_dir=STATE_DIR, defaults_dir=DEFAULTS_DIR):
    """Create missing state files from the defaults (never overwriting), then merge new config keys.

    A seed folder is copied only when the state has none, so strategies or tests the agent
    deleted stay deleted. Returns a list of what changed.
    """
    state_dir, defaults_dir = Path(state_dir), Path(defaults_dir)
    if state_dir.resolve() == defaults_dir.resolve():
        return []
    state_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in SEED_FILES:
        if not (state_dir / name).exists() and (defaults_dir / name).exists():
            shutil.copy2(defaults_dir / name, state_dir / name)
            changed.append(f"created {name}")
    for name in SEED_DIRS:
        if not (state_dir / name).exists() and (defaults_dir / name).is_dir():
            shutil.copytree(defaults_dir / name, state_dir / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
            changed.append(f"created {name}/")
    for path in (state_dir / "config.json", state_dir / "config.shadow.json"):
        if not path.exists():
            continue
        current = load_json(path)
        merged = merge_new_keys(current, load_json(defaults_dir / "config.json"))
        if merged != current:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
            changed.append(f"merged new framework keys into {path.name}")
    return changed


if __name__ == "__main__":
    for change in ensure_state():
        print(change)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        sys.exit(1)
    print(f"{CONFIG_PATH} and {WATCHLIST_PATH.name} are valid (version {cfg['version']}, "
          f"{len(cfg['watchlist'])} symbols: {' '.join(cfg['watchlist'])})")
