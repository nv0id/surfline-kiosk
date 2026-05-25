#!/usr/bin/env bash
# Bantham Kiosk — Raspberry Pi Lite setup
# Tested on Raspberry Pi OS Lite (Bookworm, 64-bit)
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
echo "[1/6] Installing packages (this may take a few minutes)..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    xserver-xorg \
    xserver-xorg-legacy \
    xserver-xorg-video-fbdev \
    xinit \
    openbox \
    unclutter \
    python3-pip

# Chromium: Bookworm (Debian 12) uses 'chromium', Bullseye uses 'chromium-browser'
if apt-cache show chromium &>/dev/null; then
    sudo apt-get install -y --no-install-recommends chromium
    CHROMIUM_BIN="chromium"
    echo "      ✓ chromium (Bookworm)"
elif apt-cache show chromium-browser &>/dev/null; then
    sudo apt-get install -y --no-install-recommends chromium-browser
    CHROMIUM_BIN="chromium-browser"
    echo "      ✓ chromium-browser (Bullseye)"
else
    echo "ERROR: Could not find chromium or chromium-browser in apt. Check your sources."
    exit 1
fi

echo "      ✓ xorg, xinit, openbox, unclutter"

# ── 2. Swap ───────────────────────────────────────────────────────────────────
echo "[2/7] Configuring swap (1 GB)..."
if command -v dphys-swapfile &>/dev/null; then
    # dphys-swapfile is the RPi OS standard swap manager
    sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
    sudo dphys-swapfile setup
    sudo dphys-swapfile swapon
    echo "      ✓ dphys-swapfile set to 1024 MB"
else
    # Fallback: plain swapfile
    if [ ! -f /swapfile ]; then
        sudo fallocate -l 1G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
    fi
    sudo swapon /swapfile 2>/dev/null || true
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "      ✓ /swapfile created (1 GB)"
fi

# ── 3. X server permissions ───────────────────────────────────────────────────
echo "[3/7] Configuring X server permissions..."

# Allow non-root users to start X, using setuid wrapper for hardware access
sudo tee /etc/X11/Xwrapper.config > /dev/null << 'EOF'
allowed_users=anybody
needs_root_rights=yes
EOF

# Add user to groups needed for display and input hardware
sudo usermod -a -G tty,video,input,render "$WHOAMI"

# Point X at the Pi's framebuffer device (avoids "framebuffer mode" error)
sudo mkdir -p /etc/X11/xorg.conf.d
sudo tee /etc/X11/xorg.conf.d/10-fbdev.conf > /dev/null << 'EOF'
Section "Device"
    Identifier "Card0"
    Driver     "fbdev"
EndSection
EOF

# Enable Ctrl+Alt+Backspace to kill X (useful for exiting the kiosk)
sudo tee /etc/X11/xorg.conf.d/20-keyboard.conf > /dev/null << 'EOF'
Section "InputClass"
    Identifier "keyboard"
    MatchIsKeyboard "on"
    Option "XkbOptions" "terminate:ctrl_alt_bksp"
EndSection
EOF

echo "      ✓ Xwrapper, groups, fbdev driver, keyboard configured"

# ── 4. Python dependency ──────────────────────────────────────────────────────
echo "[4/7] Installing Python dependency..."
pip3 install websocket-client --break-system-packages
echo "      ✓ websocket-client"

# ── 5. Configure autologin on tty1 ───────────────────────────────────────────
echo "[5/7] Configuring autologin for $WHOAMI on tty1..."
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $WHOAMI --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
echo "      ✓ autologin configured"

# ── 6. Create ~/.xinitrc (what X runs on startup) ────────────────────────────
echo "[6/7] Creating ~/.xinitrc..."
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

# ── 7. Auto-start X when logged in on tty1 ───────────────────────────────────
echo "[7/7] Configuring startx on login..."
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
echo "    pkill -f server.py && pkill $CHROMIUM_BIN"
echo "    DISPLAY=:0 $CHROMIUM_BIN \\"
echo "      --user-data-dir=$SCRIPT_DIR/chrome_profile \\"
echo "      https://www.surfline.com/sign-in"
echo ""
echo "  Log in, close Chrome, then restart:"
echo "    python3 $SCRIPT_DIR/server.py"
echo ""
echo "  ── Useful commands ───────────────────"
echo "  Stop kiosk:     pkill -f server.py && pkill $CHROMIUM_BIN"
echo "  Restart kiosk:  DISPLAY=:0 python3 $SCRIPT_DIR/server.py"
echo "  View logs:      journalctl -u getty@tty1 -f"
echo "  Reboot now:     sudo reboot"
echo ""
echo "  NOTE: Log out and back in (or reboot) for group"
echo "  membership changes to take effect."
echo ""
