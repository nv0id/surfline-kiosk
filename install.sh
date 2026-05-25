#!/usr/bin/env bash
# Bantham Kiosk — Raspberry Pi Lite setup
# Installs a minimal X stack, sets up autologin, and starts the kiosk on boot.
#
#   chmod +x install.sh && ./install.sh
#   sudo reboot
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHOAMI="$(whoami)"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Bantham Kiosk — Pi Lite Setup      ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/5] Installing packages (this may take a few minutes)..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    xserver-xorg \
    xinit \
    openbox \
    chromium-browser \
    unclutter \
    python3-pip
echo "      ✓ xorg, xinit, openbox, chromium-browser, unclutter"

# ── 2. Python dependency ──────────────────────────────────────────────────────
echo "[2/5] Installing Python dependency..."
pip3 install websocket-client --break-system-packages
echo "      ✓ websocket-client"

# ── 3. Configure autologin on tty1 ───────────────────────────────────────────
echo "[3/5] Configuring autologin for $WHOAMI on tty1..."
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $WHOAMI --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
echo "      ✓ autologin configured"

# ── 4. Create ~/.xinitrc (what X runs on startup) ────────────────────────────
echo "[4/5] Creating ~/.xinitrc..."
cat > "$HOME/.xinitrc" << EOF
#!/bin/sh

# Disable screen blanking and power management
xset s off
xset -dpms
xset s noblank

# Hide mouse cursor after 0.5 s of inactivity
unclutter -idle 0.5 -root &

# Minimal window manager (gives Chrome a proper fullscreen environment)
openbox &

# Wait for WM, then launch the kiosk server
# server.py starts Chrome automatically with --kiosk
sleep 2
exec python3 $SCRIPT_DIR/server.py
EOF
chmod +x "$HOME/.xinitrc"
echo "      ✓ ~/.xinitrc created"

# ── 5. Auto-start X when logged in on tty1 ───────────────────────────────────
echo "[5/5] Configuring startx on login..."
PROFILE="$HOME/.bash_profile"
if ! grep -q "startx" "$PROFILE" 2>/dev/null; then
    cat >> "$PROFILE" << 'PROFILE_EOF'

# Start the X kiosk automatically on tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
PROFILE_EOF
fi
echo "      ✓ ~/.bash_profile updated"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
echo "║              All done!               ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Boot sequence after reboot:"
echo "    Pi boots → autologin as $WHOAMI"
echo "    → startx → ~/.xinitrc"
echo "    → openbox + server.py"
echo "    → Chrome launches in kiosk mode"
echo ""
echo "  ── Surfline login ────────────────────"
echo "  The kiosk will open Surfline automatically."
echo "  To log in, temporarily stop the kiosk and run:"
echo ""
echo "    pkill -f server.py && pkill chromium-browser"
echo "    DISPLAY=:0 chromium-browser \\"
echo "      --user-data-dir=$SCRIPT_DIR/chrome_profile \\"
echo "      https://www.surfline.com/sign-in"
echo ""
echo "  Log in, close Chrome, then restart:"
echo "    python3 $SCRIPT_DIR/server.py"
echo ""
echo "  ── Useful commands ───────────────────"
echo "  Stop kiosk:     pkill -f server.py && pkill chromium-browser"
echo "  Restart kiosk:  python3 $SCRIPT_DIR/server.py"
echo "  Reboot now:     sudo reboot"
echo ""
