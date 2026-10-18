#!/usr/bin/env bash
# The cron schedule, in one place. setup.sh installs it once and deploy.sh reinstalls it after every
# deploy, so schedule changes ship like code. Lines are tagged and replace only this repo's entries.
#
#   scripts/cron.sh install    replace this repo's cron entries
#   scripts/cron.sh print      show them
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
SECRETS="$HOME/.config/paper-trader/env"
CRON_TAG="# paper-trader"

lines="0 9 * * 1-5 $REPO/scripts/job.sh $REPO/scripts/run_trader.sh >> $REPO/data/cron.log 2>&1 $CRON_TAG
0 17 * * 1-5 . $SECRETS; $REPO/scripts/job.sh $REPO/review/run_review.sh $CRON_TAG
0 3 * * * $REPO/scripts/job.sh $REPO/.venv/bin/python $REPO/db.py backup >> $REPO/data/cron.log 2>&1 $CRON_TAG
*/5 * * * * $REPO/scripts/deploy.sh >> $REPO/data/deploy.log 2>&1 $CRON_TAG"

case "${1:-}" in
  print) echo "$lines" ;;
  install)
    existing=$(crontab -l 2>/dev/null || true)
    new=$({ grep -vF "$CRON_TAG" <<<"$existing" || true; echo "$lines"; })
    [ "$new" = "$existing" ] || echo "$new" | crontab -
    ;;
  *) echo "usage: scripts/cron.sh install|print"; exit 2 ;;
esac
