# tackle.md — problems we hit, how we solved them, and how it works now

Project: unofficial async Python client for the Quotex broker (`QuotexAPI`).
Repo: https://github.com/kritagyapandey21/quotex-api (branch `main`).
Written after getting the library to log in and read live data from a VPS on 2026-10-25.

---

## 1. Where we are (summary)

| Capability | Status | Verified how |
|---|---|---|
| Resolve and reach Quotex servers | Working | DNS and handshake tests |
| WebSocket connection past Cloudflare | **Working only through a real Chrome** | VPS run, Ubuntu 25.10 |
| SSID login (demo account) | Working | `s_authorization` received, "Authenticated successfully" |
| Read balances | Working | Demo $9,861.30 / Real $0.00 (matches the browser) |
| Read the full instrument list with payouts | Working | 92 instruments, 29 open at payout >= 80% |
| Live prices (ticks) per pair | Working | `quotes/stream` captured on the VPS (about 2 ticks/s per pair) |
| Live payout per pair | Working | `depth/change` captured on the VPS (e.g. `["AUDCAD_otc", 91]`) |
| Past candle history from Quotex | **Not working** | `history/load/line` returns empty, `history/load` never replies (16 variants tried) |
| 1-minute candles built from live ticks | **Written, simulated, not yet run on Quotex** | `examples/collect_candles.py`; simulated feed passes all checks |
| Place trades, track results | **Not tested** | Code exists, uses guessed message formats |
| Email + password login | **Not working / not tested** | Library only simulates it |
| Unattended running (SSID refresh) | **Not built** | SSID expires after hours |

The library runs end to end on the VPS for: connect, login, balances, instrument list.

---

## 2. Problems we faced and how each was solved

### 2.1 The project was not what its docs claimed
- **Problem:** `README.md` roadmap says "currently uses mock data" while `IMPLEMENTATION_STATUS.md` says "production ready". Reading the code showed the truth is in between: connection and login code is real, but `InstrumentService.get_assets()` returns five hard-coded fake assets, and `get_balances()` silently returned a made-up $10,000 demo balance when the request timed out.
- **Why it matters:** in a trading library, fabricated data looks like real data.
- **Fix:**
  - `get_balances()` now reads the balance the server really sent, and **raises an error** if none arrives (the error lists which events did arrive).
  - The pair list is read from the real `instruments/list` data in `examples/list_pairs.py`.
- **Still open:** `api.get_assets()` itself is still the mock. Do not use it. (See section 7.)

### 2.2 The old domain no longer resolved
- **Problem:** the code pointed at `ws2.qxbroker.com` / `qxbroker.com`. On this network, `curl: (6) Could not resolve host`. The user confirmed the site moved to `quotex.com`.
- **Check:** DNS lookups showed `quotex.com`, `ws2.quotex.com`, `ws.quotex.com` resolve; the qxbroker names do not.
- **Fix:** replaced `qxbroker.com` with `quotex.com` in `config.py`, `connection.py`, `curl_websocket.py`, `chrome_websocket.py`, `.env.example`. Default WebSocket URL is now `wss://ws2.quotex.com/socket.io/?EIO=3&transport=websocket`. The user later confirmed from the browser's Network tab that this is exactly the URL the real site uses.

### 2.3 Windows terminal crashed on emoji
- **Problem:** `UnicodeEncodeError: 'charmap' codec can't encode character` when `examples/test_ssid.py` printed emoji on Windows.
- **Fix:** run with `PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8=1`). Not a connection problem. No code change.

### 2.4 Cloudflare blocks every Python-based connection (the main obstacle)
This took the longest. What we saw, in order:

