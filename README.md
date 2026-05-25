# Bantham Beach Kiosk

A full-screen rotating surf kiosk for a Raspberry Pi (or Mac). Displays live surf cams, Surfline forecast and analysis pages, local weather, and a dual-cam multiview — all running in a persistent Chrome session with no delay between panels.

## Rotation

| Panel | Duration |
|---|---|
| Bantham cam (live HLS) | 10 s |
| Bantham OV cam (live HLS) | 10 s |
| Bigbury cam (live HLS) | 10 s |
| Surfline forecast | 20 s |
| Weather (open-meteo) | 10 s |
| Surfline analysis | 20 s |
| Multiview — Bantham + Bantham OV | 20 s |

All panels are pre-loaded Chrome tabs. Switching is a single CDP call — no reload, no blank screen.

---

## Prerequisites

- Python 3.8+
- Google Chrome or Chromium
- `websocket-client` Python package

---

## macOS

```bash
# 1. Clone the repo
git clone https://github.com/nv0id/surfline-kiosk.git
cd bantham-kiosk

# 2. Install the Python dependency
pip3 install websocket-client

# 3. Run
python3 server.py
```

Chrome opens automatically in kiosk mode. A persistent profile is created at `./chrome_profile/` so your Surfline login is remembered.

---

## Raspberry Pi

### Quick install

```bash
git clone https://github.com/yourname/bantham-kiosk.git
cd bantham-kiosk
chmod +x install.sh
./install.sh
sudo reboot
```

The install script:
- Installs `websocket-client`
- Installs `unclutter` (hides the mouse cursor)
- Disables screen blanking and DPMS
- Creates an autostart entry so the kiosk launches on every boot

### Requirements

- Raspberry Pi OS with desktop (Bullseye or Bookworm)
- Auto-login to desktop enabled  
  *(Raspberry Pi Configuration → System → Auto login)*
- Chromium installed (pre-installed on Raspberry Pi OS)

---

## First-time Surfline login

The kiosk uses a persistent Chrome profile stored in `./chrome_profile/`. You need to log in to Surfline once so the session is saved.

**On macOS** — just run the kiosk, navigate to Surfline in the kiosk window, and log in. The session persists automatically.

**On the Pi** — run this once with a monitor and keyboard attached, log in, then close the window:

```bash
chromium-browser \
  --user-data-dir=/path/to/bantham-kiosk/chrome_profile \
  https://www.surfline.com/sign-in
```

After that, every time the kiosk loads Surfline it will already be logged in.

---

## Configuration

Everything is at the top of `server.py`.

### Change rotation timings

```python
ROTATION = [
    {"url": ..., "duration": 10, "label": "Bantham"},
    ...
]
```

Edit `"duration"` (seconds) for any panel.

### Add or remove panels

Add an entry to `ROTATION`. Local pages (`/cam/...`, `/weather`, `/multiview`) are served by the Python server. External URLs open directly in Chrome.

To scroll a panel to a specific heading on load, add `"scroll": True` — the script will search for a heading by text content and scroll to it.

### Change HLS stream URLs

```python
STREAMS = {
    "bantham":    "/hls/ireland/uk-bantham/playlist.m3u8",
    "bantham_ov": "/hls/ireland/uk-banthamov/playlist.m3u8",
    "bigbury":    "/hls/ireland/uk-bigbury/playlist.m3u8",
}
```

The `/hls/` prefix routes through the local proxy, which adds the required `Referer` header for the Surfline CDN.

### Change Surfline locations

Replace the URLs in `ROTATION`:

```python
{"url": "https://www.surfline.com/surf-report/YOUR-SPOT/SPOT-ID", ...}
```

### Ports

```python
PORT     = 8080   # local HTTP server
CDP_PORT = 9222   # Chrome DevTools Protocol
```

---

## How it works

```
server.py
├── HTTP server (port 8080)
│   ├── /cam/<stream>   — HLS player page (hls.js)
│   ├── /weather        — weather panel (open-meteo API)
│   ├── /multiview      — dual-cam grid
│   └── /hls/...        — HLS reverse proxy (adds Referer)
│
└── CDP controller (port 9222)
    ├── Launches Chrome with --kiosk --user-data-dir
    ├── Pre-loads all rotation panels as background tabs
    └── Rotates by calling Target.activateTarget
```

Surfline tabs are reloaded in the background immediately after their slot ends, so they're always fresh for the next cycle (~80 s to reload).

---

## Files

```
bantham-kiosk/
├── server.py          ← everything: HTTP server + CDP controller + HTML pages
├── install.sh         ← one-shot Pi setup (run once, then reboot)
├── README.md
└── chrome_profile/    ← created automatically at first run (gitignored)
                          contains your Chrome session / Surfline login
```

> **Note:** `chrome_profile/` is gitignored. It contains your login cookies — don't commit it.

---

## .gitignore

```
chrome_profile/
__pycache__/
*.pyc
```
