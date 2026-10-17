"""Rules the nightly review guard enforces on config and file changes."""
import copy
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
import guard  # noqa: E402


def make_trades(n, days, pnl=10.0, version=1, strategy="opening_range_breakout", strategy_version=1):
    start = date(2026, 6, 1)
    return pd.DataFrame({
        "date": [start + timedelta(days=i % days) for i in range(n)],
        "pnl_usd": [pnl] * n,
        "config_version": [version] * n,
        "strategy": [strategy] * n,
        "strategy_version": [strategy_version] * n,
    })


def bumped(cfg, **changes):
    new = copy.deepcopy(cfg)
    new["version"] += 1
    for path, value in changes.items():
        section, _, key = path.rpartition(".")
        (new[section] if section else new)[key] = value
    return new


@pytest.mark.parametrize("path,allowed", [  # relative to the state directory (data/)
    ("config.json", True),
    ("changelog.md", True),
    ("reports/report_2026-10-06.md", True),
    ("replay_check/trades.csv", True),
    ("strategies/orb_v2.py", True),
    ("strategies/sub/orb.py", False),
    ("strategies/Orb.py", False),
    ("tests/test_orb_v2.py", True),
    ("tests/conftest.py", False),
    ("config.shadow.json", True),
    ("memory.md", True),
    ("watchlist.json", True),
    ("journal/2026-10-07.md", True),
    ("journal/notes.md", False),
    ("universe.json", False),
    ("trades.csv", False),
    ("orders.csv", False),
    ("daily_summary.csv", False),
    ("lifetime.md", False),
    ("strategy_history/orb_v1.py", False),
    ("trader.log", False),
])
def test_path_allowlist(path, allowed):
    assert guard.path_allowed(path) is allowed


def test_no_change_is_fine(cfg):
    assert guard.config_change_errors(cfg, copy.deepcopy(cfg), make_trades(0, 1)) == []


def test_single_change_with_enough_evidence(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    assert guard.config_change_errors(cfg, new, make_trades(30, 10)) == []


def test_needs_trades_and_days(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    assert any("needs 30+ trades" in e for e in guard.config_change_errors(cfg, new, make_trades(29, 10)))
    assert any("10+ days" in e for e in guard.config_change_errors(cfg, new, make_trades(60, 9)))
    other_version = make_trades(40, 12, version=7)
    assert guard.config_change_errors(cfg, new, other_version)


def test_version_must_increment_by_one(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5})
    new["version"] += 1
    assert any("version must go" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10)))


def test_only_one_change(cfg):
    new = bumped(cfg, **{"strategy.take_profit_r": 1.5, "strategy.stop_loss_r": 0.8})
    assert any("exactly one" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10)))


def test_risk_increase_needs_profit(cfg):
    new = bumped(cfg, **{"risk.risk_per_trade_pct": 0.75})
    assert any("net-profitable" in e for e in guard.config_change_errors(cfg, new, make_trades(30, 10, pnl=-5)))
    assert guard.config_change_errors(cfg, new, make_trades(30, 10, pnl=5)) == []
    lower = bumped(cfg, **{"risk.risk_per_trade_pct": 0.25})
    assert guard.config_change_errors(cfg, lower, make_trades(30, 10, pnl=-5)) == []


def test_strategy_switch_pacing(cfg):
    new = bumped(cfg, strategy_version=2)
    assert any("trading days" in e for e in guard.config_change_errors(cfg, new, make_trades(50, 9)))
    assert guard.config_change_errors(cfg, new, make_trades(10, 10)) == []
    same = bumped(cfg, **{"strategy.name": "opening_range_breakout"})
    same["strategy_version"] = 1
    assert guard.config_change_errors(cfg, same, make_trades(30, 10))  # no-op version bump is a param change of zero


def test_module_version():
    assert guard.module_version("NAME = 'x'\nVERSION = 3\n") == 3
    assert guard.module_version("x = 1") is None


