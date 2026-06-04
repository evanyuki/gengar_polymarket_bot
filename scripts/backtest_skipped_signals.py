#!/usr/bin/env python3
"""Backtest every actionable signal in logs/signals.csv against settlement truth.

Question this answers: are the conservative gates (markov size-haircut ->
min-size skip, not-huge-edge skip, source-consensus skip) throwing away real
edge, or correctly refusing losers?

Method: for each window that produced an actionable signal (traded or skipped),
resolve win/loss from Polymarket's crypto-price (Chainlink) close-vs-open for
that window's OWN 5-min slot (symbol=BTC, variant=fiveminute). Then bucket WR
and counterfactual P&L by:
  - decision bucket (traded / min-size skip / not-huge-edge skip / source skip)
  - markov_persistence band (does low persistence actually predict losses?)
  - entry-price band (does paying up to 0.90 still pay?)

Counterfactual P&L assumes the v14 forced 5-share hold-to-resolution:
  win  -> 5 * (1 - entry)
  loss -> -5 * entry
entry = market_price (the ask the bot saw). Real FAK fills land 1-3 ticks
higher, so skipped-bucket P&L here is a best-case; flagged in output.

Run:  python3 scripts/backtest_skipped_signals.py
"""

import csv
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

SIGNALS = os.path.join("logs", "signals.csv")
API = "https://polymarket.com/api/crypto/crypto-price"
PERIOD = 300
SHARES = 5.0

ACTIONABLE = {
    "traded": "traded",
    "skipped_min_size_exceeds_kelly": "min-size skip",
    "skipped_not_huge_edge": "not-huge-edge skip",
    "skipped_source_disagreement": "source skip",
    "skipped_reverse_orderbook": "other skip",
    "skipped_liquidity_gone": "other skip",
}


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_window(window_ts: int):
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


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def persistence_band(p):
    if p >= 0.87:
        return "p>=0.87 (strong)"
    if p >= 0.75:
        return "0.75-0.87 (med)"
    if p >= 0.60:
        return "0.60-0.75 (weak)"
    return "p<0.60 (low)"


def price_band(px):
    if px >= 0.85:
        return "0.85-0.90"
    if px >= 0.80:
        return "0.80-0.85"
    if px >= 0.70:
        return "0.70-0.80"
    return "<0.70"


def main():
    with open(SIGNALS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Dedup to one decision per window: prefer a traded row, else first actionable skip.
    by_window = {}
    for r in rows:
        action = (r.get("action") or "").strip()
        if action not in ACTIONABLE:
            continue
        wts = r.get("window_ts")
        if not wts:
            continue
        prev = by_window.get(wts)
        if prev is None:
            by_window[wts] = r
        elif (prev.get("action") != "traded") and action == "traded":
            by_window[wts] = r

    decisions = sorted(by_window.values(), key=lambda r: int(float(r["window_ts"])))
    print(f"actionable windows: {len(decisions)} "
          f"(deduped from {sum(1 for r in rows if (r.get('action') or '').strip() in ACTIONABLE)} rows)\n")

    results = []
    for r in decisions:
        wts = int(float(r["window_ts"]))
        side = (r.get("side") or "").upper()
        entry = fnum(r.get("market_price"))
        o, c, completed = fetch_window(wts)
        if o is None or not completed:
            results.append({**r, "_won": None})
            continue
        winning = "UP" if c >= o else "DOWN"
        won = (winning == side)
        if won:
            pnl = SHARES * (1.0 - entry)
        else:
            pnl = -SHARES * entry
        results.append({
            "row": r, "won": won, "pnl": round(pnl, 2),
            "side": side, "entry": entry,
            "bucket": ACTIONABLE[(r.get("action") or "").strip()],
            "persist": fnum(r.get("markov_persistence")),
            "fee_edge": fnum(r.get("fee_adjusted_edge")),
            "true_prob": fnum(r.get("true_prob")),
            "delta": fnum(r.get("btc_delta_pct")),
            "wts": wts,
            "open": o, "close": c, "winning": winning,
        })

    resolved = [x for x in results if x.get("won") is not None]
    print(f"resolved {len(resolved)}/{len(decisions)} (unsettled left out)\n")

    def summarize(items, label):
        n = len(items)
        if n == 0:
            print(f"  {label:<34} n=0")
            return
        w = sum(1 for x in items if x["won"])
        pnl = sum(x["pnl"] for x in items)
        print(f"  {label:<34} n={n:>2}  {w:>2}W/{n-w:<2}L  WR={w/n*100:>5.1f}%  netP&L=${pnl:>+7.2f}")

    print("=== by decision bucket ===")
    buckets = defaultdict(list)
    for x in resolved:
        buckets[x["bucket"]].append(x)
    for b in ["traded", "min-size skip", "not-huge-edge skip", "source skip", "other skip"]:
        if b in buckets:
            summarize(buckets[b], b)

    print("\n=== SKIPPED-ONLY by markov_persistence band ===")
    skipped = [x for x in resolved if x["bucket"] != "traded"]
    pb = defaultdict(list)
    for x in skipped:
        pb[persistence_band(x["persist"])].append(x)
    for band in ["p>=0.87 (strong)", "0.75-0.87 (med)", "0.60-0.75 (weak)", "p<0.60 (low)"]:
        if band in pb:
            summarize(pb[band], band)

    print("\n=== ALL RESOLVED by markov_persistence band (haircut justification) ===")
    pb2 = defaultdict(list)
    for x in resolved:
        pb2[persistence_band(x["persist"])].append(x)
    for band in ["p>=0.87 (strong)", "0.75-0.87 (med)", "0.60-0.75 (weak)", "p<0.60 (low)"]:
        if band in pb2:
            summarize(pb2[band], band)

    print("\n=== SKIPPED-ONLY by entry-price band (D2 chase risk) ===")
    eb = defaultdict(list)
    for x in skipped:
        eb[price_band(x["entry"])].append(x)
    for band in ["<0.70", "0.70-0.80", "0.80-0.85", "0.85-0.90"]:
        if band in eb:
            summarize(eb[band], band)

    print("\n=== source-disagreement skips (settlement-gate validation) ===")
    src = [x for x in resolved if x["bucket"] == "source skip"]
    summarize(src, "source skip (all)")

    print("\n=== loss detail (every resolved loss) ===")
    losses = sorted([x for x in resolved if not x["won"]], key=lambda x: x["wts"])
    print(f"  {'window':<20} {'bucket':<20} {'side':<5} {'entry':>5} {'persist':>7} {'feeEdge':>7}  open->close")
    for x in losses:
        wt = datetime.fromtimestamp(x["wts"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {wt:<20} {x['bucket']:<20} {x['side']:<5} {x['entry']:>5.2f} "
              f"{x['persist']:>7.2f} {x['fee_edge']:>7.3f}  {x['open']:.1f}->{x['close']:.1f} ({x['winning']})")

    print("\n=== totals ===")
    summarize(resolved, "ALL actionable")
    summarize(skipped, "ALL skipped (left on table)")


if __name__ == "__main__":
    main()
