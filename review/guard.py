"""Code-level checks on what a nightly review changed. Used by run_review.sh.

Everything the review may touch lives in the state directory (data/), which is gitignored and never
committed. The baseline is a snapshot taken just before the review, not git.

python review/guard.py snapshot DIR                 record the state (hashes + copies) before the review
python review/guard.py check --snapshot DIR [--skip-replay]
                                                    exit 1 and list problems if the changes break a rule
python review/guard.py revert --snapshot DIR        undo every change made since the snapshot
python review/guard.py archive                      save each strategy's current VERSION to strategy_history/
"""
import argparse
import ast
import hashlib
import json
import os
import re
from datetime import date
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import BOUNDS_PATH, STATE_DIR, ConfigError, load_config, load_json  # noqa: E402
from analyze import evidence, format_evidence  # noqa: E402
from logger import read_trades_frame  # noqa: E402

MIN_TRADES = 30
MIN_DAYS = 10
STRATEGY_MIN_DAYS = 10
REPLAY_DAYS = 10
# Paths below are relative to the state directory.
EDITABLE_FILES = {"config.json", "config.shadow.json", "watchlist.json", "changelog.md", "memory.md"}
OUTPUT_DIRS = ("reports/", "replay/", "replay_check/")  # written by tools the reviewer runs
JOURNAL = re.compile(r"^journal/\d{4}-\d{2}-\d{2}\.md$")
STRATEGY = re.compile(r"^strategies/[a-z][a-z0-9_]*\.py$")
AGENT_TEST = re.compile(r"^tests/test_[a-z0-9_]+\.py$")
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SQLITE_TRANSIENT = ("-wal", "-shm", "-journal")  # exist only while a connection is open; trader.db itself is checked
HISTORY_DIR = "strategy_history"
MEMORY_ENTRY = re.compile(r"^### (M-\d+):", re.MULTILINE)
MEMORY_STATUSES = {"hypothesis", "supported", "confirmed", "retired"}
MAX_ACTIVE_MEMORIES = 40
MAX_MEMORY_BYTES = 60_000


