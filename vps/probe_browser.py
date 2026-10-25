"""
Check whether a real Chrome on this machine can reach the Quotex WebSocket.

Run on the VPS under a virtual display:
    xvfb-run -a python vps/probe_browser.py

Steps reported:
  1. Page load      - does Cloudflare challenge or block the page?
  2. WebSocket open - can the page open wss://ws2.quotex.com (the part curl is refused on)?
  3. Authorization  - only if QUOTEX_SSID is set; sends the authorization event
"""

import json
import os
import shutil
import sys
import tempfile
import time

import undetected_chromedriver as uc

_BROWSER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser")
CHROME_BIN = os.path.join(_BROWSER_DIR, "chrome-linux64", "chrome")
CHROMEDRIVER_BIN = os.path.join(_BROWSER_DIR, "chromedriver-linux64", "chromedriver")

WS_URL = os.getenv("QUOTEX_WS_URL", "wss://ws2.quotex.com/socket.io/?EIO=3&transport=websocket")
PAGE_URL = "https://quotex.com/en/trade"
SSID = os.getenv("QUOTEX_SSID")
HEADLESS = os.getenv("PROBE_HEADLESS", "0") == "1"  # default: real window on the xvfb display

# Runs inside the page; resolves with the list of frames received.
WS_SCRIPT = """
const [url, ssid, done] = [arguments[0], arguments[1], arguments[arguments.length - 1]];
const out = {opened: false, closed: null, frames: []};
let ws;
try { ws = new WebSocket(url); } catch (e) { out.error = String(e); return done(out); }
const finish = () => done(out);
const timer = setTimeout(finish, 12000);
ws.onopen = () => { out.opened = true; };
ws.onclose = (e) => { out.closed = e.code; clearTimeout(timer); finish(); };
ws.onerror = () => { out.error = 'websocket error'; };
ws.onmessage = (m) => {
  const data = String(m.data);
  out.frames.push(data.slice(0, 200));
  if (data === '40' && ssid) {
    ws.send('42' + JSON.stringify(['authorization', {session: ssid, isDemo: 1, tournamentId: 0}]));
  }
  if (data.indexOf('s_authorization') !== -1 || data.indexOf('"liveBalance"') !== -1) {
    clearTimeout(timer); setTimeout(finish, 500);
  }
};
"""


def main() -> int:
    options = uc.ChromeOptions()
    # throwaway profile so nothing is shared with any other Chrome on the machine
    profile_dir = tempfile.mkdtemp(prefix="quotex_probe_")
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    if HEADLESS:
        options.add_argument("--headless=new")

    print(f"[1/3] Starting Chrome ({'headless' if HEADLESS else 'on virtual display'})...")
    driver_kwargs = {}
    if os.path.exists(CHROME_BIN):  # private Chrome installed by vps/setup_ubuntu.sh
        driver_kwargs = {
            "browser_executable_path": CHROME_BIN,
            "driver_executable_path": CHROMEDRIVER_BIN,
        }
    driver = uc.Chrome(options=options, use_subprocess=True, **driver_kwargs)
    try:
        driver.set_script_timeout(30)
        driver.get(PAGE_URL)
        time.sleep(6)  # let any Cloudflare check run
        title = driver.title
        blocked = any(s in driver.page_source.lower() for s in ("just a moment", "attention required", "access denied"))
        print(f"      title: {title!r}  cloudflare-challenge/blocked: {blocked}")
        if blocked:
            print("RESULT: page blocked - this IP/browser combination is rejected by Cloudflare.")
            return 2

        print("[2/3] Opening WebSocket from inside the page...")
        result = driver.execute_async_script(WS_SCRIPT, WS_URL, SSID)
        print(json.dumps(result, indent=2))
        if not result.get("opened"):
            print("RESULT: WebSocket refused even from a real browser on this machine.")
            return 3
        print("      WebSocket opened.")

        if SSID:
            print("[3/3] Authorization result is in the frames above (look for s_authorization / balance).")
        else:
            print("[3/3] Skipped (set QUOTEX_SSID to test authorization).")
        print("RESULT: OK - browser approach works from this machine.")
        return 0
    finally:
        driver.quit()
        shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
