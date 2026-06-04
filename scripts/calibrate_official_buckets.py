#!/usr/bin/env python3
"""Calibrate BTC 5m entry buckets using official settlement trade logs.

This intentionally uses resolved trade rows (`official_winning_side` /
`won_resolution` / `resolution_method`) instead of Binance fallback or synthetic
DRY prices. Output is a compact JSON report suitable for parameter tuning.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


def _float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        raw = row.get(key, "")
        return float(raw) if raw not in ("", None) else default
    except Exception:
        return default


def _bool_win(row: dict[str, str]) -> bool | None:
    raw = str(row.get("won_resolution", "")).strip().lower()
    if raw in {"true", "1", "yes", "y", "won", "win"}:
        return True
    if raw in {"false", "0", "no", "n", "lost", "loss"}:
        return False
    official = str(row.get("official_winning_side", "")).strip().upper()
    side = str(row.get("side", "")).strip().upper()
    if official in {"UP", "DOWN"} and side in {"UP", "DOWN"}:
        return official == side
    return None


def _bucket(value: float, cuts: list[float], labels: list[str]) -> str:
    for cut, label in zip(cuts, labels):
        if value < cut:
            return label
    return labels[-1]


def row_buckets(row: dict[str, str]) -> dict[str, str]:
    price = _float(row, "entry_price")
    prob = _float(row, "prob_at_entry")
    delta = abs(_float(row, "entry_delta_pct", _float(row, "btc_delta_at_entry")))
    seconds = _float(row, "entry_seconds_remaining", _float(row, "seconds_remaining_at_entry"))
    edge = prob - price
    return {
        "price_bucket": _bucket(price, [0.55, 0.65, 0.75, 0.85], ["q<0.55", "q0.55-0.65", "q0.65-0.75", "q0.75-0.85", "q>=0.85"]),
        "prob_bucket": _bucket(prob, [0.80, 0.85, 0.90, 0.95], ["p<0.80", "p0.80-0.85", "p0.85-0.90", "p0.90-0.95", "p>=0.95"]),
        "delta_bucket": _bucket(delta, [0.06, 0.10, 0.15, 0.25], ["d<0.06", "d0.06-0.10", "d0.10-0.15", "d0.15-0.25", "d>=0.25"]),
        "time_bucket": "T>180" if seconds > 180 else "T120-180" if seconds > 120 else "T60-120" if seconds > 60 else "T<60",
        "edge_bucket": _bucket(edge, [0.05, 0.10, 0.15, 0.20, 0.30], ["e<0.05", "e0.05-0.10", "e0.10-0.15", "e0.15-0.20", "e0.20-0.30", "e>=0.30"]),
        "side": str(row.get("side", "")).upper(),
    }


def load_trade_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    wins: list[bool] = []
    prices: list[float] = []
    probs: list[float] = []
    profits: list[float] = []
    for row in rows:
        won = _bool_win(row)
        price = _float(row, "entry_price")
        if won is None or price <= 0 or price >= 1:
            continue
        wins.append(won)
        prices.append(price)
        probs.append(_float(row, "prob_at_entry"))
        profits.append(_float(row, "profit"))
    n = len(wins)
    if n == 0:
        return {"n": 0}
    wr = sum(wins) / n
    avg_req = mean(prices)
    avg_payoff = mean((1.0 - p) / p for p in prices)
    return {
        "n": n,
        "wins": int(sum(wins)),
        "losses": int(n - sum(wins)),
        "win_rate": round(wr, 6),
        "avg_model_prob": round(mean(probs), 6) if probs else 0.0,
        "avg_entry_price": round(avg_req, 6),
        "avg_required_wr": round(avg_req, 6),
        "avg_payoff_ratio": round(avg_payoff, 6),
        "ev_vs_required_wr": round(wr - avg_req, 6),
        "avg_profit": round(mean(profits), 6) if profits else 0.0,
        "total_profit": round(sum(profits), 6),
    }


def calibrate_rows(rows: Iterable[dict[str, str]], min_n: int = 5) -> dict[str, Any]:
    official_rows = []
    for row in rows:
        method = str(row.get("resolution_method", "")).lower()
        # Empty method is allowed for unit/backfilled CSVs if official_winning_side exists.
        has_official = str(row.get("official_winning_side", "")).upper() in {"UP", "DOWN"}
        if "binance_fallback" in method and not has_official:
            continue
        if _bool_win(row) is not None and _float(row, "entry_price") > 0:
            official_rows.append(row)

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in official_rows:
        buckets = row_buckets(row)
        for dim, label in buckets.items():
            if label:
                groups[(dim, label)].append(row)

    bucket_summaries = []
    for (dim, label), group_rows in sorted(groups.items()):
        summary = _summarize(group_rows)
        if summary.get("n", 0) >= min_n:
            bucket_summaries.append({"dimension": dim, "bucket": label, **summary})

    return {
        "overall": _summarize(official_rows),
        "buckets": bucket_summaries,
        "min_n": min_n,
        "source": "official_settlement_trade_rows",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate official-settlement trade buckets")
    parser.add_argument("--trades", default="logs/trades.csv", help="Path to trades.csv")
    parser.add_argument("--min-n", type=int, default=5, help="Minimum rows per reported bucket")
    parser.add_argument("--out", default="", help="Optional JSON output path")
    args = parser.parse_args()

    report = calibrate_rows(load_trade_rows(args.trades), min_n=args.min_n)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
