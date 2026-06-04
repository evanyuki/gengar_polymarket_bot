#!/usr/bin/env python3
"""Backtest raising the Chainlink-confirmation gate (min_chainlink_delta_pct).

Question (data-driven, no look-ahead): does requiring the SETTLEMENT oracle
(Chainlink) to have moved further from window-open before we trade improve
win-rate / EV? i.e. is the marginal trade we drop by raising the threshold a
-EV trade (basis noise: Binance spiked, Chainlink ignored) or a +EV trade
(real lag: Chainlink will follow)?

Per-tick Chainlink current price is NOT logged directly, but it is EXACTLY
reconstructable from two logged fields (see source_consensus.py:348-350):

    source_gap = (rtds_binance - chainlink) / chainlink * 1e4      # source_gap_bps
    chainlink_now = btc_price / (1 + source_gap_bps / 1e4)
    chainlink_delta_pct = (chainlink_now - opening_price) / opening_price * 100

Validation: row btc=73935.09 gap=14.4402 open=73828.84 ->
  chainlink_now = 73828.5, delta = -0.0005% -> chainlink_side DOWN while
  Binance side UP -> logged reason 'chainlink_direction_disagrees'. Matches.

Settlement truth: settle[N] = opening_price[N+1] (consecutive 300s windows
share the Chainlink boundary print). outcome_up = settle > open. EXACT.

We replay the LIVE gate per tick using the LOGGED model outputs
(true_prob, fee_adjusted_edge, candidate_side) so the probability/edge model is
identical to production; we ONLY override the Chainlink-confirm threshold X and
re-derive the Chainlink side/distance check from the reconstructed price.

For each X we report n / WR / avg entry price / taker net EV. We ALSO report the
WR of the marginal band [X_prev, X) -- the trades a higher threshold throws
away. If that band's WR is BELOW break-even, raising the gate is +EV.
"""

from __future__ import annotations

import csv
from collections import defaultdict

PATH = "logs/gate_ticks.csv"

# Live entry thresholds (current .env) -- held fixed across the sweep.
MIN_PROB = 0.86
HUGE_EDGE = 0.08
ENTRY_SR_LO, ENTRY_SR_HI = 10.0, 240.0   # ENTRY_WINDOW_END..START seconds_remaining
STALE_SKIP_SEC = 30.0
MAX_SOURCE_GAP_BPS = 25.0
PRICE_CAP = 0.95
TAKER = 0.07  # fee per $1 notional = TAKER*(1-p)


def fnum(row, k):
    try:
        return float(row.get(k, ""))
    except Exception:
        return None


def load_windows(path):
    wins = defaultdict(list)
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ws = int(row["window_ts"])
            except Exception:
                continue
            wins[ws].append(row)
    for ws in wins:
        wins[ws].sort(key=lambda r: float(r["timestamp"]))
    return wins


def build_settlement(wins):
    kset = set(wins)
    settle = {}
    for ws in wins:
        nxt = ws + 300
        if nxt in kset:
            op = fnum(wins[nxt][0], "opening_price")
            if op and op > 0:
                settle[ws] = op
    return settle


def chainlink_delta(row, opening_price):
    """Reconstruct per-tick Chainlink delta-from-open (%) or None."""
    b = fnum(row, "btc_price")
    gap = fnum(row, "source_gap_bps")
    if b is None or gap is None or opening_price <= 0:
        return None
    cl_now = b / (1.0 + gap / 1e4)
    return (cl_now - opening_price) / opening_price * 100.0


def first_entry(ticks, opening_price, x_thresh):
    """First tick passing the full live gate with Chainlink threshold = x_thresh.
    Returns (win_unknown placeholder, p, cl_delta, side) parts or None."""
    for t in ticks:
        sr = fnum(t, "seconds_remaining")
        if sr is None or not (ENTRY_SR_LO <= sr <= ENTRY_SR_HI):
            continue
        side = (t.get("candidate_side") or "").upper()
        if side not in ("UP", "DOWN"):
            continue
        prob = fnum(t, "true_prob")
        edge = fnum(t, "fee_adjusted_edge")
        p = fnum(t, "candidate_market_price")
        if prob is None or edge is None or p is None:
            continue
        if prob < MIN_PROB or edge < HUGE_EDGE or not (0.0 < p <= PRICE_CAP):
            continue
        # source guards (same for all X)
        age = fnum(t, "chainlink_age_seconds")
        gap = fnum(t, "source_gap_bps")
        if age is None or age > STALE_SKIP_SEC:
            continue
        if gap is None or abs(gap) > MAX_SOURCE_GAP_BPS:
            continue
        cld = chainlink_delta(t, opening_price)
        if cld is None:
            continue
        cl_side = "UP" if cld >= 0 else "DOWN"
        if cl_side != side:           # Chainlink direction must confirm
            continue
        if abs(cld) < x_thresh:        # the varying gate
            continue
        return p, cld, side
    return None


def taker_ev(win, p):
    fee = TAKER * (1.0 - p)
    gross = (1.0 / p - 1.0) if win else -1.0
    return gross - fee