1. **HTTP long-polling fallback fails.** The library's old transport used Socket.IO polling. The handshake worked (it returned a session id), but every following request returned `400 {"code":1,"message":"Session ID unknown"}`. We tried two browser fingerprints (chrome110, chrome124), kept all cookies (`io`, `__cf_bm`), and tried `ws.` and `ws2.` hosts. 0 of 6 attempts per host produced a usable session. Conclusion: polling is not usable on the current servers.
2. **Direct WebSocket from Python is refused with `403`.** We tested `curl_cffi` with five Chrome fingerprints (110, 124, 131, 136, default), with and without the `permessage-deflate` extension header.
3. **Copying the browser's Cloudflare cookie did not help.** We added `QUOTEX_CF_CLEARANCE` and `QUOTEX_USER_AGENT` settings and tried the user's real `cf_clearance` cookie plus their exact browser User-Agent (Chrome 156). Still `403`.
4. **We captured exactly what the real browser sends** (DevTools > Network > WS > Headers): `Origin: https://quotex.com`, `Pragma/Cache-Control: no-cache`, `Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits`, `Accept-Language`, `Accept-Encoding`, the same User-Agent. **The browser sends no Cookie header at all** on this WebSocket, because `cf_clearance` belongs to `quotex.com`, not `ws2.quotex.com`. We reproduced every header exactly. Still `403`.
5. **Conclusion:** the page itself loads (`200`) with or without cookies, and the Socket.IO server answers plain requests (`400 Bad request`), so the block is not about headers or cookies. Cloudflare decides on something a Python HTTP client cannot imitate (network-level browser fingerprint and bot scoring). Only a real browser passes.

**Solution:** open the WebSocket from JavaScript inside a real Chrome page, and bridge messages to Python. See section 3.

The `QUOTEX_CF_CLEARANCE` / `QUOTEX_USER_AGENT` settings are harmless but turned out not to be useful for this endpoint. They can be removed.

### 2.5 Hidden browser on a home PC was blocked, a browser on the VPS was not
- **Local test:** headless Chrome on the user's Windows PC: `Cloudflare blocked the browser page`. The user also did not want a visible browser window.
- **VPS test (`vps/probe_browser.py`):** real Chrome (not headless) on a virtual display (`xvfb`) on the VPS: page loaded, `cloudflare-challenge/blocked: False`, WebSocket opened, handshake `0{"sid":...}` then `40` received.
- **Takeaway:** hidden headless Chrome is more detectable than a normal Chrome on a virtual screen. That is why the VPS setup uses `xvfb-run` and the code only falls back to `--headless=new` when there is no `DISPLAY`. The VPS IP (Indian hosting provider) was not blocked; this is not guaranteed for other providers.

### 2.6 The VPS already hosts other things — must not interfere
The VPS (Ubuntu 25.10, 3.8 GB RAM, no swap) runs nginx on 80/443, three uvicorn apps (8001, 8010, 8101), Postgres, Docker, PM2, and two bots. Safety measures:
- Checked running processes, listening ports, and memory first (`ss -tlnp`, `ps aux`, `free -h`) before installing anything.
- **Nothing system-wide is replaced.** Python packages go into `~/quotex-api/.venv`. Chrome and chromedriver are downloaded into `~/quotex-api/vps/browser/` (Chrome for Testing), so any system Chrome is untouched. `apt` only installs *missing* libraries with `--no-install-recommends` and never upgrades or removes.
- No services, no PM2 entry, no open ports, no fixed X display (`xvfb-run -a` picks a free one), a throwaway Chrome profile per run (deleted on exit).
- **Hard limits on every run:** `systemd-run --scope -p MemoryMax=900M -p MemorySwapMax=0 -p CPUQuota=50% nice -n 10 ...`. With no swap, a runaway Chrome could otherwise trigger the kernel OOM killer on someone else's process; the cap means only this job can be killed.
- After install, apt reported "No services need to be restarted" and "No containers need to be restarted". `dmesg` showed no OOM kills.

### 2.7 Setup-script bugs we hit on the real VPS
| Symptom | Cause | Fix |
|---|---|---|
| Would fail as root | script used `sudo`, which may not exist for root | `SUDO` variable: empty when running as root |
| `$SUDO` undefined crash (`set -u`) | my first patch only half-applied | defined `SUDO` before use |
| Package names wrong on newer Ubuntu | 24.04+ renamed libs with `t64` suffix (`libasound2t64`, `libgtk-3-0t64`) | `pick()` helper uses the `t64` name when it exists |
| `Chrome ... chrome_crashpad_handler: Permission denied` then `cannot connect to chrome` | Python's zip extraction drops the executable bit, so Chrome's helper binary could not start and Chrome died at launch | `chmod -R u+x vps/browser` (now inside the setup script) |

