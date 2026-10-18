#!/usr/bin/env bash
# Pull-based deploy, run by cron every 5 minutes (see scripts/cron.sh). Ships a new commit on
# origin/main to this machine once CI has passed for it and no job is running:
#
#   fetch -> CI passed? -> exclusive deploy lock -> fast-forward -> pip install if requirements changed
#   -> checks (incl. database migrations) -> reinstall cron; on any failed check, roll back to the
#   previous commit. Either way the deploy is recorded in the runs table of data/trader.db.
#
# It never touches data/ beyond what the checks themselves do (config key merges, database
# migrations), and restores the config and the database if it rolls back. It refuses to deploy over
# local commits or local changes.
#
# Settings, from the environment or .env:
#   NTFY_TOPIC            send deploy notifications to ntfy.sh/<topic>
#   DEPLOY_GITHUB_TOKEN   read token for private repos (fine-grained, Checks: read-only)
#   DEPLOY_CI_CHECK       "require" (default) or "skip" to deploy without checking CI
set -uo pipefail

NAME="claude-quant"
CONFIG_FILES=(data/config.json data/config.shadow.json)  # may change during checks; restored on rollback
DATABASE=data/trader.db  # migrated during checks; restored on rollback

checks() {
  "$PY" -m pytest -q &&
    "$PY" config.py &&
    "$PY" db.py migrate &&
    QUANT_STATE_DIR="$REPO/data" "$PY" -m pytest -q -p quant_testing tests/test_safety_strategies.py
}

# Consistent copy of a SQLite database through its backup API (works with the WAL file present).
sqlite_copy() { "$PY" -c 'import sqlite3, sys; s, d = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2]); s.backup(d)' "$1" "$2"; }

# The deploy as a runs row (job = deploy), and what it changed in the state as versions rows. Uses
# whichever db.py is checked out; never fails the deploy.
record_deploy() {  # exit code, file with the output
  local id
  id=$("$PY" db.py run-start deploy --started "$STARTED" --sha "$(short "$TARGET")" 2>/dev/null) &&
    "$PY" db.py run-end "$id" "$1" --output "$2" >/dev/null 2>&1
  [ "$1" = 0 ] && "$PY" db.py versions --reason deploy >/dev/null 2>&1
  return 0
}

log() { echo "$(date '+%F %T') $*"; }
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
setting() { local v="${!1:-}"; [ -n "$v" ] && echo "$v" || env_value "$1"; }
short() { git rev-parse --short "$1"; }

notify() {
  local topic
  topic=$(setting NTFY_TOPIC)
  [ -n "$topic" ] && curl -fsS -m 10 -H "Title: $NAME deploy" -d "$1" "https://ntfy.sh/$topic" >/dev/null 2>&1
  return 0
}

# Log (and optionally notify) a waiting reason once per commit, not every 5 minutes.
once() {
  local key="$TARGET $1"
  [ "$(cat data/deploy_status 2>/dev/null)" = "$key" ] && return 0
  echo "$key" > data/deploy_status
  log "$(short "$TARGET"): $1"
  [ "${2:-}" = notify ] && notify "$(short "$TARGET"): $1"
  return 0
}

ci_status() {  # success | pending | failure | unknown
  [ "$(setting DEPLOY_CI_CHECK)" = "skip" ] && { echo success; return; }
  local slug token json
  slug=$(git remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')
  token=$(setting DEPLOY_GITHUB_TOKEN)
  json=$(curl -fsS -m 20 -H "Accept: application/vnd.github+json" ${token:+-H "Authorization: Bearer $token"} \
    "https://api.github.com/repos/$slug/commits/$1/check-runs") || { echo unknown; return; }
  "$PY" -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs", [])
if not runs or any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "neutral", "skipped") for r in runs):
    print("success")
else:
    print("failure")' <<<"$json"
}

main() {
  cd "$(dirname "$0")/.." || exit 1
  REPO="$PWD"
  PY="$REPO/.venv/bin/python"
  mkdir -p data

  git fetch -q origin main || { log "git fetch failed"; exit 1; }
  local head
  head=$(git rev-parse HEAD)
  TARGET=$(git rev-parse origin/main)
  [ "$head" = "$TARGET" ] && exit 0
  [ "$(cat data/deploy_failed 2>/dev/null)" = "$TARGET" ] && exit 0  # failed before; wait for a new commit

  if ! git merge-base --is-ancestor HEAD origin/main; then
    once "not deploying: this checkout has commits that are not on origin/main" notify; exit 1
  fi
  if [ -n "$(git status --porcelain)" ]; then
    once "not deploying: the checkout has local changes ($(git status --porcelain | wc -l) files)" notify; exit 1
  fi
  case "$(ci_status "$TARGET")" in
    success) ;;
    pending) once "waiting for CI"; exit 0 ;;
    failure) echo "$TARGET" > data/deploy_failed; once "not deploying: CI failed" notify; exit 1 ;;
    *) once "not deploying: cannot read the CI result (private repo? set DEPLOY_GITHUB_TOKEN)" notify; exit 1 ;;
  esac

  exec 9>data/deploy.lock
  flock -n -x 9 || { once "waiting for a running job to finish"; exit 0; }

  STARTED=$(TZ=America/New_York date '+%FT%T%:z')
  log "deploying $(short "$head") -> $(short "$TARGET"): $(git log -1 --format=%s "$TARGET")"
  local saved reqs_changed=0
  saved=$(mktemp -d)
  for f in "${CONFIG_FILES[@]}"; do [ -f "$f" ] && mkdir -p "$saved/$(dirname "$f")" && cp -p "$f" "$saved/$f"; done
  if [ -f "$DATABASE" ] && ! sqlite_copy "$DATABASE" "$saved/trader.db"; then
    log "could not back up $DATABASE; not deploying"; rm -rf "$saved"; exit 1
  fi
  git diff --quiet "$head" "$TARGET" -- requirements.txt || reqs_changed=1
  git merge -q --ff-only "$TARGET"
  [ "$reqs_changed" = 1 ] && "$PY" -m pip install -q -r requirements.txt

  if checks > data/deploy_checks.log 2>&1; then
    scripts/cron.sh install
    echo "$TARGET" > data/deployed
    rm -f data/deploy_status data/deploy_failed
    log "deployed $(short "$TARGET")"
    record_deploy 0 data/deploy_checks.log
    notify "deployed $(short "$TARGET"): $(git log -1 --format=%s "$TARGET")"
  else
    git reset -q --hard "$head"
    for f in "${CONFIG_FILES[@]}"; do [ -f "$saved/$f" ] && cp -p "$saved/$f" "$f"; done
    if [ -f "$saved/trader.db" ]; then
      sqlite_copy "$saved/trader.db" "$DATABASE" || log "WARNING: could not restore $DATABASE from the pre-deploy copy"
    else
      rm -f "$DATABASE" "$DATABASE-wal" "$DATABASE-shm"  # created by the failed deploy; the next one re-imports
    fi
    [ "$reqs_changed" = 1 ] && "$PY" -m pip install -q -r requirements.txt
    scripts/cron.sh install
    echo "$TARGET" > data/deploy_failed
    log "checks failed on $(short "$TARGET"); rolled back to $(short "$head"). Last lines of data/deploy_checks.log:"
    tail -n 15 data/deploy_checks.log
    record_deploy 1 data/deploy_checks.log
    notify "deploy of $(short "$TARGET") failed its checks on the Pi; rolled back to $(short "$head")"
  fi
  rm -rf "$saved"
}

main "$@"
exit
