#!/usr/bin/env bash
# Nightly review: headless Claude Code reads the results and may tune config.json or add a strategy.
# Every change is re-checked by review/guard.py afterwards and reverted if it breaks a rule.
#
#   review/run_review.sh                         review, auto-commit changes that pass the guard
#   REVIEW_MODE=propose review/run_review.sh     review, write proposals to the changelog only
set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.local/bin:$PATH"
if [ -x .venv/Scripts/python.exe ]; then
  export PATH="$PWD/.venv/Scripts:$PATH"
elif [ -x .venv/bin/python ]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi

today=$(date +%F)
mkdir -p data/reports
logfile="data/reports/review_$today.log"
exec >>"$logfile" 2>&1
echo "=== review started $(date)"

if [ -n "$(git status --porcelain -- . ':!data')" ]; then
  echo "uncommitted changes outside data/; commit or stash them first. aborting."
  exit 1
fi

if python lifetime.py > /dev/null; then
  git add data/lifetime.md
  git diff --cached --quiet -- data/lifetime.md || git commit -q -m "lifetime stats $today" -- data/lifetime.md
fi

base=$(git rev-parse HEAD)
if ! python review/universe.py; then
  echo "universe screen failed; watchlist adds will be refused if the old one is stale"
fi
snapshot=$(mktemp -d)
python review/guard.py snapshot "$snapshot"

prompt="$(cat review/REVIEW_PROMPT.md)"
if [ "${REVIEW_MODE:-commit}" = "propose" ]; then
  prompt="$prompt

PROPOSAL MODE: do not edit config.json, watchlist.json, config.shadow.json or anything under strategies/ or tests/,
and do not commit. Write any change you would make under a 'Proposed change' heading in today's
changelog entry instead."
fi

claude -p "$prompt" \
  --allowedTools "Read" "Glob" "Grep" \
    "Edit(config.json)" "Edit(watchlist.json)" "Edit(config.shadow.json)" "Write(config.shadow.json)" "Edit(data/changelog.md)" \
    "Edit(data/memory.md)" "Write(data/journal/**)" \
    "Edit(strategies/**)" "Write(strategies/**)" "Edit(tests/**)" "Write(tests/**)" \
    "Bash(python analyze.py:*)" "Bash(python config.py)" "Bash(python -m pytest:*)" \
    "Bash(python trader.py --replay:*)" "Bash(python trader.py --replay-last:*)" \
    "Bash(python review/guard.py check:*)" "Bash(python review/day_report.py:*)" "Bash(date:*)" \
    "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" \
    "Bash(git add:*)" "Bash(git commit:*)" "Bash(git checkout -- :*)"
echo "=== claude exited with $?"

if python review/guard.py check --base "$base" --snapshot "$snapshot"; then
  message="review $today: notes"
else
  python review/guard.py revert --base "$base" --snapshot "$snapshot"
  printf '\n## %s (guard)\n\nThe automated guard rejected this review'"'"'s changes and reverted them. See %s.\n' \
    "$today" "$logfile" >> data/changelog.md
  message="review $today: reverted changes that failed the guard"
fi

paths=(config.json watchlist.json data/changelog.md data/memory.md strategies tests)
[ -d data/journal ] && paths+=(data/journal)
[ -e config.shadow.json ] || git ls-files --error-unmatch config.shadow.json >/dev/null 2>&1 && paths+=(config.shadow.json)
git add -A -- "${paths[@]}"
if ! git diff --cached --quiet; then
  git commit -q -m "$message"
fi
rm -rf "$snapshot"
echo "=== review finished $(date)"