How we found the crashpad cause: `dmesg` ruled out an OOM kill, then running Chrome by hand printed the exact error.

### 2.8 Bugs inside the library itself
1. **Every message was processed twice.** The transport both queued each message and fired a callback, and the connection service handled both. Fix: no callback; one reader loop.
2. **Binary data could not be read.** After login Quotex sends data (balance, instruments, orders, history) as Socket.IO *binary events*: a text frame like `451-["s_balance/list",{"_placeholder":true,"num":0}]` followed by a separate binary frame (a Blob) holding the real JSON. The old parser only understood text. Fix: the browser bridge re-assembles each pair into a normal `42["s_balance/list", <payload>]` frame, so the rest of the library reads it like any event.
3. **First version of that re-assembly had an ordering bug.** Blobs are decoded asynchronously, so a later frame could overtake an earlier one and the event name got attached to the wrong data (balance timed out although login worked). Fix: a promise chain (`qx.chain`) forces strict arrival order; the event name is captured at the moment the placeholder arrives, not after decoding.
4. **The SSID was printed in logs.** Debug `print`s and an INFO log line included the full login message, which contains the SSID. Removed the prints; the log now says only `Sending Socket.IO event 'authorization'`.
5. **Pushed data was missed.** The server pushes balance/instruments right after login, before the library subscribed. Fix: the connection service now remembers the latest payload for every event (`last_event(name)`), and services read from that.

### 2.9 Getting per-pair data (prices, payout, candles)
Goal: for every pair with payout >= 80%, collect the data needed to place a 1-minute trade, including 1-minute candle data.

1. **Capture (`examples/capture_pair.py`).** Logged in on demo, followed one pair (`AUDCAD_otc`, 91%) and sent several candidate requests. Quotex answered with 8 event types in 20 s (79 events):
   - `quotes/stream` (51 events): live ticks, payload `[["AUDCAD_otc", 1792934007.654, 1.01653, 0]]` = symbol, unix timestamp (seconds, with ms), price, a trailing 0 of unknown meaning.
   - `depth/change` (22 events): live payout, payload `[["AUDCAD_otc", 91]]`.
   - `s_balance/list`, `settings/list`, `orders/opened/list`, `s_drawing/load`, `orders/closed/list` (pushed once after login).
   - `history/load/line`: replied `{"index": ..., "data": []}`, empty.
   - `history/load`: no reply.
2. **What the closed-orders record teaches.** Fields: `id`, `openTime`, `closeTime`, `openTimestamp`, `closeTimestamp`, `asset`, `amount`, `command`, `openPrice`, `closePrice`, `profit`, `percentProfit`, `percentLoss`, `isDemo`, `currency`. A trade with `command: 1` that won while closing *below* its open price suggests `1` = put and `0` = call. This is inferred, not confirmed by a test trade.
3. **History sweep (`examples/capture_history.py`).** Sent 16 variants (event `history/load/line` or `history/load`; time in seconds or milliseconds; offset 900 or 3600; with or without `period: 60`), matched replies by a unique `index`. Result: all 8 `history/load/line` variants returned `data: 0` items, all 8 `history/load` variants got no reply. The correct history request is still unknown. The reliable way to learn it is to copy the request the real website sends (browser DevTools > Network > WS > Messages while opening a chart); that was explained to the user but not done.
4. **Workaround chosen by the user: build candles ourselves from live ticks.** `quotes/stream` gives every price tick, so a 1-minute open/high/low/close candle can be built per pair. History therefore only starts when collection starts (about 6 complete candles after 6-7 minutes).
5. **Collector design (`examples/collect_candles.py`)**, with the user's rules:
   - Follow every open pair (`instruments/follow`) so payout changes for all pairs are seen.
   - Track only pairs whose payout is >= `--min` (default 80); ticks of other pairs are discarded.
   - A tracked pair whose payout falls below the threshold has its stored candles **deleted immediately** (`--grace N` delays this by N seconds to avoid wiping on a 79/80 flap; default 0 = immediate, as requested).
   - When its payout rises again, tracking restarts from empty.
   - A tracked pair with no ticks for `--stale` seconds (default 300) is dropped too, because a gap makes candles misleading; it restarts on its next tick.
   - **Sliding window:** only the newest `--window` candles per pair are kept (default 30, enough for RSI 14 / EMA 20 / Bollinger 20). Older rows are deleted on every new candle.
   - Candles are cleared at startup (a restart leaves a data gap).
   - Storage: SQLite (`candles.db`). Tables `pairs(symbol, name, type, payout, is_open, updated_at, raw)` and `candles(symbol, minute_ts, open, high, low, close, ticks)`. Only completed minutes are stored; the in-progress candle lives in memory. The first candle after start is usually partial.
   - Watchdog: no ticks for 90 s means the connection is dead, so it exits with a message (there is no auto-reconnect yet).
   - `--show` prints stored data (pairs >= min, candle counts, last candles) without any network access.
