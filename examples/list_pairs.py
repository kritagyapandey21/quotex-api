"""
List every pair on Quotex with its payout.

    python examples/list_pairs.py              # open pairs with payout >= 80
    python examples/list_pairs.py --min 85     # different threshold
    python examples/list_pairs.py --all        # include closed pairs too

Needs QUOTEX_SSID (env var). The raw instrument list is also saved to instruments_raw.json
so the field layout can be checked.

Field positions follow the Quotex instruments/list layout as commonly documented:
    [id, symbol, name, type, ?, payout%, ..., is_open at index 14]
Check them against the "first raw entries" printed below before trusting the table.
"""

import argparse
import asyncio
import json
import os
import sys

from QuotexAPI import QuotexAPI

IDX_SYMBOL, IDX_NAME, IDX_TYPE, IDX_PAYOUT, IDX_OPEN = 1, 2, 3, 5, 14


def parse(entry):
    def at(i, default=None):
        return entry[i] if isinstance(entry, list) and len(entry) > i else default

    payout = at(IDX_PAYOUT)
    return {
        "symbol": at(IDX_SYMBOL),
        "name": at(IDX_NAME),
        "type": at(IDX_TYPE),
        "payout": float(payout) if isinstance(payout, (int, float)) else None,
        "open": bool(at(IDX_OPEN)),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=80.0, help="minimum payout percent")
    ap.add_argument("--all", action="store_true", help="include closed pairs")
    args = ap.parse_args()

    ssid = os.getenv("QUOTEX_SSID")
    if not ssid:
        print("Set QUOTEX_SSID first.")
        return 1

    api = QuotexAPI(ssid=ssid)  # demo account
    await api.connect()
    try:
        await api.login_with_ssid(ssid)

        data = None
        for _ in range(100):  # up to ~20 s
            data = api.connection.last_event("instruments/list")
            if isinstance(data, list) and data:
                break
            await asyncio.sleep(0.2)
        if not isinstance(data, list) or not data:
            seen = sorted(api.connection._last_events)
            print(f"No instrument list received (events seen: {seen})")
            return 2

        with open("instruments_raw.json", "w", encoding="utf-8") as f:
            json.dump(data, f)

        print(f"Received {len(data)} instruments. First raw entries (to verify field positions):")
        for entry in data[:2]:
            print("  ", json.dumps(entry)[:300])
        print()

        rows = [parse(e) for e in data]
        rows = [r for r in rows if r["symbol"] and r["payout"] is not None]
        picked = [r for r in rows if r["payout"] >= args.min and (args.all or r["open"])]
        picked.sort(key=lambda r: (-r["payout"], r["symbol"]))

        print(f"{'SYMBOL':<16}{'NAME':<24}{'TYPE':<14}{'PAYOUT':>7}  STATUS")
        for r in picked:
            print(
                f"{r['symbol']:<16}{str(r['name'])[:23]:<24}{str(r['type'])[:13]:<14}"
                f"{r['payout']:>6.0f}%  {'open' if r['open'] else 'closed'}"
            )
        print(f"\n{len(picked)} pairs with payout >= {args.min:g}% "
              f"({'open and closed' if args.all else 'open only'}) out of {len(rows)} total")
        return 0
    finally:
        await api.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
