#!/usr/bin/env bash
# Run a scheduled job under a shared lock on data/deploy.lock, and record it in the runs table of
# data/trader.db (db.py run-start / run-end). A deploy takes the lock exclusively, so it never swaps
# code under a running job, and a job that starts during a deploy waits for it. Recording never
# stops a job: if the database cannot be written, the job runs as before.
#
#   [JOB_NAME=name] scripts/job.sh <command> [args...]
#
# JOB_NAME names the job in the runs table (cron.sh sets it); without it the command line is used.
# The job's output still goes wherever the caller sends it; when the job fails, its last lines are
# kept as the run's error.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
exec 9>>data/deploy.lock
flock -s 9

PY=.venv/bin/python
job=${JOB_NAME:-$*}
job=${job//$PWD\//}
id=$("$PY" db.py run-start "$job" --pid $$ 2>/dev/null) || id=""
out=$(mktemp)
"$@" > >(tee "$out") 2>&1
code=$?
wait $! 2>/dev/null  # let tee finish writing
[ -n "$id" ] && "$PY" db.py run-end "$id" "$code" --output "$out" >/dev/null 2>&1
rm -f "$out"
exit "$code"
