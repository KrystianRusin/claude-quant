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
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import ConfigError, load_config  # noqa: E402
from logger import read_trades_frame  # noqa: E402

MIN_TRADES = 30
MIN_DAYS = 10
STRATEGY_MIN_DAYS = 10
REPLAY_DAYS = 10
DATA_FILES = ["data/trades.csv", "data/orders.csv", "data/daily_summary.csv"]
EDITABLE_FILES = {"config.json", "config.shadow.json", "data/changelog.md"}
PROTECTED_STRATEGY_FILES = {"strategies/__init__.py", "strategies/base.py"}


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


def changed_paths(base):
    """Tracked files that differ from `base` (committed or not) plus new untracked files."""
    tracked = git("diff", "--name-only", base).split()
    untracked = git("ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


def path_allowed(path):
    if path in EDITABLE_FILES or path.startswith("data/reports/"):
        return True
    if path.startswith("strategies/") and path.endswith(".py"):
        return path not in PROTECTED_STRATEGY_FILES and path.count("/") == 1
    if path.startswith("tests/test_") and path.endswith(".py"):
        return not path.startswith("tests/test_safety")
    return False


def path_errors(paths):
    return [f"{p}: the reviewer may not change this file" for p in paths if not path_allowed(p)]


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
    """List of (path, old, new) for changed leaves; watchlist adds/removes count one each."""
    out = []
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        a, b = old.get(key), new.get(key)
        if path == "watchlist" and isinstance(a, list) and isinstance(b, list):
            out += [(f"watchlist+{s}", None, s) for s in b if s not in a]
            out += [(f"watchlist-{s}", s, None) for s in a if s not in b]
        elif isinstance(a, dict) and isinstance(b, dict):
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
        errors.append(f"exactly one parameter or one watchlist add/remove per review; got {[c[0] for c in changes]}")
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


def run(cmd):
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def check(base, snapshot=None, skip_replay=False):
    paths = changed_paths(base)
    errors = path_errors(paths) + strategy_version_errors(base, paths)
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
    old = json.loads(git("show", f"{base}:config.json"))
    trades = read_trades_frame(ROOT / "data/trades.csv")
    errors += config_change_errors(old, new, trades)
    code, out = run([sys.executable, "-m", "pytest", "-q"])
    if code != 0:
        errors.append(f"pytest failed:\n{out}")
    strategy_touched = any(p.startswith("strategies/") for p in paths)
    switched = old["strategy"] != new["strategy"] or old["strategy_version"] != new["strategy_version"]
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
    before = set((Path(snap) / "untracked.txt").read_text(encoding="utf-8").split())
    for p in git("diff", "--name-only", base).split():
        if subprocess.run(["git", "cat-file", "-e", f"{base}:{p}"], cwd=ROOT).returncode == 0:
            git("checkout", base, "--", p)
        else:
            git("rm", "-q", "-f", "--", p, check=False)
            (ROOT / p).unlink(missing_ok=True)
    for p in git("ls-files", "--others", "--exclude-standard").split():
        if p not in before:
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
