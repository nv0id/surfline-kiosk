#!/usr/bin/env python3
"""
Bantham Beach Kiosk
===================
All panels run as pre-loaded Chrome tabs. Switching is instant (CDP activateTarget).
HLS streams are played by hls.js inside local HTML pages served by this script.
Surfline pages open directly in Chrome — login persists via --user-data-dir.

Low-memory design (1 GB Pi): streams do NOT run in the background. The next
panel's stream is warmed WARM_AHEAD seconds before it appears and destroyed
once the panel rotates out, so at most two streams are ever live. Surfline
tabs reload at most every SURFLINE_RELOAD_SECS.

Rotation:
  Bantham (10s)  →  Bantham OV (10s)  →  Bigbury (10s)
  → Surfline Forecast (20s)  →  Weather (10s)
  → Surfline Analysis (20s)  →  Multiview (20s)

Run:  python3 server.py
Deps: pip3 install websocket-client --break-system-packages

Chrome is launched automatically with a persistent profile (./chrome_profile/).
Surfline login is preserved across restarts.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import websocket
except ImportError:
    sys.exit("pip3 install websocket-client --break-system-packages")

# ── Config ────────────────────────────────────────────────────────────────────

PORT     = 8080
CDP_PORT = 9222

HLS_ORIGIN = "https://hls.cdn-surfline.com"
REFERER    = "https://www.surfline.com/"
UA         = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

STREAMS = {
    "bantham":    "/hls/ireland/uk-bantham/playlist.m3u8",
    "bantham_ov": "/hls/ireland/uk-banthamov/playlist.m3u8",
    "bigbury":    "/hls/ireland/uk-bigbury/playlist.m3u8",
}

ROTATION = [
    {"url": f"http://localhost:{PORT}/cam/bantham",    "duration": 10, "label": "Bantham"},
    {"url": f"http://localhost:{PORT}/cam/bantham_ov", "duration": 10, "label": "Bantham OV"},
    {"url": f"http://localhost:{PORT}/cam/bigbury",    "duration": 10, "label": "Bigbury"},
    {"url": "https://www.surfline.com/surf-report/bantham/584204204e65fad6a77090c9",
     "duration": 20, "label": "Forecast", "scroll": True},
    {"url": f"http://localhost:{PORT}/weather",        "duration": 10, "label": "Weather"},
    {"url": ("https://www.surfline.com/surf-forecasts/south-devon/"
             "58581a836630e24c4487918a?spotId=584204204e65fad6a77090c9"),
     "duration": 20, "label": "Analysis"},
    {"url": f"http://localhost:{PORT}/multiview",      "duration": 20, "label": "Multiview"},
]

# Seconds to wait after creating all tabs before starting the rotation
PAGE_LOAD_WAIT = 12

# Start the NEXT panel's video stream this many seconds before switching to it,
# so it's already playing when shown. Streams are destroyed once a panel rotates
# out — at most 1 warming + 1 visible panel hold live streams at any time.
WARM_AHEAD = 6

# Reload Surfline tabs at most this often (seconds)
SURFLINE_RELOAD_SECS = 600

# Chrome persistent profile — login cookies live here
CHROME_PROFILE = Path(__file__).parent / "chrome_profile"

# Chrome binary candidates (macOS first, then Linux/Pi)
_CHROME_BINS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",   # Raspberry Pi OS
    "chromium",
)

CHROME_FLAGS = [
    "--kiosk",
    f"--user-data-dir={CHROME_PROFILE}",
    "--no-first-run",
    "--noerrdialogs",
    "--disable-infobars",
    "--autoplay-policy=no-user-gesture-required",
    # Display scaling for the kiosk screen
    "--force-device-scale-factor=1.75",
    # fbdev is a software renderer — disable GPU paths so Chrome doesn't thrash.
    # NB: do NOT add --disable-software-rasterizer alongside this; together they
    # leave Chrome with no renderer at all.
    "--disable-gpu",
    # Memory — important on 1 GB Pi
    "--disable-dev-shm-usage",          # use /tmp instead of /dev/shm (avoids OOM)
    "--renderer-process-limit=1",
    # Low-memory tuning: share renderer processes per site, low-end device mode.
    # No --js-flags heap cap: with renderer-process-limit=1 every tab shares one
    # process, and a global cap there risks OOM-killing the whole kiosk.
    "--process-per-site",
    "--enable-low-end-device-mode",
    f"--remote-debugging-port={CDP_PORT}",
    "--remote-allow-origins=*",
    "about:blank",
]


def launch_chrome():
    """Find and launch Chrome/Chromium with kiosk flags."""
    for candidate in _CHROME_BINS:
        binary = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if binary:
            print(f"[kiosk] Launching {binary}")
            subprocess.Popen(
                [binary] + CHROME_FLAGS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
    sys.exit("[kiosk] No Chrome/Chromium binary found. Install Chromium or Google Chrome.")


# Indices of Surfline tabs — reloaded in the background when stale
SURFLINE_IDXS = {i for i, s in enumerate(ROTATION) if "surfline.com" in s["url"]}

# Indices of panels that contain HLS video players (support kioskStart/kioskStop)
VIDEO_IDXS = {i for i, s in enumerate(ROTATION)
              if "/cam/" in s["url"] or "/multiview" in s["url"]}

# ── HTML pages ────────────────────────────────────────────────────────────────

_RESET = (
    "* {margin:0;padding:0;box-sizing:border-box} "
    "html,body {width:100%;height:100%;background:#000;overflow:hidden}"
)
HLS_JS = "https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"
# Buffer caps matter on 1 GB: hls.js's default back-buffer is unbounded and
# grows for as long as a stream plays.
HLS_CFG = ("{manifestLoadingMaxRetry:8,levelLoadingMaxRetry:8,fragLoadingMaxRetry:8,"
           "maxBufferLength:12,maxMaxBufferLength:20,backBufferLength:4,"
           "liveSyncDurationCount:3}")

# Shared player-lifecycle JS. Defines kioskStart()/kioskStop() over a list of
# [videoElementId, hlsSrc] pairs. Streams only exist between start and stop —
# the server warms the next panel just before showing it and stops panels
# after they rotate out. Safety nets: start on becoming visible, stop after
# 20 s hidden (in case a CDP call was missed).
def _player_js(pairs_js: str) -> str:
    return f"""