6. **How it was tested without Quotex.** The storage and display part was run on a fake database. The collection logic was run through a simulated feed (fake client, 40 minutes of synthetic ticks, payout changes): with a window of 5, each qualifying pair held exactly the newest 5 candles; a 70% pair stored nothing; dropping a pair to 79% deleted its 5 candles instantly and ticks while below 80 were ignored; raising it to 82% restarted it from empty with no old data; a pair at 81% began tracking; all open pairs were followed. The real run on the VPS is still to be done, and the open question there is whether following about 30 pairs at once works (the status line reports how many pairs are tracked).

---

## 3. How it works now

### 3.1 Flow
```
your script
  -> QuotexAPI.connect()
       ConnectionService (transport = auto)
         1) CurlWebSocketTransport  -> native WebSocket via curl_cffi
              refused with 403 on Quotex (fast failure, ~1 s)
         2) BrowserWebSocketTransport -> starts real Chrome (undetected-chromedriver)
              opens https://quotex.com/en/trade (passes Cloudflare)
              injects JS that opens wss://ws2.quotex.com/socket.io/?EIO=3&transport=websocket
              Python polls a message buffer every 50 ms and sends via ws.send()
  -> QuotexAPI.login_with_ssid(ssid)
       sends 42["authorization",{"session":"<ssid>","isDemo":1,"tournamentId":0}]
       waits for 42["s_authorization"]
  -> server pushes: instruments/list, s_balance/list, settings/list,
       orders/opened/list, orders/closed/list, history/list/v2 ...
  -> ConnectionService stores the latest payload per event (last_event)
```

### 3.2 Transport selection
Environment variable `QUOTEX_TRANSPORT`:
- `auto` (default): try curl, then Chrome.
- `curl`: curl only (will fail on Quotex today; may allow HTTP polling fallback).
- `browser`: Chrome only.

`QUOTEX_BROWSER_HEADLESS=1` forces headless Chrome (more likely to be blocked). Default: headless only if there is no display (Linux without `DISPLAY`). On a server run under `xvfb-run -a`.

### 3.3 Files added or changed
| File | What |
|---|---|
| `QuotexAPI/utils/browser_websocket.py` | NEW. Chrome-based transport with the JS bridge and binary-event re-assembly |
| `QuotexAPI/utils/curl_websocket.py` | native WebSocket attempt first; `allow_polling` flag; no body logging |
| `QuotexAPI/services/connection.py` | transport selection, single message path, `last_event()`, domain change, no secret logging |
| `QuotexAPI/services/account.py` | balance from server push; raises instead of fake data |
| `QuotexAPI/config.py` | new settings: `transport`, `cf_clearance`, `user_agent`; new default domain |
| `vps/setup_ubuntu.sh` | isolated install (venv, private Chrome, only-missing apt packages) |
| `vps/probe_browser.py` | standalone check: does a real Chrome on this machine pass Cloudflare and open the WebSocket |
| `examples/list_pairs.py` | NEW. Lists all pairs with payout, filters by minimum payout |
| `examples/capture_pair.py` | NEW. Logs in (demo), follows one pair, records every event Quotex sends (learned the price / payout formats) |
| `examples/capture_history.py` | NEW. Sweeps 16 history-request variants to find one that returns candles (none did) |
| `examples/collect_candles.py` | NEW. Builds 1-minute candles from live ticks for all pairs >= min payout, with sliding window and drop/restart rules; `--show` to read the data |
| `.gitignore`, `.gitattributes` | ignore `vps/browser/`, `instruments_raw.json`; keep `.sh` files LF |

