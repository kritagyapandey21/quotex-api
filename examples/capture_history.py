"""
Find which history request returns candle / price history for a pair.

    python examples/capture_history.py [PAIR]

Sends many variants of history/load and history/load/line (different time units, offsets and
periods), matches each reply by its index, and prints which variants returned data.
Demo account only. Places no trades. Full replies are saved to capture_history.json.
"""

import asyncio
import json
import os
import sys
import time

from QuotexAPI import QuotexAPI


async def main() -> int:
    ssid = os.getenv("QUOTEX_SSID")
    if not ssid:
        print("Set QUOTEX_SSID first.")
        return 1

    api = QuotexAPI(ssid=ssid)
    await api.connect()
    replies = []  # (event, data)
    t0 = time.time()

    def on_event(msg):
        if "history" in msg["event"] or "candle" in msg["event"]:
            replies.append((msg["event"], msg["data"]))

    try:
        await api.login_with_ssid(ssid)
        pair = sys.argv[1] if len(sys.argv) > 1 else None
        if not pair:
            for e in api.connection.last_event("instruments/list") or []:
                if isinstance(e, list) and len(e) > 14 and e[14] and e[5] >= 80:
                    pair = e[1]
                    break
        print(f"Pair: {pair}")
        api.connection._message_handler = on_event

        await api.connection.send_socketio_event("instruments/follow", pair)
        await asyncio.sleep(1)

        now = int(time.time())
        variants = []
        n = 0
        for event in ("history/load/line", "history/load"):
            for time_unit, tv in (("s", now), ("ms", now * 1000)):
                for offset in (900, 3600):
                    for with_period in (False, True):
                        n += 1
                        payload = {"asset": pair, "index": 10_000 + n, "time": tv, "offset": offset}
                        if with_period:
                            payload["period"] = 60
                        variants.append((event, time_unit, offset, with_period, payload))

        for event, time_unit, offset, with_period, payload in variants:
            await api.connection.send_socketio_event(event, payload)
            await asyncio.sleep(0.7)
        await asyncio.sleep(3)
    finally:
        await api.disconnect()

    with open("capture_history.json", "w", encoding="utf-8") as f:
        json.dump(replies, f)

    def size(d):
        if isinstance(d, dict):
            return {k: len(v) for k, v in d.items() if isinstance(v, (list, dict))}
        return len(d) if isinstance(d, (list, str)) else d

    print(f"\n{len(replies)} history replies\n")
    by_index = {}
    for ev, d in replies:
        idx = d.get("index") if isinstance(d, dict) else None
        by_index.setdefault(idx, []).append((ev, d))

    print(f"{'event':<20}{'time':<5}{'offset':<8}{'period':<8}reply")
    for event, time_unit, offset, with_period, payload in variants:
        got = by_index.get(payload["index"])
        if not got:
            reply = "no reply"
        else:
            reply = "; ".join(f"{ev} {size(d)}" for ev, d in got)
        print(f"{event:<20}{time_unit:<5}{offset:<8}{str(with_period):<8}{reply}")

    print("\nFirst non-empty reply sample:")
    for ev, d in replies:
        if isinstance(d, dict) and any(isinstance(v, list) and v for v in d.values()):
            print(ev, json.dumps(d)[:800])
            break
    else:
        print("(none - every reply was empty)")
    print("\nOther history events without an index:")
    for ev, d in replies:
        if not (isinstance(d, dict) and "index" in d):
            print(" ", ev, json.dumps(d)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
