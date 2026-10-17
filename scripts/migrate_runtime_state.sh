#!/usr/bin/env bash
# One-time migration of an existing install to the framework / runtime-state split, where the
# agent's config, watchlist, strategies, tests, memory, changelog and journal live in the gitignored
# data/ folder and the nightly review never commits.
#
# Pulling the split deletes the agent's tracked files from the working tree, so run this instead
# of pulling, from the repo root, outside market hours:
#
#   git fetch && git show origin/main:scripts/migrate_runtime_state.sh > /tmp/migrate.sh && bash /tmp/migrate.sh
#
# It backs everything up first, resets the checkout to origin/main (dropping the review's local
# commits, which are kept in the backup), then moves the agent's files into data/.
set -euo pipefail

[ -f trader.py ] && [ -d .git ] || { echo "Run this from the repo root."; exit 1; }
[ -f data/config.json ] && { echo "data/config.json already exists; this install is already migrated."; exit 1; }
git cat-file -e origin/main:defaults/config.json 2>/dev/null \
  || { echo "origin/main does not have the runtime-state split yet. Run 'git fetch' first."; exit 1; }
mkdir -p data
if command -v flock >/dev/null && [ -e data/trader.lock ] && ! flock -n data/trader.lock true; then
  echo "The trader is running. Run this after the session ends."; exit 1
fi

backup="$HOME/paper-trader-backup-$(date +%F-%H%M%S)"
echo "==> Backing up to $backup"
mkdir -p "$backup"
for p in config.json watchlist.json config.shadow.json strategies tests data; do
  [ -e "$p" ] && cp -a "$p" "$backup/"
done
git log -p --format='commit %H%n%ad %s' -- config.json watchlist.json config.shadow.json strategies tests data \
  > "$backup/review-history.patch"
git rev-parse HEAD > "$backup/old-head.txt"
echo "OK: $(du -sh "$backup" | cut -f1)"

echo "==> Resetting the checkout to origin/main"
git reset -q --hard origin/main
git log -1 --format='OK: now at %h %s'

echo "==> Moving the agent's files into data/"
cp -a "$backup/data/." data/
for f in config.json watchlist.json config.shadow.json; do
  [ -f "$backup/$f" ] && cp -a "$backup/$f" "data/$f" && echo "  data/$f"
done
mkdir -p data/strategies data/tests
for f in "$backup"/strategies/*.py; do
  name=$(basename "$f")
  [ "$name" = "__init__.py" ] || [ "$name" = "base.py" ] && continue
  cp -a "$f" "data/strategies/$name"
  if ! grep -q '^INTERFACE *=' "data/strategies/$name"; then
    sed -i 's/^\(VERSION *= *[0-9]\+\)$/\1\nINTERFACE = 1/' "data/strategies/$name"
  fi
  echo "  data/strategies/$name"
done
for f in "$backup"/tests/test_*.py; do
  name=$(basename "$f")
  [ -e "tests/$name" ] && continue  # still a framework test
  cp -a "$f" "data/tests/$name"
  echo "  data/tests/$name"
done

echo "==> Checking"
py=.venv/bin/python
"$py" config.py
"$py" review/guard.py archive
"$py" -m pytest -q || echo "WARNING: framework tests failed; check the checkout before the next session."
QUANT_STATE_DIR="$PWD/data" "$py" -m pytest -q -p quant_testing tests/test_safety_strategies.py data/tests \
  || echo "WARNING: the agent's tests fail against its own state (often a test that hard-codes a tuned value). The next review must fix them before the guard accepts any change."
[ -z "$(git status --porcelain)" ] && echo "OK: checkout is clean" || { echo "Unexpected local changes:"; git status --short; }

echo
echo "Done. Backup: $backup (delete it once you are happy)."
echo "Cron entries are unchanged. Remove the review's git identity if you like: git config --unset user.name"
