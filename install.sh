#!/usr/bin/env bash
# Bantham Kiosk — Raspberry Pi setup
# Run once, then reboot.
#
#   chmod +x install.sh && ./install.sh
#   sudo reboot
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_DIR="$HOME/.config/autostart"
LXSESSION_DIR="$HOME/.config/lxsession/LXDE-pi"
WAYFIRE_CFG="$HOME/.config/wayfire.ini"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║     Bantham Kiosk — Pi Setup         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Python dependency ──────────────────────────────────────────────────────
echo "[1/4] Installing Python dependencies..."
pip3 install websocket-client --break-system-packages
echo "      ✓ websocket-client"

# ── 2. Hide mouse cursor ──────────────────────────────────────────────────────
echo "[2/4] Installing unclutter (hides mouse cursor)..."
sudo apt-get install -y -q unclutter
echo "      ✓ unclutter"

# ── 3. Disable screen blanking ────────────────────────────────────────────────
echo "[3/4] Disabling screen blanking..."

# X11 / LXDE (Bullseye and older Bookworm builds)
mkdir -p "$LXSESSION_DIR"
LXDE_AUTO="$LXSESSION_DIR/autostart"
# Remove any stale lines before re-adding, so we never duplicate
if [ -f "$LXDE_AUTO" ]; then
    sed -i '/xset s\|xset -dpms\|xset s noblank\|unclutter/d' "$LXDE_AUTO"
fi
cat >> "$LXDE_AUTO" << 'EOF'
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0.1 -root
EOF
echo "      ✓ LXDE screen blanking disabled"

# Wayland / Wayfire (Bookworm on Pi 5)
if [ -f "$WAYFIRE_CFG" ]; then
    if ! grep -q "^\[idle\]" "$WAYFIRE_CFG" 2>/dev/null; then
        printf "\n[idle]\nidle_timeout = 0\n" >> "$WAYFIRE_CFG"
        echo "      ✓ Wayfire idle timeout disabled"
    fi
fi

# ── 4. Create autostart entry for kiosk ──────────────────────────────────────
echo "[4/4] Creating kiosk autostart entry..."
mkdir -p "$AUTOSTART_DIR"

# Small sleep so the desktop is fully up before the kiosk starts
cat > "$AUTOSTART_DIR/bantham-kiosk.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Bantham Kiosk
Comment=Surf kiosk — starts server and launches Chrome
Exec=bash -c 'sleep 5 && python3 $SCRIPT_DIR/server.py'
X-GNOME-Autostart-enabled=true
EOF
echo "      ✓ autostart entry created"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
echo "║              All done!               ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Log in to Surfline once (to save your session):"
echo ""
echo "     chromium-browser \\"
echo "       --user-data-dir=$SCRIPT_DIR/chrome_profile \\"
echo "       https://www.surfline.com/sign-in"
echo ""
echo "  2. Reboot to start the kiosk:"
echo ""
echo "     sudo reboot"
echo ""
echo "  ── Useful commands ───────────────────"
echo "  Stop kiosk:    pkill -f server.py && pkill chromium-browser"
echo "  Start manually: python3 $SCRIPT_DIR/server.py"
echo "  View logs:     journalctl -f   (or check terminal output)"
echo ""
