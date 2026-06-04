#!/usr/bin/env python3
"""Probe when Polymarket fiveminute crypto openPrice becomes available.

This is intentionally standalone and safe: it only performs GET requests.
It waits for the next 5-minute BTC window, then polls Polymarket's web API
from a little before the boundary until openPrice appears or timeout expires.

Usage:
  python3 scripts/probe_open_price_availability.py --asset BTC --pre 20 --timeout 90 --interval 1
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Tuple

PERIOD_SECONDS = 300
POLYMARKET_WEB_API = "https://polymarket.com/api"


def iso_utc(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def window_start(ts: int | float) -> int:
    return int(ts) - (int(ts) % PERIOD_SECONDS)


def fetch_open_price(asset: str, wts: int, timeout: float = 5.0) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    query = urllib.parse.urlencode(
        {
            "symbol": asset.upper(),
            "eventStartTime": iso_utc(wts),
            "variant": "fiveminute",
            "endDate": iso_utc(wts + PERIOD_SECONDS),
        }
    )
    url = f"{POLYMARKET_WEB_API}/crypto/crypto-price?{query}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PolyBot-openPrice-probe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
        data = json.loads(raw)
        open_price = data.get("openPrice")
        close_price = data.get("closePrice")
        return (
            float(open_price) if open_price is not None else None,
            float(close_price) if close_price is not None else None,
            None,
        )
    except Exception as exc:  # print and continue; transient failures are the point of this probe
        return None, None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--pre", type=float, default=20.0, help="seconds before boundary to start polling")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds after boundary to keep polling")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval seconds")
    parser.add_argument("--windows", type=int, default=1, help="number of consecutive new windows to probe")
    args = parser.parse_args()

    for i in range(args.windows):
        now = time.time()
        current = window_start(now)
        target = current + PERIOD_SECONDS
        start_poll_at = target - args.pre
        if now < start_poll_at:
            wait = start_poll_at - now
            print(f"waiting {wait:.1f}s until {iso_utc(start_poll_at)}; target_window={iso_utc(target)}")
            time.sleep(wait)

        deadline = target + args.timeout
        first_seen = None
        print("-" * 80)
        print(
            f"probe_window={iso_utc(target)} end={iso_utc(target + PERIOD_SECONDS)} "
            f"poll_start={iso_utc(time.time())} timeout_at={iso_utc(deadline)}"
        )
        print("offset_s,status,openPrice,closePrice,error")

        while time.time() <= deadline:
            ts = time.time()
            open_price, close_price, err = fetch_open_price(args.asset, target)
            offset = ts - target
            status = "OK" if open_price is not None else "MISS"
            print(
                f"{offset:+.3f},{status},"
                f"{'' if open_price is None else open_price},"
                f"{'' if close_price is None else close_price},"
                f"{'' if err is None else err}"
            )
            if open_price is not None:
                first_seen = ts
                break
            time.sleep(args.interval)

        if first_seen is None:
            print(f"RESULT window={iso_utc(target)} first_openPrice=NOT_SEEN timeout_s={args.timeout:.1f}")
        else:
            print(
                f"RESULT window={iso_utc(target)} first_openPrice_delay_s={first_seen - target:.3f} "
                f"first_seen_at={iso_utc(first_seen)}"
            )

        # If probing multiple windows, loop immediately; next iteration will wait for next boundary.

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
