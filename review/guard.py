"""Code-level checks on what a nightly review changed. Used by run_review.sh.

python review/guard.py snapshot DIR                 save data files and untracked list before the review
python review/guard.py check --base REV [--snapshot DIR] [--skip-replay]
                                                    exit 1 and list problems if the changes break a rule
python review/guard.py revert --base REV --snapshot DIR
                                                    undo every change made since REV
"""
import argparse
import ast
import json
import re
from datetime import date
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import BOUNDS_PATH, ConfigError, load_config, load_json  # noqa: E402
from analyze import evidence, format_evidence  # noqa: E402
from logger import read_trades_frame  # noqa: E402

MIN_TRADES = 30
MIN_DAYS = 10
STRATEGY_MIN_DAYS = 10
REPLAY_DAYS = 10
DATA_FILES = ["data/trades.csv", "data/orders.csv", "data/daily_summary.csv", "data/universe.json"]
EDITABLE_FILES = {"config.json", "config.shadow.json", "watchlist.json", "data/changelog.md", "data/memory.md"}
JOURNAL = re.compile(r"^data/journal/\d{4}-\d{2}-\d{2}\.md$")
MEMORY_ENTRY = re.compile(r"^### (M-\d+):", re.MULTILINE)
MEMORY_STATUSES = {"hypothesis", "supported", "confirmed", "retired"}
MAX_ACTIVE_MEMORIES = 40
MAX_MEMORY_BYTES = 60_000
PROTECTED_STRATEGY_FILES = {"strategies/__init__.py", "strategies/base.py"}


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