def memory(*entries, retired=()):
    def block(entry_id, status):
        return f"### {entry_id}: title\n- Status: {status}\n- Evidence: x\n"
    return ("# Trading memory\n\n## Active\n\n" + "".join(block(*e) for e in entries)
            + "\n## Retired\n\n" + "".join(block(*e) for e in retired))


def test_memory_valid_growth_and_retire():
    old = memory(("M-1", "hypothesis"))
    assert guard.memory_errors(old, memory(("M-1", "supported"), ("M-2", "hypothesis"))) == []
    assert guard.memory_errors(old, memory(retired=[("M-1", "retired")])) == []


def test_memory_entries_never_deleted():
    old = memory(("M-1", "hypothesis"), ("M-2", "hypothesis"))
    assert any("deleted" in e for e in guard.memory_errors(old, memory(("M-2", "hypothesis"))))


def test_memory_status_required():
    errors = guard.memory_errors("", memory(("M-1", "probably")))
    assert any("M-1" in e and "Status" in e for e in errors)


def test_memory_active_cap():
    many = memory(*[(f"M-{i}", "hypothesis") for i in range(guard.MAX_ACTIVE_MEMORIES + 1)])
    assert any("active entries" in e for e in guard.memory_errors("", many))
    retired = memory(retired=[(f"M-{i}", "retired") for i in range(guard.MAX_ACTIVE_MEMORIES + 1)])
    assert guard.memory_errors("", retired) == []


def test_shipped_memory_file_is_valid():
    text = (guard.STATE_DIR / "memory.md").read_text(encoding="utf-8")
    assert guard.memory_errors(text, text) == []


TODAY = date(2026, 10, 20)
RULES = {"max_adds_per_review": 3, "max_removes_per_review": 3, "readd_cooldown_days": 10,
         "max_universe_age_days": 4}


def wl(symbols, removed=()):
    return {"symbols": [{"symbol": s, "added": "2026-10-06", "reason": "initial"} for s in symbols],
            "removed": [dict(r) for r in removed]}


def add(w, sym, added=TODAY.isoformat()):
    w["symbols"].append({"symbol": sym, "added": added, "reason": "high range, liquid"})
    return w


def remove(w, sym, when=TODAY.isoformat()):
    w["symbols"] = [e for e in w["symbols"] if e["symbol"] != sym]
    w["removed"].append({"symbol": sym, "removed": when, "reason": "low range"})
    return w


UNIVERSE = {"generated": "2026-10-19", "symbols": {s: {} for s in ["SPY", "QQQ", "AMD", "MU", "XOM", "JPM", "BA"]}}


def wl_errors(old, new, universe=UNIVERSE):
    return guard.watchlist_change_errors(old, new, universe, TODAY, RULES)


def test_watchlist_swap_ok():
    old = wl(["SPY", "QQQ", "AMD"])
    new = remove(add(wl(["SPY", "QQQ", "AMD"]), "MU"), "AMD")
    assert wl_errors(old, new) == []


def test_watchlist_limits():
    old = wl(["SPY"])
    new = wl(["SPY"])
    for sym in ("MU", "XOM", "JPM", "BA"):
        add(new, sym)
    assert any("4 adds" in e for e in wl_errors(old, new))


def test_watchlist_add_must_be_in_fresh_universe():
    old = wl(["SPY"])
    assert any("not in the current universe" in e for e in wl_errors(old, add(wl(["SPY"]), "GME")))
    stale = {**UNIVERSE, "generated": "2026-10-10"}
    assert any("days old" in e for e in wl_errors(old, add(wl(["SPY"]), "MU"), stale))
    assert any("no data/universe.json" in e for e in wl_errors(old, add(wl(["SPY"]), "MU"), None))
    assert any("must be today" in e for e in wl_errors(old, add(wl(["SPY"]), "MU", added="2026-10-01")))


