#!/usr/bin/env bash
# Run a scheduled job under a shared lock on data/deploy.lock. A deploy takes the lock exclusively,
# so it never swaps code under a running job, and a job that starts during a deploy waits for it.
#
#   scripts/job.sh <command> [args...]
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
exec flock -s data/deploy.lock "$@"