def main():
    wins = load_windows(PATH)
    settle = build_settlement(wins)
    print(f"windows total={len(wins)}  settleable={len(settle)}\n")

    # Build the per-window first-entry at the LOWEST threshold (0.02 baseline),
    # capturing the realized cl_delta so we can re-bucket by any higher X without
    # re-scanning ticks (raising X only ever filters this entry set OUT, never in,
    # because a higher |cl_delta| requirement on the SAME first qualifying tick is
    # monotone -- but a higher X could pick a LATER tick. To be exact we re-scan
    # per X below; this baseline list is only for the marginal-band WR view).
    base = []  # (ws, win, p, cl_delta)
    for ws, ticks in wins.items():
        if ws not in settle:
            continue
        op = fnum(ticks[0], "opening_price")
        if not op or op <= 0:
            continue
        outcome_up = settle[ws] > op
        if settle[ws] == op:
            continue
        ent = first_entry(ticks, op, 0.02)
        if ent is None:
            continue
        p, cld, side = ent
        win = (side == "UP") == outcome_up
        base.append((ws, win, p, abs(cld)))

    n = len(base)
    if not n:
        print("no baseline signals -- nothing to sweep")
        return
    wr = sum(1 for _, w, _, _ in base if w) / n
    avgp = sum(p for _, _, p, _ in base) / n
    ev = sum(taker_ev(w, p) for _, w, p, _ in base) / n
    print(f"BASELINE (X=0.02): n={n}  WR={wr*100:.1f}%  avg_p={avgp:.3f}  "
          f"taker EV/trade={ev*100:+.2f}%\n")

    # WR vs |chainlink_delta| bucket -- the core diagnostic.
    print("WR by |chainlink_delta| bucket (does stronger CL confirm -> higher WR?)")
    edges = [0.02, 0.03, 0.04, 0.05, 0.07, 0.10, 0.15, 0.25, 1e9]
    labels = ["0.02-0.03", "0.03-0.04", "0.04-0.05", "0.05-0.07",
              "0.07-0.10", "0.10-0.15", "0.15-0.25", "0.25+"]
    for i, lab in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        bucket = [(w, p) for _, w, p, d in base if lo <= d < hi]
        bn = len(bucket)
        if not bn:
            print(f"  {lab:>10}: n=0")
            continue
        bwr = sum(1 for w, _ in bucket if w) / bn
        bev = sum(taker_ev(w, p) for w, p in bucket) / bn
        print(f"  {lab:>10}: n={bn:3d}  WR={bwr*100:5.1f}%  EV/trade={bev*100:+6.2f}%")
    print()

    # Full re-scan per threshold (exact: re-pick first qualifying tick each X).
    print(f"{'X(%)':>6} {'n':>4} {'WR':>7} {'avg_p':>7} {'EV/trade':>9} "
          f"{'tot_EV':>8} {'dropVsPrev':>11} {'dropWR':>7}")
    prev_set = None
    prev_x = None
    for x in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15):
        trades = []  # (ws, win, p)
        for ws, ticks in wins.items():
            if ws not in settle:
                continue
            op = fnum(ticks[0], "opening_price")
            if not op or op <= 0 or settle[ws] == op:
                continue
            outcome_up = settle[ws] > op
            ent = first_entry(ticks, op, x)
            if ent is None:
                continue
            p, cld, side = ent
            win = (side == "UP") == outcome_up
            trades.append((ws, win, p))
        m = len(trades)
        if not m:
            print(f"{x*100:>5.1f}% {0:>4}  -- no trades --")
            prev_set = set(); prev_x = x
            continue
        twr = sum(1 for _, w, _ in trades if w) / m
        tap = sum(p for _, _, p in trades) / m
        tev = sum(taker_ev(w, p) for _, w, p in trades) / m
        tot = sum(taker_ev(w, p) for _, w, p in trades)
        cur_set = {ws for ws, _, _ in trades}
        if prev_set is not None:
            dropped = prev_set - cur_set
            dn = len(dropped)
            dwins = [(w, p) for ws, w, p in prev_trades if ws in dropped]
            dwr = (sum(1 for w, _ in dwins if w) / dn * 100) if dn else 0.0
            dstr = f"{dn} ({prev_x*100:.0f}->{x*100:.0f})"
            dwrs = f"{dwr:.0f}%"
        else:
            dstr = "-"
            dwrs = "-"
        print(f"{x*100:>5.1f}% {m:>4} {twr*100:>6.1f}% {tap:>7.3f} "
              f"{tev*100:>+8.2f}% {tot:>+7.2f}x {dstr:>11} {dwrs:>7}")
        prev_set = cur_set
        prev_trades = trades
        prev_x = x
    print()
    print("read: dropVsPrev = trades dropped when raising from prev X to this X.")
    print("      dropWR     = win-rate of those dropped trades. If dropWR is")
    print("                   BELOW ~avg_p break-even, raising the gate is +EV.")
    print("      EV uses taker fee 0.07*(1-p); settlement = Chainlink boundary.")


if __name__ == "__main__":
    main()