def changed_paths(base):
    """Tracked files that differ from `base` (committed or not) plus new untracked files."""
    tracked = git("diff", "--name-only", base).split()
    untracked = git("ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


def path_allowed(path):
    if path in EDITABLE_FILES or path.startswith("data/reports/") or JOURNAL.match(path):
        return True
    if path.startswith("strategies/") and path.endswith(".py"):
        return path not in PROTECTED_STRATEGY_FILES and path.count("/") == 1
    if path.startswith("tests/test_") and path.endswith(".py"):
        return not path.startswith("tests/test_safety")
    return False


def path_errors(paths):
    return [f"{p}: the reviewer may not change this file" for p in paths if not path_allowed(p)]


def exists_at(base, path):
    return subprocess.run(["git", "cat-file", "-e", f"{base}:{path}"], cwd=ROOT,
                          capture_output=True).returncode == 0


def journal_errors(base, paths):
    """Past journal entries are a record: they may be added, never edited."""
    return [f"{p}: past journal entries must not be edited"
            for p in paths if JOURNAL.match(p) and exists_at(base, p)]


def memory_errors(old_text, new_text):
    """Entries are never deleted, the active list stays small, and every entry has a valid status."""
    errors = []
    if len(new_text.encode("utf-8")) > MAX_MEMORY_BYTES:
        errors.append(f"data/memory.md is over {MAX_MEMORY_BYTES} bytes; merge or retire entries")
    missing = sorted(set(MEMORY_ENTRY.findall(old_text)) - set(MEMORY_ENTRY.findall(new_text)))
    if missing:
        errors.append(f"data/memory.md: entries deleted instead of retired: {missing}")
    active = new_text.split("## Retired")[0]
    n_active = len(MEMORY_ENTRY.findall(active))
    if n_active > MAX_ACTIVE_MEMORIES:
        errors.append(f"data/memory.md: {n_active} active entries, max {MAX_ACTIVE_MEMORIES}")
    blocks = MEMORY_ENTRY.split(new_text)[1:]
    for entry_id, body in zip(blocks[::2], blocks[1::2]):
        status = re.search(r"^- Status: *(\w+)", body, re.MULTILINE)
        if not status or status.group(1) not in MEMORY_STATUSES:
            errors.append(f"data/memory.md {entry_id}: needs '- Status:' with one of {sorted(MEMORY_STATUSES)}")
    return errors


def module_version(source):
    """VERSION = <int> from a strategy module's source, or None."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "VERSION" for t in node.targets):
            return getattr(node.value, "value", None)
    return None


def strategy_version_errors(base, paths):
    """An edited existing strategy module must raise its VERSION."""
    errors = []
    for p in paths:
        if not (p.startswith("strategies/") and p.endswith(".py")) or p in PROTECTED_STRATEGY_FILES:
            continue
        old = subprocess.run(["git", "show", f"{base}:{p}"], cwd=ROOT, capture_output=True, text=True)
        if old.returncode != 0 or not (ROOT / p).exists():
            continue
        old_v, new_v = module_version(old.stdout), module_version((ROOT / p).read_text(encoding="utf-8"))
        if not (isinstance(old_v, int) and isinstance(new_v, int) and new_v > old_v):
            errors.append(f"{p}: edited in place without raising VERSION ({old_v} -> {new_v}); "
                          "prefer a new module such as <name>_v2.py")
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


def run(cmd):
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def check(base, snapshot=None, skip_replay=False):
    paths = changed_paths(base)
    errors = path_errors(paths) + strategy_version_errors(base, paths) + journal_errors(base, paths)
    memory_path = ROOT / "data/memory.md"
    if memory_path.exists():
        old_memory = subprocess.run(["git", "show", f"{base}:data/memory.md"], cwd=ROOT,
                                    capture_output=True, text=True).stdout
        errors += memory_errors(old_memory, memory_path.read_text(encoding="utf-8"))
    if snapshot:
        for f in DATA_FILES:
            saved, live = Path(snapshot) / Path(f).name, ROOT / f
            if saved.exists() != live.exists() or (saved.exists() and saved.read_bytes() != live.read_bytes()):
                errors.append(f"{f}: data files must not be modified by the review")
    try:
        new = load_config()
    except ConfigError as e:
        return errors + [str(e)]
    if (ROOT / "config.shadow.json").exists():
        try:
            load_config(ROOT / "config.shadow.json")
        except ConfigError as e:
            errors.append(f"config.shadow.json: {e}")
    new.pop("watchlist", None)
    old = json.loads(git("show", f"{base}:config.json"))
    old.pop("watchlist", None)
    trades = read_trades_frame(ROOT / "data/trades.csv")
    errors += config_change_errors(old, new, trades)
    switched = old["strategy"]["name"] != new["strategy"]["name"] or old["strategy_version"] != new["strategy_version"]
    if not switched:
        changes = [c for c in leaf_changes(old, new) if c[0] not in ("version", "strategy_version")]
        old_log = subprocess.run(["git", "show", f"{base}:data/changelog.md"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.replace("\r\n", "\n")
        new_log = (ROOT / "data/changelog.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        added_log = new_log[len(old_log):] if new_log.startswith(old_log) else new_log
        base_memory = subprocess.run(["git", "show", f"{base}:data/memory.md"], cwd=ROOT,
                                     capture_output=True, text=True).stdout
        errors += evidence_errors(changes, added_log, base_memory,
                                  trades[trades["config_version"] == old["version"]], date.today())
    if exists_at(base, "watchlist.json"):
        universe_path = ROOT / "data/universe.json"
        universe = json.loads(universe_path.read_text(encoding="utf-8")) if universe_path.exists() else None
        errors += watchlist_change_errors(json.loads(git("show", f"{base}:watchlist.json")),
                                          load_json(ROOT / "watchlist.json"), universe, date.today(),
                                          load_json(BOUNDS_PATH)["watchlist_rules"])
    code, out = run([sys.executable, "-m", "pytest", "-q"])
    if code != 0:
        errors.append(f"pytest failed:\n{out}")
    strategy_touched = any(p.startswith("strategies/") for p in paths)
    switched = switched or old["strategy"] != new["strategy"]
    if (strategy_touched or switched) and not skip_replay:
        code, out = run([sys.executable, "trader.py", "--replay-last", str(REPLAY_DAYS),
                         "--data-dir", "data/replay_check"])
        if code != 0:
            errors.append(f"replay over the last {REPLAY_DAYS} days failed:\n{out}")
    return errors


def snapshot(dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for f in DATA_FILES:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, dest / Path(f).name)
    (dest / "untracked.txt").write_text(git("ls-files", "--others", "--exclude-standard"), encoding="utf-8")


def revert(base, snap):
    """Undo every change since `base`, except a newly added journal entry, which is kept as a record."""
    before = set((Path(snap) / "untracked.txt").read_text(encoding="utf-8").split())
    for p in git("diff", "--name-only", base).split():
        if exists_at(base, p):
            git("checkout", base, "--", p)
        elif not JOURNAL.match(p):
            git("rm", "-q", "-f", "--", p, check=False)
            (ROOT / p).unlink(missing_ok=True)
    for p in git("ls-files", "--others", "--exclude-standard").split():
        if p not in before and not JOURNAL.match(p):
            (ROOT / p).unlink(missing_ok=True)
    for f in DATA_FILES:
        saved = Path(snap) / Path(f).name
        if saved.exists():
            shutil.copy2(saved, ROOT / f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("dir")
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--snapshot")
    c.add_argument("--skip-replay", action="store_true")
    r = sub.add_parser("revert")
    r.add_argument("--base", required=True)
    r.add_argument("--snapshot", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.dir)
    elif args.cmd == "revert":
        revert(args.base, args.snapshot)
    else:
        errors = check(args.base, args.snapshot, args.skip_replay)
        for e in errors:
            print(f"GUARD: {e}")
        print("GUARD: ok" if not errors else f"GUARD: {len(errors)} problem(s)")
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
