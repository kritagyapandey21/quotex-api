"""
Capture what Quotex sends for one pair, to learn the price / 1-minute candle message formats.

    python examples/capture_pair.py                 # first open OTC pair with payout >= 80
    python examples/capture_pair.py EURUSD_otc      # a specific pair

Logs in on the DEMO account, subscribes to the pair, sends several candidate history
requests, listens for ~20 s and writes everything to capture.json. It does not place trades.
Prints one sample per distinct event, so the output is short enough to paste back.
"""

import asyncio
import json
import os
import sys
import time

from QuotexAPI import QuotexAPI

LISTEN_SECONDS = 20


async def main() -> int:
    ssid = os.getenv("QUOTEX_SSID")
    if not ssid:
        print("Set QUOTEX_SSID first.")
        return 1

    api = QuotexAPI(ssid=ssid)  # demo
    await api.connect()
    frames = []

    def on_event(msg):
        frames.append({"t": round(time.time() - t0, 2), "event": msg["event"], "data": msg["data"]})

    try:
        await api.login_with_ssid(ssid)

        instruments = api.connection.last_event("instruments/list") or []
        pair = sys.argv[1] if len(sys.argv) > 1 else None
        if not pair:
            for e in instruments:
                if isinstance(e, list) and len(e) > 14 and e[14] and e[5] >= 80:
                    pair = e[1]
                    break
        if not pair:
            print("No open pair with payout >= 80 found.")
            return 2
        print(f"Capturing pair: {pair}")

        t0 = time.time()
        api.connection._message_handler = on_event  # sees every event from now on

        now = int(time.time())
        requests = [
            ("instruments/follow", pair),
            ("depth/follow", pair),
            ("chart_notification/get", {"asset": pair, "version": "1.0.0"}),
            ("history/load", {"asset": pair, "index": now * 1000, "time": now, "offset": 3600, "period": 60}),
            ("history/load/line", {"asset": pair, "index": now * 1000, "time": now, "offset": 3600}),
        ]
        for event, data in requests:
            await api.connection.send_socketio_event(event, data)
            await asyncio.sleep(0.5)

        await asyncio.sleep(LISTEN_SECONDS)
    finally:
        await api.disconnect()

    with open("capture.json", "w", encoding="utf-8") as f:
        json.dump(frames, f)

    by_event = {}
    for fr in frames:
        by_event.setdefault(fr["event"], []).append(fr)
    print(f"\n{len(frames)} events captured, {len(by_event)} distinct:\n")
    for name, items in by_event.items():
        sample = json.dumps(items[0]["data"])
        print(f"- {name}  x{len(items)}  (first at {items[0]['t']}s)")
        print(f"    {sample[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