const PLAYERS = {pairs_js}.map(([id, src]) => ({{v: document.getElementById(id), src, hls: null}}));
let hiddenSince = null;

function kioskStart() {{
  hiddenSince = Date.now();   // grace period: warming happens while still hidden
  PLAYERS.forEach(p => {{
    if (p.hls) {{ if (p.v.paused) p.v.play().catch(() => {{}}); return; }}
    p.hls = new Hls({HLS_CFG});
    p.hls.loadSource(p.src);
    p.hls.attachMedia(p.v);
    p.hls.on(Hls.Events.MANIFEST_PARSED, () => p.v.play().catch(() => {{}}));
  }});
}}
function kioskStop() {{
  PLAYERS.forEach(p => {{
    if (!p.hls) return;
    p.hls.destroy();          // frees decoder + all buffered media
    p.hls = null;
    p.v.removeAttribute('src');
    p.v.load();
  }});
}}
window.kioskStart = kioskStart;
window.kioskStop  = kioskStop;

document.addEventListener('visibilitychange', () => {{
  if (document.visibilityState === 'visible') {{ hiddenSince = null; kioskStart(); }}
  else hiddenSince = Date.now();
}});
setInterval(() => {{
  if (document.hidden) {{
    if (hiddenSince === null) hiddenSince = Date.now();
    if (Date.now() - hiddenSince > 20000) kioskStop();
  }} else {{
    PLAYERS.forEach(p => {{ if (p.hls && p.v.paused) p.v.play().catch(() => {{}}); }});
  }}
}}, 5000);

