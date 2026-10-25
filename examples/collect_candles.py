"""
Collect live prices for every open pair and build 1-minute candles (open/high/low/close).

    python examples/collect_candles.py                 # collect until stopped (Ctrl+C)
    python examples/collect_candles.py --minutes 10    # stop after 10 minutes
    python examples/collect_candles.py --show          # print what is stored (no network)
    python examples/collect_candles.py --show --min 85 --last 5   # add --all for closed pairs too
    python examples/collect_candles.py --show --json                  # machine-readable, all stored candles

Quotex's own history request is not usable yet, so candles are built from the live tick stream
(`quotes/stream`): history starts when collection starts. After ~7 minutes there are ~6 complete
candles per pair.

Only pairs whose payout is >= --min (default 80) are tracked:
  - a pair that falls below --min has its candles deleted (after --grace seconds, default 0)
  - when it rises back, it starts again from empty
  - a tracked pair with no ticks for --stale seconds (default 300) is dropped too (data gap)
  - only the newest --window candles (default 30) are kept per pair (sliding window)
  - candles are cleared at startup, because a restart leaves a gap in the data
All open pairs stay followed so a pair's payout rising back above --min is noticed.

Stored in SQLite (default candles.db):
    pairs(symbol, name, type, payout, is_open, updated_at, raw)   <- live payout via depth/change
    candles(symbol, minute_ts, open, high, low, close, ticks)     <- one row per complete minute
The SSID is read from QUOTEX_SSID or a .env file in the working directory. Demo account only.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time

from QuotexAPI import QuotexAPI
from QuotexAPI.config import load_config

IDX_ID, IDX_SYMBOL, IDX_NAME, IDX_TYPE, IDX_PAYOUT, IDX_OPEN = 0, 1, 2, 3, 5, 14
STALL_SECONDS = 90  # no ticks for this long => exit so a supervisor / the user can restart


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS pairs (symbol TEXT PRIMARY KEY, name TEXT, type TEXT,"
        " payout REAL, is_open INTEGER, updated_at REAL, raw TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS candles (symbol TEXT, minute_ts INTEGER, open REAL, high REAL,"
        " low REAL, close REAL, ticks INTEGER, PRIMARY KEY (symbol, minute_ts))"
    )
    db.commit()
    return db


def show(args) -> int:
    db = open_db(args.db)
    rows = db.execute(
        "SELECT p.symbol, p.name, p.payout, p.is_open,"
        " (SELECT COUNT(*) FROM candles c WHERE c.symbol = p.symbol) AS n"
        " FROM pairs p WHERE p.payout >= ? AND (p.is_open = 1 OR ?) ORDER BY p.payout DESC, p.symbol",
        (args.min, 1 if args.all else 0),
    ).fetchall()
    if not rows:
        print("No pairs stored yet. Run the collector first.")
        return 1
    if args.json:
        out = {}
        for symbol, name, payout, is_open, n in rows:
            candles = db.execute(
                "SELECT minute_ts, open, high, low, close, ticks FROM candles WHERE symbol=?"
                " ORDER BY minute_ts",
                (symbol,),
            ).fetchall()
            out[symbol] = {"name": name, "payout": payout, "open": bool(is_open),
                           "candles": [list(c) for c in candles]}  # [ts, open, high, low, close, ticks]
        print(json.dumps(out))
        return 0
    print(f"{'SYMBOL':<16}{'PAYOUT':>7}  {'OPEN':<6}{'CANDLES':>8}   LAST CANDLES (o/h/l/c)")
    for symbol, name, payout, is_open, n in rows:
        last = db.execute(
            "SELECT minute_ts, open, high, low, close FROM candles WHERE symbol=?"
            " ORDER BY minute_ts DESC LIMIT ?",
            (symbol, args.last),
        ).fetchall()
        text = "  ".join(
            f"{time.strftime('%H:%M', time.localtime(ts))} {o:g}/{h:g}/{l:g}/{c:g}"
            for ts, o, h, l, c in reversed(last)
        )
        print(f"{symbol:<16}{payout:>6.0f}%  {'yes' if is_open else 'no':<6}{n:>8}   {text}")
    ready = sum(1 for r in rows if r[4] >= args.last)
    print(f"\n{len(rows)} pairs with payout >= {args.min:g}%, {ready} have >= {args.last} candles")
    return 0


async def collect(args) -> int:
    ssid = load_config().ssid
    if not ssid:
        print("Set QUOTEX_SSID (env var or .env file).")
        return 1

    db = open_db(args.db)
    api = QuotexAPI(ssid=ssid)  # demo
    await api.connect()

    current = {}  # symbol -> [minute, open, high, low, close, ticks]  (tracked pairs only)
    payout = {}  # symbol -> latest payout percent (all pairs, kept live via depth/change)
    below_since = {}  # symbol -> when its payout first fell under the threshold (for --grace)
    last_tick = {}  # symbol -> time of its latest tick (for --stale)
    stats = {"ticks": 0, "last_tick": time.time(), "candles": 0, "wiped": 0}

    def wipe(symbol, why):
        """Forget a pair: delete its stored candles and its half-built candle."""
        had = db.execute("SELECT COUNT(*) FROM candles WHERE symbol=?", (symbol,)).fetchone()[0]
        db.execute("DELETE FROM candles WHERE symbol=?", (symbol,))
        if current.pop(symbol, None) is not None or had:
            stats["wiped"] += 1
            print(f"- dropped {symbol} ({why}), {had} candles deleted", flush=True)
        below_since.pop(symbol, None)

    def flush(symbol, c):
        db.execute(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)",
            (symbol, c[0], c[1], c[2], c[3], c[4], c[5]),
        )
        # sliding window: keep only the newest --window candles for this pair
        db.execute(
            "DELETE FROM candles WHERE symbol=? AND minute_ts <= ?",
            (symbol, c[0] - args.window * 60),
        )
        stats["candles"] += 1

    def set_payout(symbol, value):
        previous = payout.get(symbol)
        payout[symbol] = value
        db.execute("UPDATE pairs SET payout=?, updated_at=? WHERE symbol=?", (value, time.time(), symbol))
        if value >= args.min:
            below_since.pop(symbol, None)
            if previous is not None and previous < args.min:
                print(f"+ {symbol} payout {value:g}% - tracking starts", flush=True)
        elif symbol in current or db.execute(
            "SELECT 1 FROM candles WHERE symbol=? LIMIT 1", (symbol,)
        ).fetchone():
            if args.grace <= 0:
                wipe(symbol, f"payout fell to {value:g}%")
            else:
                below_since.setdefault(symbol, time.time())

    def on_tick(sym, ts, price):
        if payout.get(sym, 0) < args.min:
            return  # not worth keeping; the pair stays followed so its payout is still watched
        last_tick[sym] = time.time()
        minute = int(ts // 60) * 60
        c = current.get(sym)
        if c is None or c[0] != minute:
            if c is not None and minute > c[0]:
                flush(sym, c)
            current[sym] = [minute, price, price, price, price, 1]
        else:
            c[2] = max(c[2], price)
            c[3] = min(c[3], price)
            c[4] = price
            c[5] += 1

    def on_event(msg):
        name, data = msg["event"], msg["data"]
        try:
            if name == "quotes/stream" and isinstance(data, list):
                for item in data:
                    if isinstance(item, list) and len(item) >= 3:
                        on_tick(item[0], float(item[1]), float(item[2]))
                        stats["ticks"] += 1
                stats["last_tick"] = time.time()
            elif name == "depth/change" and isinstance(data, list):
                for item in data:
                    if isinstance(item, list) and len(item) >= 2:
                        set_payout(item[0], float(item[1]))
        except Exception as e:  # never let one bad frame stop collection
            print(f"bad {name} frame: {e}")

    try:
        await api.login_with_ssid(ssid)

        instruments = []
        for _ in range(100):
            data = api.connection.last_event("instruments/list")
            if isinstance(data, list) and data:
                instruments = data
                break
            await asyncio.sleep(0.2)
        if not instruments:
            print("No instrument list received.")
            return 2

        now = time.time()
        followed = []
        for e in instruments:
            if not (isinstance(e, list) and len(e) > IDX_OPEN):
                continue
            db.execute(
                "INSERT OR REPLACE INTO pairs VALUES (?,?,?,?,?,?,?)",
                (e[IDX_SYMBOL], e[IDX_NAME], e[IDX_TYPE], float(e[IDX_PAYOUT]),
                 1 if e[IDX_OPEN] else 0, now, json.dumps(e)),
            )
            payout[e[IDX_SYMBOL]] = float(e[IDX_PAYOUT])
            if e[IDX_OPEN]:
                followed.append(e[IDX_SYMBOL])
        # candles from an earlier run are stale (there is a gap since): start clean
        db.execute("DELETE FROM candles")
        db.commit()

        api.connection._message_handler = on_event
        for symbol in followed:
            await api.connection.send_socketio_event("instruments/follow", symbol)
            await asyncio.sleep(0.05)
        print(f"Following {len(followed)} open pairs "
              f"({sum(1 for e in instruments if isinstance(e, list) and e[IDX_OPEN] and e[IDX_PAYOUT] >= args.min)}"
              f" with payout >= {args.min:g}%). Candles are saved to {args.db}.")

        start = time.time()
        last_report = start
        while True:
            await asyncio.sleep(1)
            db.commit()
            if args.minutes and time.time() - start >= args.minutes * 60:
                print("Time limit reached.")
                break
            if not api.is_connected or not api.connection._ws or not api.connection._ws.is_connected():
                print("WebSocket closed - exiting (restart to resume; candles are cleared on start).")
                return 3
            if time.time() - stats["last_tick"] > STALL_SECONDS:
                print(f"No ticks for {STALL_SECONDS}s - connection looks dead, exiting.")
                return 3
            now = time.time()
            for symbol, since in list(below_since.items()):  # grace period over, still below
                if now - since >= args.grace and payout.get(symbol, 0) < args.min:
                    wipe(symbol, f"payout stayed below {args.min:g}% for {args.grace:g}s")
            for symbol, t in list(last_tick.items()):  # tracked pair went quiet: gap in data
                if now - t > args.stale:
                    last_tick.pop(symbol, None)
                    wipe(symbol, f"no ticks for {args.stale:g}s")
            if time.time() - last_report >= 30:
                last_report = time.time()
                rows = db.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM candles").fetchone()
                print(f"[{int((time.time() - start) // 60)}m{int((time.time() - start) % 60):02d}s] "
                      f"{len(current)} pairs tracked (payout >= {args.min:g}%), "
                      f"{rows[0]} candles stored over {rows[1]} pairs (window {args.window}), "
                      f"{stats['wiped']} drops so far",
                      flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        db.commit()
        db.close()
        await api.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="candles.db")
    ap.add_argument("--min", type=float, default=80.0, help="payout percent threshold")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = run until stopped)")
    ap.add_argument("--window", type=int, default=30, help="candles kept per pair (sliding window)")
    ap.add_argument("--grace", type=float, default=0, help="seconds a pair may stay below --min before its data is dropped (0 = immediately)")
    ap.add_argument("--stale", type=float, default=300, help="drop a tracked pair's data after this many seconds without ticks")
    ap.add_argument("--show", action="store_true", help="print stored data instead of collecting")
    ap.add_argument("--all", action="store_true", help="--show: include pairs that are closed")
    ap.add_argument("--json", action="store_true", help="--show: print all pairs and candles as JSON (for other programs)")
    ap.add_argument("--last", type=int, default=6, help="--show: candles per pair to print")
    args = ap.parse_args()
    if args.show:
        return show(args)
    try:
        return asyncio.run(collect(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
