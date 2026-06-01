#!/usr/bin/env python3
"""Backtest the early-window de-biased Binance-move pocket.

Settlement truth trick: Polymarket 5m windows are back-to-back. Chainlink price
at the N/N+1 boundary IS both the settlement of window N and the open of window
N+1. So settle[N] = opening_price[N+1] for consecutive windows (window_ts diff
== 300). This is exact (same Chainlink source, same timestamp) -- no basis
estimation needed.

De-biased Binance move = (btc_price_now - binance_open) / binance_open, where
binance_open ~= first logged btc_price of the window (within a few seconds of
window_ts). This removes the constant ~+14.5bps Binance/Chainlink basis that
poisoned the live btc_delta_pct (which anchored to the Chainlink open).

Strategy under test: one entry per window, FIRST tick that satisfies
  - seconds_remaining in [t_lo, t_hi]
  - |de-biased move| in [m_lo, m_hi]  (percent, e.g. 0.03..0.10)
  - candidate-side book price <= price_cap
Buy candidate side (UP if move>0 else DOWN). Hold to resolution.
PnL per $1 stake: win -> (1-price)/price * (1 - fee) ; lose -> -1.
We report gross and net (fee in bps of notional, charged on entry).
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict


def load_windows(path):
    wins = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            wins[int(row["window_ts"])].append(row)
    for ws in wins:
        wins[ws].sort(key=lambda r: float(r["timestamp"]))
    return wins


def f(row, k):
    v = row.get(k, "")
    try:
        return float(v)
    except Exception:
        return None


def build_settlement(wins):
    """settle[ws] = opening_price of next consecutive window (= Chainlink close)."""
    kset = set(wins)
    settle = {}
    for ws in wins:
        nxt = ws + 300
        if nxt in kset:
            op_next = f(wins[nxt][0], "opening_price")
            if op_next and op_next > 0:
                settle[ws] = op_next
    return settle


def binance_open(ticks):
    for t in ticks:
        b = f(t, "btc_price")
        if b and b > 0:
            return b
    return None


def simulate(wins, settle, t_lo, t_hi, m_lo, m_hi, price_cap, fee_bps):
    """Return list of trades: dict(side, price, win, ret_net, move_pct)."""
    trades = []
    skipped_tie = 0
    for ws, ticks in wins.items():
        if ws not in settle:
            continue
        op_chain = f(ticks[0], "opening_price")
        b_open = binance_open(ticks)
        if not op_chain or not b_open:
            continue
        settle_px = settle[ws]
        if settle_px == op_chain:
            skipped_tie += 1
            continue
        outcome_up = settle_px > op_chain  # Chainlink-settled truth

        entry = None
        for t in ticks:
            sr = f(t, "seconds_remaining")
            b = f(t, "btc_price")
            if sr is None or b is None:
                continue
            if not (t_lo <= sr <= t_hi):
                continue
            move = (b - b_open) / b_open * 100.0  # de-biased, percent
            if not (m_lo <= abs(move) <= m_hi):
                continue
            side_up = move > 0
            price = f(t, "up_price") if side_up else f(t, "down_price")
            if price is None or price <= 0 or price >= 1:
                continue
            if price > price_cap:
                continue
            entry = (side_up, price, move)
            break
        if entry is None:
            continue
        side_up, price, move = entry
        win = (side_up == outcome_up)
        # stake $1 notional -> shares = 1/price ; payoff if win = shares*1 = 1/price
        gross_ret = (1.0 / price - 1.0) if win else -1.0
        fee = (fee_bps / 10000.0)  # fraction of $1 notional charged on entry
        net_ret = gross_ret - fee
        trades.append(dict(side_up=side_up, price=price, win=win,
                           gross=gross_ret, net=net_ret, move=move, ws=ws))
    return trades, skipped_tie


def stats(trades, key):
    if not trades:
        return None
    rets = [t[key] for t in trades]
    n = len(rets)
    wins_ = sum(1 for t in trades if t["win"])
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n) if n else 0.0
    # geometric (Kelly-ish) growth at fractional stake f=0.25 of bankroll per trade
    # approximate compounding if each trade risks 25% notional:
    return dict(n=n, wr=wins_ / n, mean=mean, sd=sd, se=se,
                tstat=(mean / se if se > 0 else 0.0),
                total=sum(rets))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/gate_ticks.csv")
    ap.add_argument("--fee-bps", type=float, default=0.0)
    args = ap.parse_args()

    wins = load_windows(args.file)
    settle = build_settlement(wins)
    print(f"windows={len(wins)} settleable={len(settle)}")

    # base rate
    up_wins = sum(1 for ws in settle
                  if settle[ws] > f(wins[ws][0], "opening_price"))
    print(f"base rate UP-wins among settleable: {up_wins}/{len(settle)} "
          f"= {up_wins/len(settle)*100:.1f}%")
    print()

    grids = [
        # (label, t_lo, t_hi, m_lo, m_hi, price_cap)
        ("EARLY pocket T150-240 |move|0.03-0.10 cap0.85", 150, 240, 0.03, 0.10, 0.85),
        ("EARLY pocket T120-240 |move|0.04-0.12 cap0.88", 120, 240, 0.04, 0.12, 0.88),
        ("EARLY wide   T120-240 |move|0.03-0.20 cap0.90", 120, 240, 0.03, 0.20, 0.90),
        ("ANY time     T010-300 |move|0.06-1.00 cap0.97", 10, 300, 0.06, 1.00, 0.97),
        ("LATE         T010-090 |move|0.06-1.00 cap0.97", 10, 90, 0.06, 1.00, 0.97),
        ("BIG move     T060-240 |move|0.10-1.00 cap0.92", 60, 240, 0.10, 1.00, 0.92),
    ]
    for label, tl, th, ml, mh, cap in grids:
        trades, ties = simulate(wins, settle, tl, th, ml, mh, cap, args.fee_bps)
        g = stats(trades, "gross")
        net = stats(trades, "net")
        if not g:
            print(f"{label}: 0 trades")
            continue
        print(f"{label}")
        print(f"  trades={g['n']}  WR={g['wr']*100:.1f}%  ties_skipped={ties}")
        print(f"  GROSS mean_ret={g['mean']*100:+.2f}%  total={g['total']:+.2f}x  "
              f"sd={g['sd']*100:.1f}%  t={g['tstat']:.2f}")
        print(f"  NET(fee={args.fee_bps}bps) mean_ret={net['mean']*100:+.2f}%  "
              f"total={net['total']:+.2f}x  t={net['tstat']:.2f}")
        avgp = sum(t["price"] for t in trades) / len(trades)
        print(f"  avg_entry_price={avgp:.3f}")
        print()


if __name__ == "__main__":
    main()
