#!/usr/bin/env python3
"""Backtest the DISCOUNT maker-entry variant: rest a buy d cents BELOW the
entry-time candidate-side price, on the side BTC is favored (de-biased move).

User proposal: "新窗口 + maker(-8c) 挂单等买入大于open price的优势位".
i.e. identify the favored side early in the window, then instead of taking at
p_T, POST a resting maker BUY at p_T - d (d up to 0.08). Fills only when the
candidate-side book later dips to <= p_T - d -- a deeper dip than the at-market
maker, so fills are MORE adversely selected, but each fill is at a cheaper price
(bigger payoff if it still wins). Net effect is an empirical question.

Settlement truth: settle[N] = opening_price[N+1] (Chainlink boundary), exact.
De-biased move = (btc_now - binance_open) / binance_open.

For each discount d we report, over the SAME signal set:
  - fill rate (did price ever dip to <= p_T - d after T, before resolution)
  - WR among FILLED (does the favored side still win after an 8c crater?)
  - winners MISSED (won but never dipped enough -> maker never entered)
  - per-SIGNAL net EV: filled -> net_ret at the cheaper price; unfilled -> 0
    (you placed an order and got nothing; that is the real opportunity cost)
  vs taker baseline: always fill at p_T, pay taker fee.

Taker fee per $1 notional = TAKER*(1-p). Maker fee 0, + crypto rebate 20% of
the taker fee that a taker would have paid at the same price (tiny).
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict


def load_windows(path):
    wins = defaultdict(list)
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            wins[int(row["window_ts"])].append(row)
    for ws in wins:
        wins[ws].sort(key=lambda r: float(r["timestamp"]))
    return wins


def f(row, k):
    try:
        return float(row.get(k, ""))
    except Exception:
        return None


def build_settlement(wins):
    kset = set(wins)
    settle = {}
    for ws in wins:
        nxt = ws + 300
        if nxt in kset:
            op = f(wins[nxt][0], "opening_price")
            if op and op > 0:
                settle[ws] = op
    return settle


def binance_open(ticks):
    for t in ticks:
        b = f(t, "btc_price")
        if b and b > 0:
            return b
    return None


TAKER = 0.07
REBATE_FRAC = 0.20  # crypto maker rebate = 20% of taker fee at same price


def signal_entry(ticks, b_open, t_lo, t_hi, m_lo, m_hi, cap):
    for i, t in enumerate(ticks):
        sr = f(t, "seconds_remaining")
        b = f(t, "btc_price")
        if sr is None or b is None:
            continue
        if not (t_lo <= sr <= t_hi):
            continue
        move = (b - b_open) / b_open * 100.0
        if not (m_lo <= abs(move) <= m_hi):
            continue
        side_up = move > 0
        price = f(t, "up_price") if side_up else f(t, "down_price")
        if price is None or price <= 0 or price >= 1:
            continue
        if price > cap:
            continue
        return i, side_up, price, move
    return None


def main():
    path = "logs/gate_ticks.csv"
    wins = load_windows(path)
    settle = build_settlement(wins)

    T_LO, T_HI, M_LO, M_HI, CAP = 150, 240, 0.03, 0.10, 0.85

    # gather signal set once
    signals = []  # (ws, idx, side_up, p_T, win, ticks)
    for ws, ticks in wins.items():
        if ws not in settle:
            continue
        op_chain = f(ticks[0], "opening_price")
        b_open = binance_open(ticks)
        if not op_chain or not b_open:
            continue
        settle_px = settle[ws]
        if settle_px == op_chain:
            continue
        outcome_up = settle_px > op_chain
        sig = signal_entry(ticks, b_open, T_LO, T_HI, M_LO, M_HI, CAP)
        if sig is None:
            continue
        idx, side_up, p_T, move = sig
        win = (side_up == outcome_up)
        signals.append((ws, idx, side_up, p_T, win, ticks))

    n_sig = len(signals)
    n_win = sum(1 for s in signals if s[4])
    avg_p = sum(s[3] for s in signals) / n_sig if n_sig else 0
    print(f"settleable windows = {len(settle)}")
    print(f"signal set: n={n_sig}  taker WR={n_win/n_sig*100:.1f}%  "
          f"avg_p_T={avg_p:.3f}\n")

    # taker baseline per-signal EV (always fills at p_T)
    taker_ev = 0.0
    for ws, idx, side_up, p_T, win, ticks in signals:
        fee = TAKER * (1.0 - p_T)
        gross = (1.0 / p_T - 1.0) if win else -1.0
        taker_ev += gross - fee
    taker_ev /= n_sig
    print(f"TAKER baseline: per-signal net EV = {taker_ev*100:+.2f}%  "
          f"(always fills, pays {TAKER}*(1-p) fee)\n")

    print(f"{'disc':>5} {'fills':>6} {'fillWR':>7} {'missW':>6} {'missL':>6} "
          f"{'avgFillP':>9} {'EV/sig':>8} {'EV/fill':>8}")
    for d in (0.00, 0.02, 0.05, 0.08):
        fills = 0
        fill_win = 0
        miss_win = 0
        miss_lose = 0
        fill_px_sum = 0.0
        ev_signal = 0.0  # unfilled contributes 0
        ev_fill_sum = 0.0
        for ws, idx, side_up, p_T, win, ticks in signals:
            limit = p_T - d
            if limit <= 0.01:
                # can't rest below 1c; treat as unfillable
                if win:
                    miss_win += 1
                else:
                    miss_lose += 1
                continue
            filled = False
            for t in ticks[idx + 1:]:
                px = f(t, "up_price") if side_up else f(t, "down_price")
                if px is None:
                    continue
                if px <= limit:
                    filled = True
                    break
            if filled:
                fills += 1
                fill_px_sum += limit
                gross = (1.0 / limit - 1.0) if win else -1.0
                rebate = REBATE_FRAC * (TAKER * (1.0 - limit))  # maker rebate
                net = gross + rebate
                ev_signal += net
                ev_fill_sum += net
                if win:
                    fill_win += 1
            else:
                if win:
                    miss_win += 1
                else:
                    miss_lose += 1
        ev_signal /= n_sig
        fwr = (fill_win / fills * 100) if fills else 0.0
        afp = (fill_px_sum / fills) if fills else 0.0
        evf = (ev_fill_sum / fills * 100) if fills else 0.0
        print(f"{d*100:>4.0f}c {fills:>6} {fwr:>6.1f}% {miss_win:>6} "
              f"{miss_lose:>6} {afp:>9.3f} {ev_signal*100:>+7.2f}% "
              f"{evf:>+7.2f}%")

    print()
    print("read: fillWR = WR of windows where the discount order filled.")
    print("      missW = winners that never dipped enough (maker missed the win).")
    print("      missL = losers the maker DODGED by not filling.")
    print("      EV/sig = per-SIGNAL net EV (unfilled = 0, the real cost of not")
    print("               getting filled). compare to TAKER baseline above.")
    print("      EV/fill = net EV conditional on a fill (survivorship-biased).")


if __name__ == "__main__":
    main()
