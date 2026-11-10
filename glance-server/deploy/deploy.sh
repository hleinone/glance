#!/usr/bin/env bash
# Deploy glance-server to the Raspberry Pi and (re)start it.
#
#   deploy/deploy.sh pi@raspberrypi.local   (normally via `mise run deploy`)
#
# Copies the code (not .venv or cache), plus config.local.toml with the
# Digitransit key, into ~/glance-server on the Pi; creates a venv that uses
# Debian's Pillow (nothing to compile on a Pi 1); installs and restarts the
# systemd user service. No sudo needed, after this one-time setup on the Pi:
#
#   sudo apt install python3-pil python3-venv rsync && sudo loginctl enable-linger $USER
#
# Extra ssh options can be passed in GLANCE_SSH.
set -euo pipefail

target=${1:?usage: deploy/deploy.sh user@host}
ssh_cmd="ssh -4 ${GLANCE_SSH:-}"
cd "$(dirname "$0")/.."

rsync -az --delete -e "$ssh_cmd" \
  --exclude .venv --exclude cache --exclude __pycache__ \
  --exclude /glance.png --exclude config.local.toml --exclude mise.local.toml \
  ./ "$target:glance-server/"
if [ -f config.local.toml ]; then
  # Holds the key: readable by the owner only. (macOS's rsync has no --chmod.)
  $ssh_cmd "$target" 'umask 077 && cat > glance-server/config.local.toml.new && chmod 600 glance-server/config.local.toml.new && mv glance-server/config.local.toml.new glance-server/config.local.toml' < config.local.toml
fi

$ssh_cmd "$target" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/glance-server
if ! python3 -c "import PIL, venv, ensurepip" 2>/dev/null; then
  echo "One-time setup needed on the Pi:  sudo apt install python3-pil python3-venv" >&2
  exit 1
fi
# A venv is complete once it has pip; a half-made one (from a run before
# python3-venv was installed) is made again.
[ -x .venv/bin/pip ] || { rm -rf .venv; python3 -m venv --system-site-packages .venv; }
.venv/bin/pip install --quiet -r requirements.txt
# The service's sandbox makes this folder read-only to it, so it can't save
# compiled modules itself and would compile them at every start.
.venv/bin/python -m compileall -q glance

mkdir -p cache  # the service's sandbox binds it writable, so it must exist
# Before stopping the running service: can the new code load this config and
# draw an image with it? (No network, nothing written.) If not, stop here and
# leave the old service running.
if ! .venv/bin/python -m glance render --offline -o /dev/null; then
  echo "The new code or config.local.toml does not work (see above); the service was not restarted." >&2
  exit 1
fi
mkdir -p ~/.config/systemd/user
sed "s|@DIR@|$HOME/glance-server|g" deploy/glance.service > ~/.config/systemd/user/glance.service
systemctl --user daemon-reload
systemctl --user enable --quiet glance
systemctl --user restart glance
if [ "$(loginctl show-user "$USER" -p Linger --value)" != yes ]; then
  echo "Note: linger is off, so the service won't start at boot until you log in." >&2
  echo "      Fix once with:  sudo loginctl enable-linger $USER" >&2
fi
# Give it time to fall over (a bad config.local.toml, say) before calling the
# deploy good. "start-pre" is the wait for network time, which can take longer.
sleep 12
systemctl --user --no-pager --lines=5 status glance || true
state=$(systemctl --user show glance -p SubState --value)
restarts=$(systemctl --user show glance -p NRestarts --value)
if { [ "$state" != running ] && [ "$state" != start-pre ]; } || [ "$restarts" != 0 ]; then
  echo "The service did not stay up (state: $state, restarts: $restarts). Its log:  journalctl --user-unit glance -e" >&2
  exit 1
fi
REMOTE