if (document.visibilityState === 'visible') kioskStart();
"""


def cam_page(key: str) -> str:
    src   = STREAMS[key]
    label = {"bantham": "Bantham", "bantham_ov": "Bantham OV", "bigbury": "Bigbury"}[key]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{label}</title>
<style>
{_RESET}
video {{width:100%;height:100%;object-fit:contain}}
#lbl  {{position:fixed;top:20px;left:20px;background:rgba(0,0,0,.6);color:#fff;
        font:300 1.4rem/1 system-ui;padding:6px 16px;border-radius:6px;letter-spacing:.04em}}
</style></head><body>
<video id="v" muted playsinline></video>
<div id="lbl">{label}</div>
<script src="{HLS_JS}"></script>
<script>
{_player_js(json.dumps([["v", src]]))}
</script>
</body></html>"""


WEATHER_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Weather — Bantham</title>
<style>
{_RESET}
body  {{display:flex;flex-direction:column;align-items:center;justify-content:center;
        gap:1.8rem;background:#07111f;color:#e2eff8;
        font-family:'Segoe UI',system-ui,-apple-system,sans-serif}}
.loc  {{font-size:2.6rem;font-weight:200;letter-spacing:.35em;text-transform:uppercase;color:#7ecfed}}
.clk  {{font-size:1.05rem;color:#6b8899;letter-spacing:.05em}}
.grid {{display:grid;grid-template-columns:repeat(3,1fr);gap:1.4rem;width:min(92vw,960px)}}
.card {{background:rgba(255,255,255,.04);border:1px solid rgba(126,207,237,.18);
        border-radius:18px;padding:1.8rem 1.4rem;text-align:center}}
.lbl  {{font-size:.72rem;text-transform:uppercase;letter-spacing:.18em;color:#7ecfed;margin-bottom:.4rem}}
.icon {{font-size:2.6rem;line-height:1;margin-bottom:.2rem}}
.val  {{font-size:4rem;font-weight:100;line-height:1}}
.unit {{font-size:1rem;color:#6b8899;margin-top:.1rem}}
.sub  {{font-size:1rem;color:#94b4c8;margin-top:.4rem}}
</style></head><body>
<div class="loc">Bantham</div>
<div class="clk" id="clk"></div>
<div class="grid" id="grid">
  <div class="card" style="grid-column:1/-1;color:#6b8899;font-size:1.1rem">Loading weather…</div>
</div>
<script>
const LAT = 50.28, LON = -3.876;
const WMO = {{
   0:['Clear sky','☀️'],    1:['Mainly clear','🌤️'],  2:['Partly cloudy','⛅'],
   3:['Overcast','☁️'],   45:['Foggy','🌫️'],         48:['Icy fog','🌫️'],
  51:['Light drizzle','🌦️'],53:['Drizzle','🌧️'],     55:['Heavy drizzle','🌧️'],
  61:['Light rain','🌦️'], 63:['Rain','🌧️'],          65:['Heavy rain','🌧️'],
  80:['Showers','🌦️'],    81:['Showers','🌧️'],        82:['Heavy showers','⛈️'],
  95:['Thunderstorm','⛈️'],99:['Thunderstorm/hail','⛈️']
}};
const DIRS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
const dir  = d => DIRS[Math.round(d / 22.5) % 16];

setInterval(() => {{
  const n = new Date();
  document.getElementById('clk').textContent =
    n.toLocaleDateString('en-GB', {{weekday:'long',day:'numeric',month:'long'}}) +
    '  ·  ' + n.toLocaleTimeString('en-GB', {{hour:'2-digit',minute:'2-digit'}});
}}, 1000);

async function loadWeather() {{
  try {{
    const [w, m] = await Promise.all([
      fetch(`https://api.open-meteo.com/v1/forecast?latitude=${{LAT}}&longitude=${{LON}}&current=temperature_2m,wind_speed_10m,wind_direction_10m,weather_code&wind_speed_unit=mph&timezone=Europe%2FLondon`).then(r => r.json()),
      fetch(`https://marine-api.open-meteo.com/v1/marine?latitude=${{LAT}}&longitude=${{LON}}&current=wave_height,wave_direction,wave_period&timezone=Europe%2FLondon`).then(r => r.json()),
    ]);
    const wc = w.current, mc = m.current;
    const [cl, ci] = WMO[wc.weather_code] ?? ['Unknown', '❓'];
    const waveH = mc.wave_height   != null ? mc.wave_height.toFixed(1)   : '—';
    const wavePer = mc.wave_period != null ? mc.wave_period.toFixed(0) + 's' : '';
    const waveDir = mc.wave_direction != null ? dir(mc.wave_direction) : '';
    document.getElementById('grid').innerHTML = `
      <div class="card">
        <div class="lbl">Temperature</div><div class="icon">${{ci}}</div>
        <div class="val">${{Math.round(wc.temperature_2m)}}°</div>
        <div class="unit">Celsius</div><div class="sub">${{cl}}</div>
      </div>
      <div class="card">
        <div class="lbl">Wind</div>
        <div class="icon" style="display:inline-block;transform:rotate(${{wc.wind_direction_10m}}deg)">↑</div>
        <div class="val">${{Math.round(wc.wind_speed_10m)}}</div>
        <div class="unit">mph · ${{dir(wc.wind_direction_10m)}}</div>
      </div>
      <div class="card">
        <div class="lbl">Waves</div><div class="icon">🌊</div>
        <div class="val">${{waveH}}</div><div class="unit">metres</div>
        <div class="sub">${{[wavePer, waveDir].filter(Boolean).join(' · ')}}</div>
      </div>`;
  }} catch (e) {{
    document.getElementById('grid').innerHTML =
      '<div class="card" style="grid-column:1/-1;color:#6b8899">Weather unavailable</div>';
  }}
}}
loadWeather();
setInterval(loadWeather, 60_000);
</script>
</body></html>"""


def multiview_page() -> str:
    # Bantham + Bantham OV only (Bigbury removed)
    mv = [("bantham", "Bantham"), ("bantham_ov", "Bantham OV")]
    slots = "\n".join(
        f'  <div class="slot">'
        f'<video id="v{i}" muted playsinline></video>'
        f'<div class="lbl">{label}</div>'
        f'</div>'
        for i, (_, label) in enumerate(mv)
    )
    pairs_js = json.dumps([[f"v{i}", STREAMS[k]] for i, (k, _) in enumerate(mv)])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Multiview</title>
<style>
{_RESET}
body  {{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr}}
.slot {{position:relative;overflow:hidden;min-height:0;background:#000}}
video {{width:100%;height:100%;object-fit:cover}}
.lbl  {{position:absolute;bottom:16px;left:16px;background:rgba(0,0,0,.55);color:#fff;
        font:300 1.2rem/1 system-ui;padding:6px 16px;border-radius:6px;letter-spacing:.03em}}
</style></head><body>
{slots}
<script src="{HLS_JS}"></script>
<script>
{_player_js(pairs_js)}
</script>
</body></html>"""


# ── HLS proxy ─────────────────────────────────────────────────────────────────

def _rewrite_m3u8(body: bytes, proxy_path: str) -> bytes:
    """Rewrite URLs in a playlist so segments are also fetched through the proxy."""
    base = proxy_path.rsplit("/", 1)[0]
    out  = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            if s.startswith(("http://", "https://")):
                s = "/hls" + urllib.parse.urlparse(s).path
            elif s.startswith("/"):
                s = "/hls" + s
            else:
                s = base + "/" + s
        out.append(s)
    return "\n".join(out).encode()


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/cam/") and (key := path[5:]) in STREAMS:
            self._html(cam_page(key))
        elif path == "/weather":
            self._html(WEATHER_PAGE)
        elif path == "/multiview":
            self._html(multiview_page())
        elif path.startswith("/hls/"):
            self._hls(path)
        else:
            self.send_error(404)

    def _html(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",  "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _hls(self, proxy_path: str):
        remote = HLS_ORIGIN + proxy_path[4:]   # strip leading /hls
        try:
            req = urllib.request.Request(remote, headers={
                "Referer":    REFERER,
                "User-Agent": UA,
                "Origin":     "https://www.surfline.com",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                ct   = resp.headers.get("Content-Type", "application/octet-stream")
            if "mpegurl" in ct or proxy_path.endswith(".m3u8"):
                body = _rewrite_m3u8(body, proxy_path)
                ct   = "application/vnd.apple.mpegurl"
            self.send_response(200)
            self.send_header("Content-Type",                ct)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control",               "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as e:
            self.send_error(e.code, str(e))
        except Exception as e:
            self.send_error(502, str(e))

    def log_message(self, fmt, *args):
        # Suppress 200s to keep logs readable; show everything else
        if args and str(args[1]) != "200":
            super().log_message(fmt, *args)


# ── CDP helpers ───────────────────────────────────────────────────────────────

def _cdp(path: str):
    r = urllib.request.urlopen(f"http://localhost:{CDP_PORT}{path}", timeout=5)
    return json.loads(r.read())


def _ws(ws_url, method, params=None):
    conn = websocket.create_connection(ws_url, timeout=30)
    try:
        conn.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        return json.loads(conn.recv())
    finally:
        conn.close()


def _browser(method, params=None):
    info = _cdp("/json/version")
    return _ws(info["webSocketDebuggerUrl"], method, params)


def _reload(target_id):
    for t in _cdp("/json"):
        if t.get("id") == target_id and "webSocketDebuggerUrl" in t:
            _ws(t["webSocketDebuggerUrl"], "Page.reload")
            return


def _eval(target_id, expression):
    """Run JS in a tab; returns the result value or None."""
    for t in _cdp("/json"):
        if t.get("id") == target_id and "webSocketDebuggerUrl" in t:
            r = _ws(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True})
            return r.get("result", {}).get("result", {}).get("value")


def _start_stream(tab_ids, idx, label):
    try:
        _eval(tab_ids[idx], "window.kioskStart && window.kioskStart()")
        print(f"[kiosk]   ▶ warm {label}")
    except Exception as e:
        print(f"[kiosk]   warm error ({label}): {e}")


def _stop_stream(tab_ids, idx, label):
    try:
        _eval(tab_ids[idx], "window.kioskStop && window.kioskStop()")
        print(f"[kiosk]   ■ stop {label}")
    except Exception as e:
        print(f"[kiosk]   stop error ({label}): {e}")


# Scroll Surfline to "Current Surf Conditions" (or similar heading).
# Matches by text content only — no class names — so it survives redesigns.
_SURF_SCROLL_JS = """
(function() {
  var targets = ['Current Surf Conditions', 'Surf Conditions', 'Current Conditions'];
  var headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  for (var i = 0; i < targets.length; i++) {
    var el = headings.find(function(h) {
      return h.textContent.trim().indexOf(targets[i]) === 0;
    });
    if (el) {
      el.scrollIntoView({behavior: 'instant', block: 'start'});
      return 'ok: ' + el.textContent.trim();
    }
  }
  // Fallback: scroll to ~25% down (where conditions usually sit)
  window.scrollTo({top: document.documentElement.scrollHeight * 0.25, behavior: 'instant'});
  return 'fallback';
})()
"""


def _scroll_surfline(target_id, delay=1.5):
    """Inject scroll JS into a Surfline tab after a short delay (non-blocking)."""
    def _run():
        time.sleep(delay)
        try:
            for t in _cdp("/json"):
                if t.get("id") == target_id and "webSocketDebuggerUrl" in t:
                    r = _ws(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                            {"expression": _SURF_SCROLL_JS, "returnByValue": True})
                    result = r.get("result", {}).get("result", {}).get("value", "?")
                    print(f"[kiosk]   scroll → {result}")
                    return
        except Exception as e:
            print(f"[kiosk]   scroll error: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ── Rotation loop ─────────────────────────────────────────────────────────────

def rotation_loop():
    time.sleep(2)   # let the HTTP server bind first

    # Launch Chrome if it's not already listening on the CDP port
    try:
        _cdp("/json/version")
        print(f"[kiosk] Chrome already running on port {CDP_PORT}")
    except Exception:
        launch_chrome()

    # Wait for Chrome to be ready
    print(f"[kiosk] Waiting for Chrome on CDP port {CDP_PORT}…")
    for attempt in range(30):
        try:
            _cdp("/json/version")
            break
        except Exception:
            if attempt == 29:
                sys.exit(f"\n[kiosk] Chrome did not start. Check binary and flags.\n")
            time.sleep(1)

    print("[kiosk] Chrome connected — creating tabs")

    # Remember existing tabs so we can close them after our new ones are ready
    old_tabs = [t["id"] for t in _cdp("/json") if t.get("type") == "page"]

    # Create one tab per rotation step (all start loading in the background)
    tab_ids = []
    for step in ROTATION:
        r   = _browser("Target.createTarget", {"url": step["url"]})
        tid = r.get("result", {}).get("targetId")
        if not tid:
            sys.exit(f"[kiosk] Failed to create tab: {step['label']}")
        tab_ids.append(tid)
        print(f"[kiosk]   + {step['label']}")

    # Show the first tab and close the old blank one(s)
    _browser("Target.activateTarget", {"targetId": tab_ids[0]})
    for tid in old_tabs:
        try:
            _browser("Target.closeTarget", {"targetId": tid})
        except Exception:
            pass

    # Give all tabs time to load before the rotation begins.
    # (Video pages load light — streams don't start until warmed/visible.)
    print(f"[kiosk] Preloading — waiting {PAGE_LOAD_WAIT}s…")
    time.sleep(PAGE_LOAD_WAIT)
    print("[kiosk] Rotation started")

    n = len(ROTATION)
    surfline_last_reload = {i: 0.0 for i in SURFLINE_IDXS}
    prev_video = None   # index of the last video panel shown, to stop it

    while True:
        for i, (tid, step) in enumerate(zip(tab_ids, ROTATION)):
            try:
                _browser("Target.activateTarget", {"targetId": tid})
                print(f"[kiosk] → {step['label']}  ({step['duration']}s)")
                if i in VIDEO_IDXS:
                    _start_stream(tab_ids, i, step["label"])  # idempotent (warmed already)
                if step.get("scroll"):
                    _scroll_surfline(tid)   # non-blocking, fires after 1.5 s
            except Exception as e:
                print(f"[kiosk] activate error: {e}")

            # Kill the previous panel's stream now that it's off-screen
            if prev_video is not None and prev_video != i:
                _stop_stream(tab_ids, prev_video, ROTATION[prev_video]["label"])
            prev_video = i if i in VIDEO_IDXS else None

            # Sleep most of the slot, then warm the next panel's stream so it's
            # already playing when it appears.
            warm = min(WARM_AHEAD, step["duration"])
            time.sleep(step["duration"] - warm)
            nxt = (i + 1) % n
            if nxt in VIDEO_IDXS and nxt != i:
                _start_stream(tab_ids, nxt, ROTATION[nxt]["label"])
            time.sleep(warm)

            # Reload Surfline tabs in the background when stale (>10 min)
            if i in SURFLINE_IDXS and time.time() - surfline_last_reload[i] > SURFLINE_RELOAD_SECS:
                try:
                    _reload(tid)
                    surfline_last_reload[i] = time.time()
                    print(f"[kiosk]   reloading {step['label']} in background")
                except Exception as e:
                    print(f"[kiosk]   reload error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=rotation_loop, daemon=True).start()
    print(f"[kiosk] Serving on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[kiosk] Stopped")


if __name__ == "__main__":
    main()