def files(state_dir):
    """Every file under the state directory, as relative POSIX paths."""
    state_dir = Path(state_dir)
    for dirpath, dirnames, filenames in os.walk(state_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(SQLITE_TRANSIENT):
                continue
            yield (Path(dirpath) / name).relative_to(state_dir).as_posix()


def manifest(state_dir):
    """{relative path: sha256} for every file in the state directory."""
    return {p: hashlib.sha256((Path(state_dir) / p).read_bytes()).hexdigest() for p in files(state_dir)}


def changed_paths(before, after):
    """Paths added, removed or modified between two manifests."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def path_allowed(path):
    return (path in EDITABLE_FILES or path.startswith(OUTPUT_DIRS) or bool(JOURNAL.match(path))
            or bool(STRATEGY.match(path)) or bool(AGENT_TEST.match(path)))


def path_errors(paths):
    return [f"data/{p}: the reviewer may not change this file" for p in paths if not path_allowed(p)]


def framework_status():
    """Uncommitted changes to the framework checkout. data/ is gitignored, so this is code only."""
    return set(subprocess.run(["git", "status", "--porcelain", "--no-renames", "-uall"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.splitlines())


def framework_errors(snap):
    before = set(json.loads((Path(snap) / "framework.json").read_text(encoding="utf-8")))
    return [f"{line[3:]}: the reviewer may not change framework code" for line in sorted(framework_status() - before)]


def saved_text(snap, path):
    """A file's content when the snapshot was taken, or "" if it did not exist."""
    f = Path(snap) / "state" / path
    return f.read_text(encoding="utf-8") if f.exists() else ""


def journal_errors(before, paths):
    """Past journal entries are a record: they may be added, never edited."""
    return [f"data/{p}: past journal entries must not be edited" for p in paths if JOURNAL.match(p) and p in before]


def memory_errors(old_text, new_text):
    """Entries are never deleted, the active list stays small, and every entry has a valid status."""
    errors = []
    if len(new_text.encode("utf-8")) > MAX_MEMORY_BYTES:
        errors.append(f"memory.md is over {MAX_MEMORY_BYTES} bytes; merge or retire entries")
    missing = sorted(set(MEMORY_ENTRY.findall(old_text)) - set(MEMORY_ENTRY.findall(new_text)))
    if missing:
        errors.append(f"memory.md: entries deleted instead of retired: {missing}")
    active = new_text.split("## Retired")[0]
    n_active = len(MEMORY_ENTRY.findall(active))
    if n_active > MAX_ACTIVE_MEMORIES:
        errors.append(f"memory.md: {n_active} active entries, max {MAX_ACTIVE_MEMORIES}")
    blocks = MEMORY_ENTRY.split(new_text)[1:]
    for entry_id, body in zip(blocks[::2], blocks[1::2]):
        status = re.search(r"^- Status: *(\w+)", body, re.MULTILINE)
        if not status or status.group(1) not in MEMORY_STATUSES:
            errors.append(f"memory.md {entry_id}: needs '- Status:' with one of {sorted(MEMORY_STATUSES)}")
    return errors


def module_version(source):
    """VERSION = <int> from a strategy module's source, or None."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "VERSION" for t in node.targets):
            return getattr(node.value, "value", None)
    return None


def history_path(state_dir, name, version):
    return Path(state_dir) / HISTORY_DIR / f"{name}_v{version}.py"


def strategy_version_errors(snap, paths, state_dir):
    """An edited strategy must raise its VERSION, to a number not already archived with other code."""
    errors = []
    for p in paths:
        live = Path(state_dir) / p
        if not STRATEGY.match(p) or not live.exists():
            continue
        new_source = live.read_text(encoding="utf-8")
        new_v = module_version(new_source)
        old_source = saved_text(snap, p)
        if old_source:
            old_v = module_version(old_source)
            if not (isinstance(old_v, int) and isinstance(new_v, int) and new_v > old_v):
                errors.append(f"data/{p}: edited in place without raising VERSION ({old_v} -> {new_v}); "
                              "prefer a new module such as <name>_v2.py")
                continue
        archived = history_path(state_dir, Path(p).stem, new_v)
        if archived.exists() and archived.read_text(encoding="utf-8") != new_source:
            errors.append(f"data/{p}: VERSION {new_v} was already used by different code "
                          f"({archived.relative_to(state_dir)}); raise VERSION again")
    return errors


def leaf_changes(old, new, prefix=""):
    """List of (path, old, new) for changed leaves."""
    out = []
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            out += leaf_changes(a, b, f"{path}.")
        elif a != b:
            out.append((path, a, b))
    return out


def config_change_errors(old, new, trades):
    """Rules for one review's config change, given all logged trades."""
    if old == new:
        return []
    errors = []
    if new.get("version") != old["version"] + 1:
        errors.append(f"version must go from {old['version']} to {old['version'] + 1}")
    changes = [c for c in leaf_changes(old, new) if c[0] not in ("version", "strategy_version")]
    under_version = trades[trades["config_version"] == old["version"]]
    switch = (old["strategy"]["name"] != new["strategy"]["name"]
              or old["strategy_version"] != new["strategy_version"])
    if switch:
        same = trades[(trades["strategy"] == old["strategy"]["name"])
                      & (trades["strategy_version"] == old["strategy_version"])]
        days = same["date"].nunique()
        if days < STRATEGY_MIN_DAYS:
            errors.append(f"strategy change needs {STRATEGY_MIN_DAYS}+ trading days on the current strategy "
                          f"version; it has {days}")
        if old["strategy"]["name"] == new["strategy"]["name"] and new["strategy_version"] <= old["strategy_version"]:
            errors.append("strategy_version must increase")
        return errors
    if len(changes) != 1:
        errors.append(f"exactly one parameter change per review; got {[c[0] for c in changes]}")
    n, days = len(under_version), under_version["date"].nunique()
    if n < MIN_TRADES or days < MIN_DAYS:
        errors.append(f"config v{old['version']} has {n} trades over {days} days; "
                      f"needs {MIN_TRADES}+ trades and {MIN_DAYS}+ days")
    for path, a, b in changes:
        if path.startswith("risk.") and isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a:
            pnl = under_version["pnl_usd"].astype(float).sum()
            if n < MIN_TRADES or pnl <= 0:
                errors.append(f"{path} increased ({a} -> {b}) without a net-profitable stretch of "
                              f"{MIN_TRADES}+ trades (net ${pnl:,.2f} over {n})")
    return errors


def memory_entries(text):
    """{id: {"query": str | None, "since": date | None}} for every entry in a memory file."""
    blocks = MEMORY_ENTRY.split(text.replace("\r\n", "\n"))[1:]
    out = {}
    for entry_id, body in zip(blocks[::2], blocks[1::2]):
        query = re.search(r"^- Query: *`([^`]+)`", body, re.MULTILINE)
        since = re.search(r"^- Since: *(\d{4}-\d{2}-\d{2})", body, re.MULTILINE)
        out[entry_id] = {"query": query.group(1) if query else None,
                         "since": date.fromisoformat(since.group(1)) if since else None}
    return out


def evidence_errors(changes, changelog_added, base_memory, trades, today):
    """A parameter change must cite a pre-registered memory hypothesis whose query passes the evidence test.

    Risk-reducing changes are exempt.
    """
    if not changes or all(p.startswith("risk.") and isinstance(a, (int, float)) and isinstance(b, (int, float))
                          and b < a for p, a, b in changes):
        return []
    refs = sorted(set(re.findall(r"Evidence: *(M-\d+)", changelog_added)))
    if not refs:
        return ["config change needs 'Evidence: M-<id>' in today's changelog entry, citing a memory "
                "hypothesis with a Query"]
    entries = memory_entries(base_memory)
    reports = []
    for ref in refs:
        entry = entries.get(ref)
        if entry is None:
            reports.append(f"{ref}: not in memory before this review (write hypotheses down first)")
        elif not entry["query"] or not entry["since"]:
            reports.append(f"{ref}: needs '- Query: `...`' and '- Since: <date>' lines")
        elif entry["since"] >= today:
            reports.append(f"{ref}: registered today; evidence must come from a hypothesis written earlier")
        else:
            try:
                result = evidence(trades, entry["query"], entry["since"])
            except ValueError as e:
                reports.append(f"{ref}: {e}")
                continue
            if result["passed"]:
                return []
            reports.append(f"{ref}: {format_evidence(result)}")
    return ["config change evidence did not pass:\n" + "\n".join(reports)]


def watchlist_change_errors(old, new, universe, today, rules):
    """Paced, explained swaps from the current universe; history is append-only."""
    errors = []
    old_entries = {e["symbol"]: e for e in old["symbols"]}
    new_entries = {e["symbol"]: e for e in new["symbols"]}
    adds = sorted(set(new_entries) - set(old_entries))
    removes = sorted(set(old_entries) - set(new_entries))
    if len(adds) > rules["max_adds_per_review"]:
        errors.append(f"watchlist: {len(adds)} adds, max {rules['max_adds_per_review']} per review")
    if len(removes) > rules["max_removes_per_review"]:
        errors.append(f"watchlist: {len(removes)} removes, max {rules['max_removes_per_review']} per review")
    for sym in set(old_entries) & set(new_entries):
        if old_entries[sym] != new_entries[sym]:
            errors.append(f"watchlist {sym}: existing entries must not be rewritten")
    if new["removed"][:len(old["removed"])] != old["removed"]:
        errors.append("watchlist.removed: history is append-only")
    logged = {e["symbol"] for e in new["removed"][len(old["removed"]):]}
    for sym in removes:
        if sym not in logged:
            errors.append(f"watchlist: removing {sym} needs a 'removed' entry with date and reason")
    if adds:
        age = (today - date.fromisoformat(universe["generated"])).days if universe else None
        if universe is None:
            errors.append("watchlist: no data/universe.json to add symbols from")
        elif age > rules["max_universe_age_days"]:
            errors.append(f"watchlist: universe is {age} days old; adds need one under "
                          f"{rules['max_universe_age_days']} days")
    for sym in adds:
        if universe and sym not in universe["symbols"]:
            errors.append(f"watchlist: {sym} is not in the current universe")
        if new_entries[sym].get("added") != today.isoformat():
            errors.append(f"watchlist {sym}: 'added' must be today ({today})")
        for r in old["removed"]:
            if r["symbol"] == sym and (today - date.fromisoformat(r["removed"])).days < rules["readd_cooldown_days"]:
                errors.append(f"watchlist: {sym} was removed on {r['removed']}; "
                              f"wait {rules['readd_cooldown_days']} days before re-adding")
    return errors


def run(cmd, env=None):
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def agent_tests(state_dir):
    """The strategy safety tests and the agent's own tests, both run against the agent's state."""
    cmd = [sys.executable, "-m", "pytest", "-q", "-c", str(ROOT / "pytest.ini"), "-p", "quant_testing",
           "tests/test_safety_strategies.py"]
    if any(Path(state_dir, "tests").glob("test_*.py")):
        cmd.append(str(Path(state_dir, "tests")))
    return run(cmd, env={**os.environ, "QUANT_STATE_DIR": str(state_dir)})


def check(snapshot, skip_replay=False, state_dir=STATE_DIR):
    snap = Path(snapshot)
    before = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    paths = changed_paths(before, manifest(state_dir))
    errors = (path_errors(paths) + framework_errors(snap) + strategy_version_errors(snap, paths, state_dir)
              + journal_errors(before, paths))
    memory_path = Path(state_dir) / "memory.md"
    if memory_path.exists():
        errors += memory_errors(saved_text(snap, "memory.md"), memory_path.read_text(encoding="utf-8"))
    try:
        new = load_config(Path(state_dir) / "config.json", watchlist_path=Path(state_dir) / "watchlist.json")
    except ConfigError as e:
        return errors + [str(e)]
    shadow = Path(state_dir) / "config.shadow.json"
    if shadow.exists():
        try:
            load_config(shadow, watchlist_path=Path(state_dir) / "watchlist.json")
        except ConfigError as e:
            errors.append(f"config.shadow.json: {e}")
    new.pop("watchlist", None)
    old = json.loads(saved_text(snap, "config.json"))
    old.pop("watchlist", None)
    try:
        trades = read_trades_frame(Path(state_dir) / "trades.csv")
    except (KeyError, ValueError) as e:
        return errors + [f"data/trades.csv is unreadable ({e!r}); cannot check the config change"]
    errors += config_change_errors(old, new, trades)
    switched = old["strategy"]["name"] != new["strategy"]["name"] or old["strategy_version"] != new["strategy_version"]
    if not switched:
        changes = [c for c in leaf_changes(old, new) if c[0] not in ("version", "strategy_version")]
        old_log = saved_text(snap, "changelog.md").replace("\r\n", "\n")
        new_log = (Path(state_dir) / "changelog.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        added_log = new_log[len(old_log):] if new_log.startswith(old_log) else new_log
        errors += evidence_errors(changes, added_log, saved_text(snap, "memory.md"),
                                  trades[trades["config_version"] == old["version"]], date.today())
    old_watchlist = saved_text(snap, "watchlist.json")
    if old_watchlist:
        universe_path = Path(state_dir) / "universe.json"
        universe = json.loads(universe_path.read_text(encoding="utf-8")) if universe_path.exists() else None
        errors += watchlist_change_errors(json.loads(old_watchlist), load_json(Path(state_dir) / "watchlist.json"),
                                          universe, date.today(), load_json(BOUNDS_PATH)["watchlist_rules"])
    code, out = agent_tests(state_dir)
    if code != 0:
        errors.append(f"pytest failed:\n{out}")
    strategy_touched = any(STRATEGY.match(p) for p in paths)
    switched = switched or old["strategy"] != new["strategy"]
    if (strategy_touched or switched) and not skip_replay:
        code, out = run([sys.executable, "trader.py", "--replay-last", str(REPLAY_DAYS),
                         "--data-dir", str(Path(state_dir) / "replay_check")])
        if code != 0:
            errors.append(f"replay over the last {REPLAY_DAYS} days failed:\n{out}")
    return errors


def snapshot(dest, state_dir=STATE_DIR, framework=True):
    """Hash every state file and copy all but the bulky tool outputs, so revert can restore them."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps(manifest(state_dir)), encoding="utf-8")
    for p in files(state_dir):
        if not p.startswith(OUTPUT_DIRS):
            (dest / "state" / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(state_dir) / p, dest / "state" / p)
    if framework:
        (dest / "framework.json").write_text(json.dumps(sorted(framework_status())), encoding="utf-8")


def revert(snapshot, state_dir=STATE_DIR, framework=True):
    """Undo every change since the snapshot. New journal entries and tool outputs are kept as a record."""
    snap = Path(snapshot)
    before = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    for p in changed_paths(before, manifest(state_dir)):
        saved, live = snap / "state" / p, Path(state_dir) / p
        if saved.exists():
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, live)
        elif not (JOURNAL.match(p) or p.startswith(OUTPUT_DIRS)):
            live.unlink(missing_ok=True)
    if framework:
        baseline = set(json.loads((snap / "framework.json").read_text(encoding="utf-8")))
        for line in sorted(framework_status() - baseline):
            path = line[3:]
            if line.startswith("??"):
                (ROOT / path).unlink(missing_ok=True)
            else:
                subprocess.run(["git", "checkout", "--", path], cwd=ROOT, check=False)


def archive(state_dir=STATE_DIR):
    """Copy each strategy's current code to strategy_history/<name>_v<VERSION>.py if not already there.

    Trades record strategy and strategy_version, so every result maps back to the exact code.
    """
    saved = []
    for module in sorted(Path(state_dir, "strategies").glob("*.py")):
        version = module_version(module.read_text(encoding="utf-8"))
        dest = history_path(state_dir, module.stem, version)
        if isinstance(version, int) and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(module, dest)
            saved.append(dest.name)
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("dir")
    c = sub.add_parser("check")
    c.add_argument("--snapshot", required=True)
    c.add_argument("--skip-replay", action="store_true")
    r = sub.add_parser("revert")
    r.add_argument("--snapshot", required=True)
    sub.add_parser("archive")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.dir)
    elif args.cmd == "revert":
        revert(args.snapshot)
    elif args.cmd == "archive":
        for name in archive():
            print(f"archived {name}")
    else:
        errors = check(args.snapshot, args.skip_replay)
        for e in errors:
            print(f"GUARD: {e}")
        print("GUARD: ok" if not errors else f"GUARD: {len(errors)} problem(s)")
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
