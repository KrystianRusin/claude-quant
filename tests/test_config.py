import json

import pytest

from config import ConfigError, load_config, validate, validate_watchlist


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


def test_shipped_watchlist_is_valid(watchlist, bounds):
    assert validate_watchlist(watchlist, bounds["watchlist_rules"]) == []


def test_watchlist_rules(watchlist, bounds):
    rules = bounds["watchlist_rules"]
    watchlist["symbols"].append(dict(watchlist["symbols"][0]))
    assert any("duplicate" in e for e in validate_watchlist(watchlist, rules))

    watchlist["symbols"] = [{"symbol": f"A{chr(65 + i)}", "added": "2026-10-07", "reason": "x"} for i in range(26)]
    assert any("must be 5-25" in e for e in validate_watchlist(watchlist, rules))

    watchlist["symbols"] = watchlist["symbols"][:5]
    watchlist["symbols"][0]["symbol"] = "brk.b"
    watchlist["symbols"][1]["reason"] = " "
    errors = validate_watchlist(watchlist, rules)
    assert any("bad symbol" in e for e in errors) and any("reason" in e for e in errors)

    watchlist["removed"] = [{"symbol": "XYZ", "removed": "yesterday"}]
    assert any("removed[0]" in e for e in validate_watchlist(watchlist, rules))


def test_load_config_injects_watchlist():
    assert load_config()["watchlist"][:2] == ["SPY", "QQQ"]


def test_watchlist_not_allowed_in_config_json(tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="lives in watchlist.json"):
        load_config(config_path=path)


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
    del cfg["watchlist"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        load_config(config_path=path)


def test_ensure_state_seeds_missing_files_and_never_overwrites(tmp_path):
    from config import DEFAULTS_DIR, ensure_state

    state = tmp_path / "data"
    created = ensure_state(state, DEFAULTS_DIR)
    assert "created config.json" in created and "created strategies/" in created
    assert (state / "strategies" / "opening_range_breakout.py").exists()
    assert (state / "tests" / "test_opening_range_breakout.py").exists()
    (state / "memory.md").write_text("agent memory")
    (state / "strategies" / "opening_range_breakout.py").unlink()
    assert ensure_state(state, DEFAULTS_DIR) == []
    assert (state / "memory.md").read_text() == "agent memory"
    assert not (state / "strategies" / "opening_range_breakout.py").exists()  # the agent deleted it


def test_ensure_state_merges_new_framework_keys(tmp_path):
    from config import ensure_state

    defaults, state = tmp_path / "defaults", tmp_path / "data"
    defaults.mkdir()
    state.mkdir()
    (defaults / "config.json").write_text(json.dumps(
        {"version": 1, "risk": {"a": 1, "new_limit": 5}, "new_section": {"x": 1},
         "strategy": {"name": "seed", "seed_param": 1}}))
    (state / "config.json").write_text(json.dumps(
        {"version": 7, "risk": {"a": 3, "retired": 2}, "strategy": {"name": "mine", "my_param": 9}}))
    assert ensure_state(state, defaults) == ["merged new framework keys into config.json"]
    assert json.loads((state / "config.json").read_text()) == {
        "version": 7, "risk": {"a": 3, "new_limit": 5}, "new_section": {"x": 1},
        "strategy": {"name": "mine", "my_param": 9}}


def test_ensure_state_is_a_no_op_on_the_defaults_themselves():
    from config import DEFAULTS_DIR, ensure_state

    assert ensure_state(DEFAULTS_DIR, DEFAULTS_DIR) == []
