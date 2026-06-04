#!/usr/bin/env python3
"""Re-resolve logs/trades.csv against Polymarket settlement truth.

The deferred (sub-$5 hold-to-resolution) path used to decide win/loss from a
racy wallet-balance delta and could write the WRONG window's official prices
onto a trade. That produced phantom_confirmed "losses" on trades that actually
won (verified on-chain + via the official close price).

This rebuilds trades.csv using the authoritative source: Polymarket's
crypto-price (Chainlink) close vs open for each trade's OWN window
(symbol=BTC, variant=fiveminute). It preserves every entry row and only
corrects the resolution columns. The original file is archived first.

Run:  python scripts/reresolve_trades.py
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TRADES = os.path.join("logs", "trades.csv")
ARCHIVE_DIR = os.path.join("logs", "archive")
API = "https://polymarket.com/api/crypto/crypto-price"
PERIOD = 300


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_window(window_ts: int):
    """Return (open, close, completed) for a 5-min window, or (None, None, False)."""
    start = int(window_ts)
    q = urllib.parse.urlencode({
        "symbol": "BTC",
        "eventStartTime": iso(start),
        "variant": "fiveminute",
        "endDate": iso(start + PERIOD),
    })
    url = f"{API}?{q}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PolyBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read().decode())
            o, c = d.get("openPrice"), d.get("closePrice")
            if o is not None and c is not None:
                return float(o), float(c), bool(d.get("completed", True))
        except Exception as e:
            if attempt == 2:
                print(f"  ! fetch failed window {window_ts}: {e}")
        time.sleep(0.4)
    return None, None, False


def main():
    if not os.path.exists(TRADES):
        print(f"no {TRADES}")
        return 1
    with open(TRADES, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys()) if rows else []
    if not rows:
        print("trades.csv empty — nothing to do")
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = os.path.join(ARCHIVE_DIR, f"trades_pre_p0fix_{stamp}.csv")
    with open(TRADES, encoding="utf-8") as src, open(archive, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    print(f"archived original -> {archive}\n")

    changed = 0
    print(f"{'win':>5} {'side':<4} {'@px':>5} {'old_won':>7} -> {'new_won':>7}  {'old_$':>7} {'new_$':>7}  open->close")
    for r in rows:
        wts = int(float(r["window_ts"]))
        side = (r.get("side") or "").upper()
        shares = float(r.get("entry_shares") or 0)
        cost = float(r.get("entry_cost") or 0)
        exit_rev = float(r.get("exit_revenue") or 0)
        old_won = r.get("won_resolution")
        old_profit = r.get("profit")

        o, c, completed = fetch_window(wts)
        if o is None or not completed:
            print(f"{r.get('window_time',''):>5} {side:<4} {r.get('entry_price',''):>5}  "
                  f"UNSETTLED — left as-is")
            continue

        winning = "UP" if c >= o else "DOWN"
        won = (winning == side)
        if exit_rev > 0:                      # sold (claim_sell): realized cash
            profit = round(exit_rev - cost, 2)
        elif won:                             # held winner redeems at $1/share
            profit = round(shares * 1.0 - cost, 2)
        else:                                 # held loser
            profit = round(-(cost - exit_rev), 2)

        official_delta = round((c - o) / o * 100, 4) if o > 0 else 0.0
        r["official_open_price"] = round(o, 2)
        r["official_close_price"] = round(c, 2)
        r["official_delta_pct"] = official_delta
        r["official_winning_side"] = winning
        r["won_resolution"] = won
        r["resolution_payout"] = round(shares * 1.0, 2) if won else 0.0
        r["profit"] = profit
        r["return_pct"] = round((profit / cost * 100) if cost > 0 else 0, 2)
        r["profit_if_held"] = round(shares * 1.0 - cost, 2) if won else round(-cost, 2)
        r["btc_final_price"] = round(c, 2)
        r["final_price_source"] = "polymarket_crypto_price"
        if "phantom" in (r.get("resolution_method") or ""):
            r["resolution_method"] = "auto_resolution_apifix"

        flag = "  <== FIXED" if str(old_won) != str(won) else ""
        print(f"{r.get('window_time',''):>5} {side:<4} {r.get('entry_price',''):>5}  "
              f"{str(old_won):>7} -> {str(won):>7}  {str(old_profit):>7} {profit:>7.2f}  "
              f"{o:.2f}->{c:.2f} ({winning}){flag}")
        if str(old_won) != str(won):
            changed += 1

    with open(TRADES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    won_n = sum(1 for r in rows if str(r.get("won_resolution")) == "True")
    net = sum(float(r.get("profit") or 0) for r in rows)
    print(f"\nrewrote {TRADES}: {len(rows)} trades | {won_n}W/{len(rows)-won_n}L | "
          f"net ${net:+.2f} | {changed} outcome(s) corrected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