Git history on `main` (user's repo): domain change + probe, setup fixes, browser transport, bridge ordering fix, list_pairs.

---

## 4. How to run it on the VPS

One-time install (project folder only, plus any missing apt libraries):
```bash
cd ~
git clone https://github.com/kritagyapandey21/quotex-api.git
cd quotex-api
bash vps/setup_ubuntu.sh
```

Connection check (no login):
```bash
xvfb-run -a .venv/bin/python vps/probe_browser.py      # expect: RESULT: OK
```

Normal runs: always pass the SSID without echoing it, and keep the limits:
```bash
cd ~/quotex-api
git pull
read -rs -p "SSID: " QUOTEX_SSID; export QUOTEX_SSID; echo
systemd-run --scope -E QUOTEX_SSID -p MemoryMax=900M -p MemorySwapMax=0 -p CPUQuota=50% \
  nice -n 10 xvfb-run -a .venv/bin/python examples/test_ssid.py      # login + balances
# or:  ... examples/list_pairs.py [--min 85] [--all]                 # pair list
unset QUOTEX_SSID
```
Each run takes about 10-20 s and uses roughly 500-900 MB while Chrome is open.

### Collecting candles (10-minute test run)
```bash
cd ~/quotex-api
git pull
read -rs -p "SSID: " QUOTEX_SSID; export QUOTEX_SSID; echo
systemd-run --scope -E QUOTEX_SSID -p MemoryMax=900M -p MemorySwapMax=0 -p CPUQuota=50% \
  nice -n 10 xvfb-run -a .venv/bin/python examples/collect_candles.py --minutes 10
unset QUOTEX_SSID
.venv/bin/python examples/collect_candles.py --show --last 5      # read what was stored
```
Options: `--min 85` (payout threshold), `--window 60` (candles kept per pair), `--grace 30` (seconds below threshold before deleting), `--stale 300` (seconds without ticks before dropping a pair), `--minutes N` (stop after N minutes; 0 = run until stopped).
Status line every 30 s: pairs tracked, candles stored, drops so far. Running it detached in the background (a named, limited `systemd-run` job, stoppable with one command) is planned but not set up yet.

### Capture scripts (for investigating Quotex messages)
`examples/capture_pair.py [PAIR]` and `examples/capture_history.py [PAIR]`, run the same way as above. Output goes to `capture.json` / `capture_history.json` (git-ignored).

To undo everything: `rm -rf ~/quotex-api` (the only change outside it is the extra apt libraries).

### Getting the SSID
Browser > quotex.com/en/trade (logged in) > F12 > Application > Cookies > `ssid`, or the `session` value in the WebSocket `authorization` message (Network > WS > Messages). Use only the token. It expires after a few hours.

---

## 5. Results from the live runs

**Login + balances (demo account):**
```
[WEBSOCKET] Upgrade failed (... Refused WebSocket upgrade: 403 ...)
[BROWSER] Starting Chrome...
[BROWSER] WebSocket open
Authenticated successfully!
Demo Balance: $9,861.30
Real Balance: $0.00
```

**Pair list on 2026-10-25 (a Sunday):** 92 instruments, 29 open with payout >= 80%, all OTC pairs (regular forex pairs are closed at weekends). Best: `EURCAD_otc` and `GBPJPY_otc` at 94%, `AUDJPY_otc` 93%, then twelve pairs at 92% (AUDNZD, DOTUSD, ETCUSD, EURNZD, EURUSD, GBPUSD, UKBrent, USDBDT, USDMXN, USDPKR, XAUUSD, XRPUSD). Lowest on the list: `CADCHF_otc` at 80%.

Instrument entry layout (confirmed against real data):
`[id, symbol, name, type, ?, payout%, ... , is_open at index 14, ...]`
e.g. `[36, "AUDCAD", "AUD/CAD", "currency", 5, 80, 60, 30, 3, 0, 67, 0, [[30,"put"],...], 1792972800, false, ...]`.
The meaning of the numbers between the payout and the open flag, and whether the payout differs by expiry, is not yet identified.

**Live price and payout stream (capture, pair `AUDCAD_otc`, 20 s):** 51 `quotes/stream` ticks and 22 `depth/change` payout updates for the one followed pair, e.g. `[["AUDCAD_otc", 1792934007.654, 1.01653, 0]]` and `[["AUDCAD_otc", 91]]`.

**History requests:** 16 variants tried, none returned candle data (see section 2.9).

---

## 6. Security notes
- The SSID is a live session token. The browser session we used was on the **real-money account** (`"isDemo":0` in the browser's own message). Every library run uses `isDemo: 1` (demo). Keep it that way until trade flow is verified.
- The token was pasted in chat several times. After testing: log out of Quotex in the browser (invalidates it) and use a fresh token for anything that runs long-term.
- Never commit `.env`; `.gitignore` already excludes it. The repo contains no tokens or cookies (checked before every push).
- Passing the SSID: use `read -rs` + `systemd-run -E` as above so it does not land in shell history or logs.
- Automating a trading account against the platform's bot protection may violate Quotex's terms of service and could get the account restricted.

---

## 7. Known limitations and what is left

1. **`api.get_assets()` is still a mock.** Make it read `instruments/list` (the parsing already exists in `examples/list_pairs.py`) and add a `min_payout` filter. `get_payout()` depends on it.
2. **Trading is untested.** `buy()`, `place_trade()`, `track_trade()`, `wait_for_result()`, `cancel_trade()`, `get_open_trades()`, `get_trade_history()` use message formats nobody verified. Test on demo with $1.
3. **`switch_account()` is wrong.** It sends a `switch_account` message that Quotex does not use; real switching probably means re-authorizing with a different `isDemo`.
4. **Email + password login is a stub.** It sends a made-up `login` event. Real Quotex login is over HTTPS with its own protections and needs separate work.
5. **SSID expiry.** There is no automatic refresh. A long-running process dies when the token expires. This is the biggest blocker for unattended use.
6. **Candle history from Quotex is unsolved.** The real history request is unknown (see 2.9). Candles are built from live ticks instead, so there is no history before collection starts. Library methods for prices/candles (`get_candles`, `subscribe_candles`) are still the old unverified code; the working path is in `examples/collect_candles.py`.
7. **Collector not yet run against real Quotex.** Unknown: whether following about 30 pairs at once works, and how steady the tick stream is for all of them. No auto-reconnect; it exits after 90 s without ticks. It needs a fresh SSID each run (expires after hours) and runs in the foreground only.
8. **Payout per expiry** not identified. Each instrument entry carries more numbers after the payout (for example `60, 30, 3, 0, 67`), and a list like `[[30,"put"],[60,"call"],...]`, whose meaning is unknown. The `depth/change` payout is a single number per pair; whether it applies to the 1-minute expiry is not confirmed.
9. **Direction code** (`command` 0 = call, 1 = put) is inferred from one winning past order, not tested. The order-placement message (`orders/open`) format is unverified.
10. **No reconnect.** `_reconnect()` is a stub ("Reconnection would happen here"). If Chrome or the socket drops, the process must be restarted.
11. **Each run starts Chrome** (about 12 s). A long-lived process would avoid that.
12. **Cloudflare can change.** It may start blocking the VPS IP or the browser setup at any time. The probe script (`vps/probe_browser.py`) is the quick way to re-check.
13. Documentation in the repo (`IMPLEMENTATION_STATUS.md`, `WEBSOCKET_*`, `SOCKETIO_*`) describes behavior from earlier attempts and overstates what works. Treat this file as the current truth.
14. Old, unused files remain: `chrome_websocket.py` (half-finished earlier browser attempt), many root-level `test_*.py` experiment scripts.
