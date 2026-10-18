#!/bin/bash
# Stops and removes dm's engine. Downloaded files always stay; the job list
# and settings stay too unless you pass --purge.
set -euo pipefail
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now dm.service >/dev/null 2>&1 || true
rm -f "$unit_dir/dm.service" "$HOME/.local/bin/dm"
systemctl --user daemon-reload

if [[ ${1:-} == --purge ]]; then
  rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/dm" "${XDG_CONFIG_HOME:-$HOME/.config}/dm"
fi
rm -rf "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/dm"
echo "dm engine removed. Remove the bar widget with: omarchy plugin remove io.github.puneethkethanapalli.dm"