def test_watchlist_remove_needs_log_and_history_is_append_only():
    old = wl(["SPY", "QQQ"], removed=[{"symbol": "AMD", "removed": "2026-09-01", "reason": "x"}])
    silent = wl(["SPY"], removed=old["removed"])
    assert any("needs a 'removed' entry" in e for e in wl_errors(old, silent))
    rewritten = remove(wl(["SPY", "QQQ"]), "QQQ")
    assert any("append-only" in e for e in wl_errors(old, rewritten))


def test_watchlist_readd_cooldown():
    old = wl(["SPY"], removed=[{"symbol": "MU", "removed": "2026-10-15", "reason": "x"}])
    new = add(wl(["SPY"], removed=old["removed"]), "MU")
    assert any("wait 10 days" in e for e in wl_errors(old, new))
    old["removed"][0]["removed"] = new["removed"][0]["removed"] = "2026-10-01"
    assert wl_errors(old, new) == []


def test_watchlist_entries_not_rewritten():
    old = wl(["SPY", "QQQ"])
    new = wl(["SPY", "QQQ"])
    new["symbols"][0]["reason"] = "changed my mind"
    assert any("must not be rewritten" in e for e in wl_errors(old, new))


def late_losers(days=16):
    rows = []
    for d in range(days):
        for minute, r in ((70, -1.0), (75, -0.8), (20, 0.5)):
            rows.append({"date": date(2026, 9, 1) + timedelta(days=d), "symbol": "SPY", "side": "long",
                         "pnl_r": r, "pnl_usd": r * 100, "entry_minute_after_open": minute, "range_pct": 0.5})
    return pd.DataFrame(rows)


HYPOTHESIS = """## Active

### M-3: late entries lose
- Status: supported
- Since: 2026-09-08 | Last reviewed: 2026-09-20
- Query: `entry_minute_after_open >= 60`
- Evidence: x
"""
CHANGE = [("strategy.entry_window_end", "11:00", "10:30")]
EV_TODAY = date(2026, 9, 20)


def test_evidence_required_for_param_change():
    errors = guard.evidence_errors(CHANGE, "## 2026-09-20\nmoved window", HYPOTHESIS, late_losers(), EV_TODAY)
    assert any("Evidence: M-<id>" in e for e in errors)


def test_evidence_passes_with_registered_hypothesis():
    assert guard.evidence_errors(CHANGE, "Evidence: M-3", HYPOTHESIS, late_losers(), EV_TODAY) == []


def test_evidence_must_be_registered_earlier():
    errors = guard.evidence_errors(CHANGE, "Evidence: M-4", HYPOTHESIS, late_losers(), EV_TODAY)
    assert any("not in memory before" in e for e in errors)
    today_entry = HYPOTHESIS.replace("Since: 2026-09-08", "Since: 2026-09-20")
    errors = guard.evidence_errors(CHANGE, "Evidence: M-3", today_entry, late_losers(), EV_TODAY)
    assert any("registered today" in e for e in errors)


def test_evidence_fails_on_thin_data():
    errors = guard.evidence_errors(CHANGE, "Evidence: M-3", HYPOTHESIS, late_losers(days=9), EV_TODAY)
    assert any("did not pass" in e for e in errors)


def test_risk_decrease_needs_no_evidence():
    change = [("risk.risk_per_trade_pct", 0.5, 0.25)]
    assert guard.evidence_errors(change, "", "", late_losers(), EV_TODAY) == []
    assert guard.evidence_errors([("risk.risk_per_trade_pct", 0.5, 0.75)], "", "", late_losers(), EV_TODAY)


def make_state(root):
    state = root / "data"
    (state / "strategies").mkdir(parents=True)
    (state / "journal").mkdir()
    (state / "config.json").write_text('{"version": 1}')
    (state / "trades.csv").write_text("date,pnl_usd\n")
    (state / "journal" / "2026-10-06.md").write_text("# old day")
    (state / "strategies" / "orb.py").write_text("NAME = 'orb'\nVERSION = 1\n")
    return state


