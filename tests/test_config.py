import json

import pytest

from config import ConfigError, load_config, validate


def test_shipped_config_is_valid(cfg, bounds):
    assert validate(cfg, bounds) == []


@pytest.mark.parametrize("section,key,value", [
    ("risk", "risk_per_trade_pct", 1.5),
    ("risk", "max_daily_loss_pct", 3.5),
    ("risk", "max_trades_per_day", 0),
    ("strategy", "take_profit_r", 0.5),
    ("strategy", "take_profit_r", 4.5),
    ("strategy", "opening_range_minutes", 2),
    ("strategy", "force_exit_time", "15:59"),
    ("strategy", "entry_window_end", "13:00"),
])
def test_out_of_bounds_values_rejected(cfg, bounds, section, key, value):
    cfg[section][key] = value
    errors = validate(cfg, bounds)
    assert any(f"{section}.{key}" in e for e in errors)


@pytest.mark.parametrize("value", ["1.0", True, None])
def test_wrong_types_rejected(cfg, bounds, value):
    cfg["risk"]["risk_per_trade_pct"] = value
    assert validate(cfg, bounds)


def test_bool_is_not_an_int(cfg, bounds):
    cfg["risk"]["max_trades_per_day"] = True
    assert validate(cfg, bounds)


def test_bad_time_format(cfg, bounds):
    cfg["strategy"]["entry_window_end"] = "11am"
    assert any("HH:MM" in e for e in validate(cfg, bounds))


def test_watchlist_rules(cfg, bounds):
    cfg["watchlist"] = cfg["watchlist"] + ["GME"]
    assert any("not allowed" in e for e in validate(cfg, bounds))

    cfg["watchlist"] = ["SPY", "SPY"]
    assert any("duplicate" in e for e in validate(cfg, bounds))

    cfg["watchlist"] = bounds["watchlist"]["allowed"][:26]
    assert any("at most 25" in e for e in validate(cfg, bounds))

    cfg["watchlist"] = []
    assert validate(cfg, bounds)


def test_unknown_and_missing_keys(cfg, bounds):
    cfg["strategy"]["secret_sauce"] = 1
    del cfg["risk"]["max_trades_per_day"]
    errors = validate(cfg, bounds)
    assert any("secret_sauce: unknown key" in e for e in errors)
    assert any("max_trades_per_day: missing" in e for e in errors)


def test_strategy_must_exist_and_versions_match(cfg, bounds):
    cfg["strategy_version"] = 2
    assert any("VERSION = 1" in e for e in validate(cfg, bounds))

    cfg["strategy_version"] = 1
    cfg["strategy"]["name"] = "no_such_strategy"
    assert any("unknown strategy" in e for e in validate(cfg, bounds))


def test_central_bounds_override_module_params(cfg, bounds):
    bounds["strategy_params"]["take_profit_r"] = {"type": "float", "min": 1.0, "max": 1.5}
    cfg["strategy"]["take_profit_r"] = 2.0
    assert any("take_profit_r" in e for e in validate(cfg, bounds))


def test_live_feed_rejected(cfg, bounds):
    cfg["data_feed"] = "sip"
    assert validate(cfg, bounds)


def test_cross_field_rules(cfg, bounds):
    cfg["strategy"]["min_range_pct"] = 0.6
    cfg["strategy"]["max_range_pct"] = 0.5
    assert any("min_range_pct" in e for e in validate(cfg, bounds))

    cfg["strategy"]["min_range_pct"] = 0.15
    cfg["strategy"]["max_range_pct"] = 2.5
    cfg["strategy"]["opening_range_minutes"] = 30
    cfg["strategy"]["entry_window_end"] = "09:55"
    assert any("entry_window_end" in e for e in validate(cfg, bounds))


def test_load_config_raises(tmp_path, cfg):
    cfg["risk"]["risk_per_trade_pct"] = 5
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        load_config(config_path=path)
