#!/usr/bin/env bash
# One-time setup on a Raspberry Pi (64-bit Raspberry Pi OS) or other Debian/Ubuntu machine.
# Safe to re-run: each step checks what is already done.
#
#   scripts/setup.sh
#
# Installs system packages, the Python venv and packages, the Claude Code CLI, writes .env
# with your Alpaca paper keys, stores a Claude token for the nightly review, sets the
# timezone, runs the tests and the Alpaca connection check, and installs the cron jobs.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
SECRETS_DIR="$HOME/.config/paper-trader"
SECRETS="$SECRETS_DIR/env"
CRON_TAG="# paper-trader"

step() { printf '\n==> %s\n' "$*"; }
ask() { local reply; read -r -p "$1 [y/N] " reply; [[ "$reply" =~ ^[Yy]$ ]]; }

step "Checking platform"
if [ "$(uname -s)" != "Linux" ]; then
  echo "This script is for Linux. On Windows, follow the README instead."; exit 1
fi
arch=$(uname -m)
if [ "$arch" != "aarch64" ] && [ "$arch" != "x86_64" ]; then
  echo "Need a 64-bit OS (found $arch). Flash Raspberry Pi OS Lite (64-bit)."; exit 1
fi
mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
[ "$mem_mb" -lt 1800 ] && echo "Warning: ${mem_mb} MB RAM. The Claude CLI may struggle below 2 GB."
echo "OK: $arch, ${mem_mb} MB RAM"

step "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "Python 3.11+ required, found $(python3 --version). Use Raspberry Pi OS Bookworm or newer."; exit 1; }
echo "OK: $(python3 --version)"

step "Timezone"
tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone)
if [ "$tz" = "America/Toronto" ] || [ "$tz" = "America/New_York" ]; then
  echo "OK: $tz"
elif ask "Timezone is $tz. Cron times assume Eastern. Set it to America/Toronto?"; then
  sudo timedatectl set-timezone America/Toronto
  echo "OK: America/Toronto"
else
  echo "Left as $tz. Adjust the cron hours yourself so they match 09:00 and 17:00 ET."
fi

step "Python virtual environment and packages"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
echo "OK"

step "Alpaca paper keys (.env)"
if [ -f .env ] && grep -q '^ALPACA_API_KEY=.\+' .env && grep -q '^ALPACA_SECRET_KEY=.\+' .env; then
  echo "OK: .env already has keys"
else
  echo "Paste your Alpaca PAPER keys (input is hidden)."
  read -r -s -p "ALPACA_API_KEY: " key; echo
  read -r -s -p "ALPACA_SECRET_KEY: " secret; echo
  [ -n "$key" ] && [ -n "$secret" ] || { echo "Both keys are required."; exit 1; }
  umask 077
  printf 'ALPACA_API_KEY=%s\nALPACA_SECRET_KEY=%s\n' "$key" "$secret" > .env
  unset key secret
  echo "OK: wrote .env (readable only by you)"
fi

step "Claude Code CLI"
export PATH="$HOME/.local/bin:$PATH"
if command -v claude >/dev/null; then
  echo "OK: $(claude --version 2>/dev/null | head -1)"
else
  echo "Installing with Anthropic's native installer"
  curl -fsSL https://claude.ai/install.sh | bash
  command -v claude >/dev/null || { echo "claude not found after install; check ~/.local/bin"; exit 1; }
fi

step "Claude token for the nightly review"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
if [ -f "$SECRETS" ] && grep -q '^export CLAUDE_CODE_OAUTH_TOKEN=.\+' "$SECRETS"; then
  echo "OK: token already stored in $SECRETS"
else
  echo "Next, 'claude setup-token' prints a sign-in link. Open it on any device, sign in with your"
  echo "Claude subscription, and copy the token it prints."
  claude setup-token || true
  read -r -s -p "Paste the token (input is hidden): " token; echo
  [ -n "$token" ] || { echo "No token given."; exit 1; }
  umask 077
  printf 'export CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token" > "$SECRETS"
  unset token
  echo "OK: stored in $SECRETS (readable only by you)"
fi
# shellcheck disable=SC1090
. "$SECRETS"
if claude -p "reply with ok" 2>&1 | grep -qi '\bok\b'; then
  echo "OK: Claude CLI answers"
else
  echo "Warning: 'claude -p' did not answer ok. Check the token, then re-run this script."
fi

step "Git identity (the nightly review commits config changes)"
if git config user.name >/dev/null && git config user.email >/dev/null; then
  echo "OK: $(git config user.name) <$(git config user.email)>"
else
  read -r -p "git user.name: " gname
  read -r -p "git user.email: " gemail
  git config user.name "$gname"
  git config user.email "$gemail"
fi
git rev-parse HEAD >/dev/null 2>&1 || echo "Warning: no commits yet. The review needs at least one commit."

step "Tests"
.venv/bin/python -m pytest -q

step "Alpaca connection check"
.venv/bin/python broker.py

step "Cron jobs (trader 09:00, review 17:00, weekdays)"
mkdir -p data
cron_lines="0 9 * * 1-5 $REPO/scripts/run_trader.sh >> $REPO/data/cron.log 2>&1 $CRON_TAG
0 17 * * 1-5 . $SECRETS; $REPO/review/run_review.sh $CRON_TAG"
existing=$(crontab -l 2>/dev/null || true)
if grep -qF "$CRON_TAG" <<<"$existing" && ! ask "Cron jobs already installed. Replace them?"; then
  echo "Left existing cron jobs unchanged"
else
  { grep -vF "$CRON_TAG" <<<"$existing" || true; echo "$cron_lines"; } | crontab -
  echo "OK: installed"
fi
crontab -l | grep -F "$CRON_TAG"

step "Done"
echo "The trader runs weekdays at 09:00 and the review at 17:00 (local time)."
echo "Logs: data/trader.log, data/cron.log, data/reports/review_<date>.log"
echo "If the Windows scheduled tasks exist, delete them so only one machine trades."