def snap(tmp_path, state):
    guard.snapshot(tmp_path / "snap", state_dir=state, framework=False)
    return tmp_path / "snap"


def changed(snapshot_dir, state):
    before = guard.json.loads((snapshot_dir / "manifest.json").read_text())
    return guard.changed_paths(before, guard.manifest(state))


def test_snapshot_detects_changes_without_git(tmp_path):
    state = make_state(tmp_path)
    s = snap(tmp_path, state)
    assert changed(s, state) == []
    (state / "config.json").write_text('{"version": 2}')
    (state / "journal" / "2026-10-07.md").write_text("# new day")
    (state / "trades.csv").unlink()
    (state / "strategies" / "__pycache__").mkdir()
    (state / "strategies" / "__pycache__" / "orb.pyc").write_bytes(b"x")
    paths = changed(s, state)
    assert paths == ["config.json", "journal/2026-10-07.md", "trades.csv"]
    assert guard.path_errors(paths) == ["data/trades.csv: the reviewer may not change this file"]


def test_past_journal_edit_detected(tmp_path):
    state = make_state(tmp_path)
    s = snap(tmp_path, state)
    (state / "journal" / "2026-10-06.md").write_text("# rewritten")
    before = guard.json.loads((s / "manifest.json").read_text())
    assert guard.journal_errors(before, changed(s, state))


def test_revert_restores_snapshot_and_keeps_new_journal(tmp_path):
    state = make_state(tmp_path)
    s = snap(tmp_path, state)
    (state / "config.json").write_text('{"version": 99}')
    (state / "strategies" / "orb.py").write_text("broken")
    (state / "strategies" / "new_one.py").write_text("NAME = 'new_one'\nVERSION = 1\n")
    (state / "trades.csv").unlink()
    (state / "journal" / "2026-10-07.md").write_text("# new day")
    (state / "reports").mkdir()
    (state / "reports" / "report.md").write_text("r")
    guard.revert(s, state_dir=state, framework=False)
    assert (state / "config.json").read_text() == '{"version": 1}'
    assert "VERSION = 1" in (state / "strategies" / "orb.py").read_text()
    assert not (state / "strategies" / "new_one.py").exists()
    assert (state / "trades.csv").exists()
    assert (state / "journal" / "2026-10-07.md").exists()
    assert (state / "reports" / "report.md").exists()
    assert changed(s, state) == ["journal/2026-10-07.md", "reports/report.md"]


def test_strategy_edit_needs_version_bump(tmp_path):
    state = make_state(tmp_path)
    s = snap(tmp_path, state)
    module = state / "strategies" / "orb.py"
    module.write_text("NAME = 'orb'\nVERSION = 1\nX = 2\n")
    assert any("without raising VERSION" in e for e in guard.strategy_version_errors(s, changed(s, state), state))
    module.write_text("NAME = 'orb'\nVERSION = 2\nX = 2\n")
    assert guard.strategy_version_errors(s, changed(s, state), state) == []


def test_archive_keeps_every_version_and_blocks_reuse(tmp_path):
    state = make_state(tmp_path)
    assert guard.archive(state) == ["orb_v1.py"]
    assert guard.archive(state) == []
    s = snap(tmp_path, state)
    module = state / "strategies" / "orb.py"
    module.write_text("NAME = 'orb'\nVERSION = 2\n")
    assert guard.archive(state) == ["orb_v2.py"]
    assert (state / "strategy_history" / "orb_v1.py").read_text() == "NAME = 'orb'\nVERSION = 1\n"
    module.write_text("NAME = 'orb'\nVERSION = 2\nX = 'different code, same version'\n")
    assert any("already used" in e for e in guard.strategy_version_errors(s, changed(s, state), state))
