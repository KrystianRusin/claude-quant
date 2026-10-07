"""Load config.json and validate it against config_bounds.json.

Run `python config.py` to validate the current config from the command line.
"""
import json
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
BOUNDS_PATH = ROOT / "config_bounds.json"
MARKET_OPEN = time(9, 30)


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
    elif kind == "symbols":
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            return [f"{path}: expected a list of symbols"]
        if len(set(value)) != len(value):
            errors.append(f"{path}: duplicate symbols")
        bad = [s for s in value if s not in rule["allowed"]]
        if bad:
            errors.append(f"{path}: symbols not allowed by bounds: {bad}")
        if len(value) < rule.get("min_items", 0):
            errors.append(f"{path}: needs at least {rule['min_items']} symbols")
        if "max_items" in rule and len(value) > rule["max_items"]:
            errors.append(f"{path}: at most {rule['max_items']} symbols allowed, got {len(value)}")
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
    top = {k: v for k, v in bounds.items() if k not in ("strategy", "strategy_params")}
    errors = _walk({k: v for k, v in config.items() if k != "strategy"}, top)
    if "strategy" not in config:
        return errors + ["strategy: missing"]
    return errors + _strategy_errors(config, bounds)


def load_config(config_path=CONFIG_PATH, bounds_path=BOUNDS_PATH):
    """Load and validate the config. Raises ConfigError listing every problem."""
    config = load_json(config_path)
    errors = validate(config, load_json(bounds_path))
    if errors:
        raise ConfigError("config.json is invalid:\n  " + "\n  ".join(errors))
    return config


if __name__ == "__main__":
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        sys.exit(1)
    print(f"config.json is valid (version {cfg['version']})")
