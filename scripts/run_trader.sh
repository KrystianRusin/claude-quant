#!/usr/bin/env bash
# Start today's live paper session, plus the shadow dry run when config.shadow.json exists.
cd "$(dirname "$0")/.."
py=.venv/bin/python
[ -x "$py" ] || py=.venv/Scripts/python.exe
if [ -f config.shadow.json ]; then
  "$py" trader.py --dry-run --config config.shadow.json --data-dir data/shadow >/dev/null 2>&1 &
fi
exec "$py" trader.py
