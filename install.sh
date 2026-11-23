#!/usr/bin/env bash
# Installs Print Lab on a Raspberry Pi (Raspberry Pi OS Bookworm or newer, 32- or 64-bit).
# Run from the project folder as your normal user (not root):   ./install.sh
# Re-running it is safe; use it after `git pull` too.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
PORT="${PRINTLAB_PORT:-80}"

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user, not root. It will ask for sudo when needed." >&2
  exit 1
fi

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

echo "==> Creating Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/data"

echo "==> Installing the printlab service (port $PORT, starts at boot)"
sudo tee /etc/systemd/system/printlab.service >/dev/null <<EOF
[Unit]
Description=Print Lab 3D print queue
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PRINTLAB_DATA=$APP_DIR/data
ExecStart=$APP_DIR/.venv/bin/uvicorn printlab.main:create_app --factory --host 0.0.0.0 --port $PORT --log-level warning
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now printlab.service
sudo systemctl restart printlab.service

echo "==> Setting the touch screen to open the control panel at login"
chmod +x "$APP_DIR/deploy/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/printlab-panel.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Print Lab panel
Exec=$APP_DIR/deploy/kiosk.sh panel $PORT
X-GNOME-Autostart-enabled=true
EOF

if command -v raspi-config >/dev/null; then
  echo "==> Turning off screen blanking"
  sudo raspi-config nonint do_blanking 1 || true
fi

IP="$(hostname -I | awk '{print $1}')"
HOST="$(hostname)"
SUFFIX=""
[ "$PORT" != "80" ] && SUFFIX=":$PORT"
echo
echo "Done. Print Lab is running."
echo "  Time zone on this Pi: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
echo "    (the 1:30 PM rule uses this clock; fix it with: sudo raspi-config > Localisation Options > Timezone)"
echo "  Student upload page:  http://$HOST.local$SUFFIX/   or   http://$IP$SUFFIX/"
echo "  TV board:             http://$IP$SUFFIX/board"
echo "  Teachers:             http://$IP$SUFFIX/teacher"
echo "  Admin (set up first): http://$IP$SUFFIX/admin"
echo
echo "Reboot to start the touch-screen panel:  sudo reboot"
