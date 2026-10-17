#!/usr/bin/env bash
# Nightly review: headless Claude Code reads the results and may tune the config, swap watchlist
# symbols or write strategies. Everything it touches lives in data/ (gitignored); nothing is committed.
# Every change is re-checked by review/guard.py against a snapshot taken first, and reverted if it
# breaks a rule.
#
#   review/run_review.sh                         review, keep changes that pass the guard
#   REVIEW_MODE=propose review/run_review.sh     review, write proposals to the changelog only
set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.local/bin:$PATH"
if [ -x .venv/Scripts/python.exe ]; then
  export PATH="$PWD/.venv/Scripts:$PATH"
elif [ -x .venv/bin/python ]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi
export QUANT_STATE_DIR="$PWD/data"

today=$(date +%F)
mkdir -p data/reports
logfile="data/reports/review_$today.log"
exec >>"$logfile" 2>&1
echo "=== review started $(date)"

if [ -n "$(git status --porcelain)" ]; then
  echo "the framework checkout has local changes; it must match origin/main. aborting."
  git status --short
  exit 1
fi

python config.py || { echo "state is invalid; aborting."; exit 1; }
python review/guard.py archive
python lifetime.py > /dev/null || echo "lifetime stats failed"
if ! python review/universe.py; then
  echo "universe screen failed; watchlist adds will be refused if the old one is stale"
fi
snapshot=$(mktemp -d)
python review/guard.py snapshot "$snapshot"
export REVIEW_SNAPSHOT="$snapshot"

prompt="$(cat review/REVIEW_PROMPT.md)"
if [ "${REVIEW_MODE:-apply}" = "propose" ]; then
  prompt="$prompt

PROPOSAL MODE: do not edit data/config.json, data/watchlist.json, data/config.shadow.json or anything
under data/strategies/ or data/tests/. Write any change you would make under a 'Proposed change'
heading in today's changelog entry instead."
fi

claude -p "$prompt" \
  --allowedTools "Read" "Glob" "Grep" \
    "Edit(data/config.json)" "Edit(data/watchlist.json)" "Edit(data/config.shadow.json)" "Write(data/config.shadow.json)" \
    "Edit(data/changelog.md)" "Edit(data/memory.md)" "Write(data/journal/**)" \
    "Edit(data/strategies/**)" "Write(data/strategies/**)" "Edit(data/tests/**)" "Write(data/tests/**)" \
    "Bash(python analyze.py:*)" "Bash(python config.py)" "Bash(python -m pytest:*)" \
    "Bash(python trader.py --replay:*)" "Bash(python trader.py --replay-last:*)" \
    "Bash(python review/guard.py check:*)" "Bash(python review/day_report.py:*)" "Bash(date:*)"
echo "=== claude exited with $?"

if python review/guard.py check --snapshot "$snapshot"; then
  python review/guard.py archive
  echo "=== guard passed; changes kept"
else
  python review/guard.py revert --snapshot "$snapshot"
  printf '\n## %s (guard)\n\nThe automated guard rejected this review'"'"'s changes and reverted them. See %s.\n' \
    "$today" "$logfile" >> data/changelog.md
  echo "=== guard failed; changes reverted"
fi
rm -rf "$snapshot"
echo "=== review finished $(date)"
