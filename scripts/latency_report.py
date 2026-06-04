#!/usr/bin/env python3
"""Summarize logs/order_latency.csv — the submit->ack latency split.

Answers the only question that matters for the no-fills: where do the
milliseconds go (sign vs post=network+match), and is post latency the reason
the book walks away before our FAK lands.

Usage:
    python3 scripts/latency_report.py [path]
    watch -n 30 'python3 scripts/latency_report.py'   # live during a run
"""

import csv
import os
import sys
from statistics import median


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.getenv("LOG_DIR", "logs"), "order_latency.csv"
    )
    if not os.path.exists(path):
        print(f"no latency log yet: {path}")
        return 0

    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "outcome": r["outcome"],
                    "bal": float(r["bal_ms"]),
                    "sign": float(r["sign_ms"]),
                    "post": float(r["post_ms"]),
                    "ack": float(r["submit_ack_ms"]),
                })
            except (KeyError, ValueError):
                continue

    if not rows:
        print(f"latency log empty: {path}")
        return 0

    print(f"order_latency.csv — {len(rows)} orders @ {path}\n")

    # Overall submit->ack split (the controllable hot path).
    def col(key, subset=rows):
        return [x[key] for x in subset]

    print("=== ALL orders: phase split (ms) ===")
    print(f"{'phase':<18}{'median':>9}{'p90':>9}{'max':>9}")
    for key, label in [
        ("bal", "bal (pre-sign GET)"),
        ("sign", "sign (local)"),
        ("post", "post (net+match)"),
        ("ack", "submit->ack"),
    ]:
        v = col(key)
        print(f"{label:<18}{median(v):>9.0f}{_pct(v,90):>9.0f}{max(v):>9.0f}")

    # Filled vs no-fill: is post latency higher when we miss?
    filled = [x for x in rows if x["outcome"] == "filled"]
    nofill = [x for x in rows if "no_fill" in x["outcome"]]
    print("\n=== filled vs no_fill: median post(net+match) ms ===")
    print(f"filled   n={len(filled):<4} post={median(col('post',filled)) if filled else 0:.0f}  "
          f"ack={median(col('ack',filled)) if filled else 0:.0f}")
    print(f"no_fill  n={len(nofill):<4} post={median(col('post',nofill)) if nofill else 0:.0f}  "
          f"ack={median(col('ack',nofill)) if nofill else 0:.0f}")

    # Outcome tally.
    print("\n=== outcome tally ===")
    tally = {}
    for x in rows:
        tally[x["outcome"]] = tally.get(x["outcome"], 0) + 1
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24}{n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
