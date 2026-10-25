"""
WebSocket transport that runs inside a real Chrome (undetected-chromedriver).

Cloudflare refuses WebSocket upgrades from non-browser clients, so the socket is opened by
JavaScript in a real browser page and bridged to Python by polling a message buffer.

On a server without a display, run under a virtual display:
    xvfb-run -a python your_script.py
"""

import os
import queue
import shutil
import tempfile
import threading
import time
from typing import Callable, Dict, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

PAGE_URL = "https://quotex.com/en/trade"

_BROWSER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vps", "browser"
)

# Installs window.__qx: opens the socket and buffers every frame as text.
# Socket.IO binary events arrive as a text frame `451-["event",{"_placeholder":true,...}]`
# followed by a Blob; they are re-assembled into a normal `42["event",<payload>]` frame so the
# rest of the library can parse them like any other event.
_BRIDGE_JS = """
const url = arguments[0];
window.__qx = {q: [], ws: null, state: 'connecting', err: null, pending: null};
const qx = window.__qx;
const ws = new WebSocket(url);
qx.ws = ws;
ws.onopen = () => { qx.state = 'open'; };
ws.onclose = (e) => { qx.state = 'closed'; qx.err = 'closed ' + e.code; };
ws.onerror = () => { qx.err = 'websocket error'; };
ws.onmessage = async (m) => {
  const d = m.data;
  if (typeof d === 'string') {
    const hit = d.match(/^45\\d*-\\[\\s*"([^"]+)"/);
    if (hit) { qx.pending = hit[1]; return; }
    qx.q.push(d);
    return;
  }
  const buf = await (d.arrayBuffer ? d.arrayBuffer() : new Response(d).arrayBuffer());
  let text = new TextDecoder().decode(buf).replace(/^[\\u0000-\\u001f]+/, '');
  const name = qx.pending || 'binary';
  qx.pending = null;
  let payload;
  try { payload = JSON.parse(text); } catch (e) { payload = text; }
  qx.q.push('42' + JSON.stringify([name, payload]));
};
"""

_DRAIN_JS = "const q = window.__qx.q; window.__qx.q = []; return [window.__qx.state, window.__qx.err, q];"


class BrowserWebSocketTransport:
    """Same interface as CurlWebSocketTransport, backed by a real Chrome page."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.headers = headers or {}  # unused: the browser sets its own headers
        self.driver = None
        self.message_queue: "queue.Queue[str]" = queue.Queue()
        self.running = False
        self._lock = threading.Lock()  # webdriver is not thread-safe
        self._thread: Optional[threading.Thread] = None
        self._profile_dir: Optional[str] = None
        self._on_message: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_close: Optional[Callable] = None

    # -- lifecycle ---------------------------------------------------------------------
    def connect(self) -> bool:
        try:
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            self._profile_dir = tempfile.mkdtemp(prefix="quotex_browser_")
            options.add_argument(f"--user-data-dir={self._profile_dir}")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1366,768")
            # No display (plain SSH session)? fall back to headless; with xvfb-run DISPLAY is set.
            headless = os.getenv("QUOTEX_BROWSER_HEADLESS")
            if headless == "1" or (headless is None and os.name != "nt" and not os.getenv("DISPLAY")):
                options.add_argument("--headless=new")

            kwargs = {}
            chrome = os.path.join(_BROWSER_DIR, "chrome-linux64", "chrome")
            driver_bin = os.path.join(_BROWSER_DIR, "chromedriver-linux64", "chromedriver")
            if os.path.exists(chrome) and os.path.exists(driver_bin):  # from vps/setup_ubuntu.sh
                kwargs = {"browser_executable_path": chrome, "driver_executable_path": driver_bin}

            print("[BROWSER] Starting Chrome...")
            self.driver = uc.Chrome(options=options, use_subprocess=True, **kwargs)
            self.driver.set_script_timeout(30)
            self.driver.get(PAGE_URL)
            time.sleep(5)  # let any Cloudflare check run

            source = self.driver.page_source.lower()
            if any(s in source for s in ("just a moment", "attention required", "access denied")):
                logger.error("Cloudflare blocked the browser page")
                self.close()
                return False

            self.driver.execute_script(_BRIDGE_JS, self.url)
            deadline = time.time() + 15
            while time.time() < deadline:
                state, err, _ = self._drain(keep=True)
                if state == "open":
                    break
                if state == "closed" or err:
                    logger.error(f"Browser WebSocket failed: {err}")
                    self.close()
                    return False
                time.sleep(0.1)
            else:
                logger.error("Browser WebSocket did not open in time")
                self.close()
                return False

            self.running = True
            self._thread = threading.Thread(target=self._pump, daemon=True, name="BrowserPump")
            self._thread.start()
            print("[BROWSER] WebSocket open")
            return True
        except Exception as e:
            logger.error(f"Browser transport failed: {e}")
            print(f"[BROWSER] Error: {e}")
            self.close()
            return False

    def close(self) -> None:
        self.running = False
        with self._lock:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    def is_connected(self) -> bool:
        return self.running and self.driver is not None

    # -- io ----------------------------------------------------------------------------
    def _drain(self, keep: bool = False):
        with self._lock:
            if self.driver is None:
                return "closed", "driver closed", []
            return self.driver.execute_script(_DRAIN_JS)

    def _pump(self) -> None:
        while self.running:
            try:
                state, err, frames = self._drain()
            except Exception as e:
                if self.running:
                    logger.error(f"Browser poll error: {e}")
                break
            for frame in frames:
                self.message_queue.put(frame)
                if self._on_message:
                    try:
                        self._on_message(frame)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
            if state == "closed":
                logger.error(f"Browser WebSocket closed: {err}")
                break
            time.sleep(0.05)
        self.running = False
        if self._on_close:
            self._on_close()

    def send(self, message: str) -> bool:
        try:
            with self._lock:
                if self.driver is None:
                    return False
                self.driver.execute_script("window.__qx.ws.send(arguments[0]);", message)
            return True
        except Exception as e:
            logger.error(f"Browser send error: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    def recv(self, timeout: Optional[float] = None) -> Optional[str]:
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def set_on_message(self, callback: Callable[[str], None]) -> None:
        self._on_message = callback

    def set_on_error(self, callback: Callable[[Exception], None]) -> None:
        self._on_error = callback

    def set_on_close(self, callback: Callable[[], None]) -> None:
        self._on_close = callback
