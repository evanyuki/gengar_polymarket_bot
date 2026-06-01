#!/usr/bin/env python3
"""Backtest the maker-entry + reverse-sell-50% + same-side pyramid proposal.

Uses the same settlement-truth trick as backtest_early_window_pocket.py:
settle[N] = opening_price[N+1] (Chainlink boundary price), exact, no basis est.

Three mechanisms under test (each isolated, then combined):

1. MAKER ENTRY (0 fee). Adverse-selection test. At the first early-window tick
   whose de-biased Binance move is in the pocket, instead of taking at p_T we
   POST a resting maker BUY at p_T on the candidate side. A resting buy fills
   only when a SELLER crosses down to it, i.e. when the candidate-side book
   price later dips to <= p_T. That dip happens precisely when the candidate
   side becomes LESS likely -> fills are adversely selected. We measure:
     - fill rate (did price ever dip to <= p_T after T, before resolution)
     - WR of FILLED makers   vs   WR of a taker who always fills at p_T
     - among eventual WINNERS, how many never dipped (= maker missed the win)
   Book depth/bid are not logged, so p_T is the logged candidate-side price
   (treated as the marketable ask). This is a directional proxy, not a fill
   simulator -- magnitudes rough, sign of the effect is what matters.

2. REVERSE-SELL-50%. After entry, if the de-biased move reverses sign (BTC
   crosses back through Binance-open against the held side), sell 50% at the
   then-current candidate-side price as a TAKER (fee 0.07*(1-p)), hold the
   other 50% to resolution. Compare net vs full hold-to-resolution. This is a
   partial stop; v12 found stops destroy value here.

3. SAME-SIDE PYRAMID. If a later tick in the same window still satisfies the
   gate (same side, move still in pocket, price <= cap), add a 2nd equal unit.
   Hold all to resolution. WR is identical (one window outcome); only avg entry
   price and variance change. We report avg 1st vs 2nd entry price.

Taker fee model (official): per $1 notional, fee = 0.07 * (1 - p). Maker fee 0.
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


TAKER = 0.07  # crypto feeRate; fee per $1 notional = TAKER*(1-p)


def signal_entry(ticks, b_open, t_lo, t_hi, m_lo, m_hi, cap):
    """First tick satisfying the early-window de-biased pocket. Returns idx,
    side_up, price, move; or None."""
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

    # accumulators
    taker_trades = []          # (win, price)  -- always fills at p_T
    maker_filled = []          # (win, price)  -- resting buy that got hit
    maker_unfilled_win = 0     # winners the maker never filled (missed)
    maker_unfilled_lose = 0
    rev_full = []              # net ret, hold-to-resolution
    rev_half = []              # net ret, reverse-sell-50% variant
    pyr_first_px = []
    pyr_second_px = []
    pyr_windows = 0

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

        # ---- taker baseline (always fills at p_T) ----
        taker_trades.append((win, p_T))

        # ---- maker fill: does candidate-side price dip to <= p_T after T? ----
        eps = 0.005
        filled = False
        for t in ticks[idx + 1:]:
            px = f(t, "up_price") if side_up else f(t, "down_price")
            if px is None:
                continue
            if px <= p_T - eps:
                filled = True
                break
        if filled:
            maker_filled.append((win, p_T))
        else:
            if win:
                maker_unfilled_win += 1
            else:
                maker_unfilled_lose += 1

        # ---- reverse-sell-50% vs full hold (taker entry, taker partial sell) ----
        fee_entry = TAKER * (1.0 - p_T)
        gross_full = (1.0 / p_T - 1.0) if win else -1.0
        net_full = gross_full - fee_entry
        rev_full.append(net_full)

        # find reversal: de-biased move flips sign vs held side
        sell_px = None
        for t in ticks[idx + 1:]:
            b = f(t, "btc_price")
            if b is None:
                continue
            mv = (b - b_open) / b_open * 100.0
            reversed_ = (mv <= 0) if side_up else (mv >= 0)
            if reversed_:
                sell_px = f(t, "up_price") if side_up else f(t, "down_price")
                if sell_px and 0 < sell_px < 1:
                    break
                sell_px = None
        if sell_px is None:
            net_half = net_full  # never reversed -> identical to full hold
        else:
            # 50% notional sold at sell_px (taker), 50% held to resolution
            # entry shares for $1 notional = 1/p_T ; sell half of them at sell_px
            shares = 1.0 / p_T
            sell_shares = shares * 0.5
            proceeds = sell_shares * sell_px
            sell_fee = TAKER * (1.0 - sell_px) * (sell_shares * sell_px)  # fee on sold notional
            held_shares = shares * 0.5
            held_payoff = held_shares * (1.0 if win else 0.0)
            # net = proceeds + held_payoff - 1(stake) - entry_fee - sell_fee
            net_half = proceeds + held_payoff - 1.0 - fee_entry - sell_fee
        rev_half.append(net_half)

        # ---- pyramid: second same-side gate hit later in window ----
        sec = signal_entry(ticks[idx + 1:], b_open, T_LO, T_HI, M_LO, M_HI, CAP)
        if sec is not None and sec[1] == side_up:
            pyr_windows += 1
            pyr_first_px.append(p_T)
            pyr_second_px.append(sec[2])

    def wr(trades):
        n = len(trades)
        return (sum(1 for w, _ in trades if w) / n, n) if n else (0.0, 0)

    def agg(rets):
        n = len(rets)
        if not n:
            return None
        m = sum(rets) / n
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / n) if n > 1 else 0.0
        se = sd / math.sqrt(n) if n else 0.0
        return dict(n=n, mean=m, sd=sd, t=(m / se if se else 0.0), tot=sum(rets))

    print(f"settleable windows = {len(settle)}\n")

    tw, tn = wr(taker_trades)
    mw, mn = wr(maker_filled)
    avg_tp = sum(p for _, p in taker_trades) / tn if tn else 0
    print("== MECHANISM 1: MAKER ENTRY (adverse selection) ==")
    print(f"  signals (taker always-fill): n={tn}  WR={tw*100:.1f}%  avg_price={avg_tp:.3f}")
    print(f"  maker FILLED (price dipped to entry): n={mn}  WR={mw*100:.1f}%")
    print(f"  maker UNFILLED winners (missed wins): {maker_unfilled_win}")
    print(f"  maker UNFILLED losers  (dodged):      {maker_unfilled_lose}")
    miss = maker_unfilled_win
    print(f"  => maker captures {mn}/{tn} signals; "
          f"of {sum(1 for w,_ in taker_trades if w)} taker-winners it MISSES {miss}")
    print()

    print("== MECHANISM 2: REVERSE-SELL-50% vs FULL HOLD ==")
    a, b = agg(rev_full), agg(rev_half)
    if a and b:
        print(f"  FULL hold : n={a['n']} mean={a['mean']*100:+.2f}% t={a['t']:.2f} tot={a['tot']:+.2f}x")
        print(f"  REV-50%   : n={b['n']} mean={b['mean']*100:+.2f}% t={b['t']:.2f} tot={b['tot']:+.2f}x")
        print(f"  delta(rev-full) = {(b['mean']-a['mean'])*100:+.2f}% per trade")
    print()

    print("== MECHANISM 3: SAME-SIDE PYRAMID ==")
    if pyr_windows:
        af = sum(pyr_first_px) / len(pyr_first_px)
        asd = sum(pyr_second_px) / len(pyr_second_px)
        print(f"  windows with 2nd same-side gate hit: {pyr_windows}/{tn}")
        print(f"  avg 1st entry price: {af:.3f}")
        print(f"  avg 2nd entry price: {asd:.3f}  (delta {(asd-af)*100:+.1f}c)")
        print(f"  pyramid adds capital at {'HIGHER' if asd>af else 'lower'} price "
              f"-> {'lower payoff/higher var, same WR' if asd>af else 'better'}")
    else:
        print(f"  no window had a 2nd same-side gate hit (pyramid never triggers)")


if __name__ == "__main__":
    main()
