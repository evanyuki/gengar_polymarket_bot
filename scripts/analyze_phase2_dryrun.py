#!/usr/bin/env python3
"""Analyze newly appended rows for a Phase-2 PolyBot DRY_RUN session.

Usage:
  python3 scripts/analyze_phase2_dryrun.py logs/phase2/<baseline>.json
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def new_rows(log_dir: Path, baseline: dict, name: str) -> list[dict[str, str]]:
    rows = read_rows(log_dir / name)
    start = int((baseline.get("files", {}).get(name) or {}).get("rows") or 0)
    return rows[start:]


def f(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, "")
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


def pct(n: int, d: int) -> str:
    return "0.0%" if d <= 0 else f"{n / d * 100:.1f}%"


def describe(vals: list[float]) -> str:
    vals = [v for v in vals if math.isfinite(v)]
    if not vals:
        return "n=0"
    return f"n={len(vals)} avg={mean(vals):.4f} med={median(vals):.4f} min={min(vals):.4f} max={max(vals):.4f}"


def counter_lines(c: Counter, limit: int = 20) -> list[str]:
    if not c:
        return ["  (none)"]
    return [f"  {k or '(blank)'}: {v}" for k, v in c.most_common(limit)]


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: analyze_phase2_dryrun.py <baseline.json>")
        return 2
    baseline_path = Path(sys.argv[1])
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    root = Path(baseline.get("cwd") or ".")
    log_dir = root / "logs"
    run_id = baseline.get("run_id", baseline_path.stem.replace("_baseline", ""))

    gate = new_rows(log_dir, baseline, "gate_ticks.csv")
    trades = new_rows(log_dir, baseline, "trades.csv")
    signals = new_rows(log_dir, baseline, "signals.csv")
    sessions = new_rows(log_dir, baseline, "dry_run_sessions.csv")
    lat = new_rows(log_dir, baseline, "order_latency.csv")

    out: list[str] = []
    out.append(f"Phase 2 DRY_RUN analysis — {run_id}")
    out.append(f"Baseline: {baseline_path}")
    out.append("")
    out.append("新增行数:")
    out.append(f"  gate_ticks: {len(gate)}")
    out.append(f"  signals: {len(signals)}")
    out.append(f"  trades: {len(trades)}")
    out.append(f"  dry_run_sessions: {len(sessions)}")
    out.append(f"  order_latency: {len(lat)}")
    out.append("")

    if gate:
        windows = {r.get("window_ts") for r in gate if r.get("window_ts")}
        ready = [r for r in gate if str(r.get("signal_ready", "")).lower() in {"true", "1", "yes"}]
        out.append("Gate-level 统计:")
        out.append(f"  ticks={len(gate)} windows={len(windows)} signal_ready_ticks={len(ready)} ({pct(len(ready), len(gate))})")
        out.append("  gate_reason counts:")
        out.extend(counter_lines(Counter(r.get("gate_reason", "") for r in gate), 15))
        out.append("  source_reason counts:")
        out.extend(counter_lines(Counter(r.get("source_reason", "") for r in gate), 15))
        out.append("  candidate_side counts:")
        out.extend(counter_lines(Counter(r.get("candidate_side", "") for r in gate), 5))
        out.append("  signal_source counts:")
        out.extend(counter_lines(Counter(r.get("signal_source", "") for r in gate), 10))

        cl_vals = [f(r, "chainlink_delta_pct") for r in gate if r.get("chainlink_delta_pct", "") != ""]
        rb_vals = [f(r, "rtds_binance_delta_pct") for r in gate if r.get("rtds_binance_delta_pct", "") != ""]
        sig_vals = [f(r, "signal_delta_pct") for r in gate if r.get("signal_delta_pct", "") != ""]
        out.append("  delta distributions:")
        out.append(f"    chainlink_delta_pct: {describe(cl_vals)}")
        out.append(f"    rtds_binance_delta_pct: {describe(rb_vals)}")
        out.append(f"    signal_delta_pct: {describe(sig_vals)}")

        def side_from_delta(x: float) -> str:
            return "UP" if x >= 0 else "DOWN"
        comparable = [r for r in gate if r.get("chainlink_delta_pct", "") not in ("", "0", "0.0") and r.get("rtds_binance_delta_pct", "") not in ("", "0", "0.0")]
        disagree = [r for r in comparable if side_from_delta(f(r, "chainlink_delta_pct")) != side_from_delta(f(r, "rtds_binance_delta_pct"))]
        out.append("  source direction disagreement:")
        out.append(f"    comparable_ticks={len(comparable)} disagree_ticks={len(disagree)} ({pct(len(disagree), len(comparable))})")
        if disagree:
            out.append("    disagreement gate_reason counts:")
            out.extend(counter_lines(Counter(r.get("gate_reason", "") for r in disagree), 10))

        near_zero = [r for r in gate if abs(f(r, "chainlink_delta_pct")) < 0.02]
        out.append(f"  near-zero Chainlink |delta|<0.02% ticks={len(near_zero)} ({pct(len(near_zero), len(gate))})")
        out.append("")
    else:
        out.append("Gate-level 统计: 无新增 gate_ticks。根因优先检查：bot 未启动、price feed 未连上、或日志 schema 被重置后未写入。")
        out.append("")

    if trades:
        won = [r for r in trades if str(r.get("won_resolution", "")).lower() in {"true", "1", "yes"}]
        pnl = [f(r, "profit") for r in trades]
        out.append("Trade-level 统计:")
        out.append(f"  trades={len(trades)} wins={len(won)} losses={len(trades)-len(won)} win_rate={pct(len(won), len(trades))}")
        out.append(f"  profit_total={sum(pnl):+.4f} avg_profit={mean(pnl):+.4f}" if pnl else "  profit: n=0")
        out.append("  side counts:")
        out.extend(counter_lines(Counter(r.get("side", "") for r in trades), 5))
        out.append("  entry_signal_source counts:")
        out.extend(counter_lines(Counter(r.get("entry_signal_source", "") for r in trades), 10))
        out.append(f"  entry_chainlink_delta_pct: {describe([f(r, 'entry_chainlink_delta_pct') for r in trades])}")
        out.append(f"  entry_rtds_binance_delta_pct: {describe([f(r, 'entry_rtds_binance_delta_pct') for r in trades])}")
        out.append("")
    else:
        out.append("Trade-level 统计: 新增 trades=0。不要美化：如果 signal_ready 也很少，这是策略门控过严/边缘不足；如果 signal_ready 很多但 trades=0，才是执行热路径问题。")
        out.append("")

    if signals:
        out.append("Signal log action/skip_reason:")
        out.append("  action counts:")
        out.extend(counter_lines(Counter(r.get("action", "") for r in signals), 15))
        out.append("  skip_reason counts:")
        out.extend(counter_lines(Counter(r.get("skip_reason", "") for r in signals), 15))
        out.append("")

    if lat:
        out.append("Order latency rows:")
        out.append(f"  rows={len(lat)}")
        for key in ["latency_ms", "bal_ms", "sign_ms", "post_ms", "ack_ms"]:
            vals = [f(r, key) for r in lat if r.get(key, "") != ""]
            if vals:
                out.append(f"  {key}: {describe(vals)}")
        out.append("")

    out.append("Root-cause framing:")
    if gate:
        ready_n = sum(1 for r in gate if str(r.get("signal_ready", "")).lower() in {"true", "1", "yes"})
        if not trades and ready_n == 0:
            out.append("  本轮主要不是下单慢，而是策略没有产生可交易信号。优先看 gate_reason/source_reason，不要调 executor。")
        elif not trades and ready_n > 0:
            out.append("  有 signal_ready 但无 trade，重点查 warm/source/edge/live ask/min-size/orderbook 热路径。")
        else:
            out.append("  已有交易样本；下一步按 entry_chainlink_delta_pct、entry price、side、source disagreement 分桶看 EV。")
    else:
        out.append("  无 gate_ticks，先修运行/日志，不要讨论策略优劣。")

    report = "\n".join(out)
    report_path = root / "logs" / "phase2" / f"{run_id}_analysis.txt"
    report_path.write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\nReport written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
