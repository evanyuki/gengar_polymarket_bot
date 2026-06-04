#!/usr/bin/env python3
"""Summarize newly appended gate_ticks source-gap fields.

Usage:
    python3 scripts/analyze_gate_source_gaps.py --start-line "$GATE_START"
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from pathlib import Path


def as_float(value: str):
    try:
        if value is None or value == "":
            return None
        x = float(value)
        if math.isnan(x):
            return None
        return x
    except Exception:
        return None


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = int(round((len(xs) - 1) * q))
    return xs[max(0, min(idx, len(xs) - 1))]


def summarize(name: str, values: list[float]) -> None:
    if not values:
        print(f"{name}: no data")
        return
    abs_values = [abs(v) for v in values]
    print(
        f"{name}: n={len(values)} "
        f"min={min(values):+.2f} mean={sum(values)/len(values):+.2f} "
        f"p50={pct(values,0.50):+.2f} p90={pct(values,0.90):+.2f} "
        f"p95={pct(values,0.95):+.2f} max={max(values):+.2f} bps | "
        f"abs_p50={pct(abs_values,0.50):.2f} abs_p90={pct(abs_values,0.90):.2f} "
        f"abs_p95={pct(abs_values,0.95):.2f} abs_max={max(abs_values):.2f} bps"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/gate_ticks.csv")
    ap.add_argument("--start-line", type=int, default=1, help="1-based line number from wc -l before run")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"missing {path}")
        return 1

    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            if idx <= args.start_line:
                continue
            rows.append(row)

    print(f"gate_ticks rows analyzed: {len(rows)} from {path} after line {args.start_line}")
    if not rows:
        return 0

    source_gaps = [x for x in (as_float(r.get("source_gap_bps", "")) for r in rows) if x is not None]
    direct_gaps = [x for x in (as_float(r.get("direct_vs_rtds_binance_gap_bps", "")) for r in rows) if x is not None]
    summarize("source_gap_bps (RTDS Binance - Chainlink)", source_gaps)
    summarize("direct_vs_rtds_binance_gap_bps", direct_gaps)

    print("\nsource_reason counts:")
    for reason, n in Counter(r.get("source_reason", "") or "<blank>" for r in rows).most_common(12):
        print(f"  {reason}: {n}")

    print("\ngate_reason counts:")
    for reason, n in Counter(r.get("gate_reason", "") or "<blank>" for r in rows).most_common(12):
        print(f"  {reason}: {n}")

    over_12 = sum(1 for x in source_gaps if abs(x) > 12.0)
    over_25 = sum(1 for x in source_gaps if abs(x) > 25.0)
    over_30 = sum(1 for x in source_gaps if abs(x) > 30.0)
    if source_gaps:
        print(
            f"\nabs(source_gap) threshold hit-rate: "
            f">12bps={over_12}/{len(source_gaps)} ({over_12/len(source_gaps)*100:.1f}%), "
            f">25bps={over_25}/{len(source_gaps)} ({over_25/len(source_gaps)*100:.1f}%), "
            f">30bps={over_30}/{len(source_gaps)} ({over_30/len(source_gaps)*100:.1f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
