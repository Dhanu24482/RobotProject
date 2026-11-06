#!/usr/bin/env bash
# Install (or remove) the Lumi control panel as a system service that runs as the current user.
#   sudo -v && ./install.sh            install + enable + start
#   ./install.sh --uninstall           stop + disable + remove
# The panel only listens on 127.0.0.1:8090 unless you enable "network" in its settings.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="$(dirname "$HERE")"
USER_NAME="${SUDO_USER:-$(id -un)}"
UNIT=/etc/systemd/system/lumi-panel.service

if [ "${1:-}" = "--uninstall" ]; then
  sudo systemctl disable --now lumi-panel.service 2>/dev/null || true
  sudo rm -f "$UNIT"; sudo systemctl daemon-reload
  echo "Removed lumi-panel.service (settings in ~/.config/lumi are kept)."; exit 0
fi

sed -e "s|@USER@|$USER_NAME|g" -e "s|@WS@|$WS|g" -e "s|@UID@|$(id -u "$USER_NAME")|g" \
  "$HERE/lumi-panel.service" | sudo tee "$UNIT" >/dev/null
sudo chmod 644 "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now lumi-panel.service
sleep 2
systemctl --no-pager --lines=5 status lumi-panel.service || true
echo
echo "Panel:  http://127.0.0.1:8090/   (add a Cloudflare Tunnel hostname -> http://localhost:8090 and protect it with Access)"
