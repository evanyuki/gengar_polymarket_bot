#!/usr/bin/env python3
"""Backtest the ENTRY-VELOCITY same-direction gate on the bot's REAL entries.

Thesis (oracle-lag): the edge exists only while BTC is still MOVING in the bet
direction. If BTC has already peaked and is rolling over, taking the stale-lag
bet = betting into mean-reversion. The proposed gate keeps an entry only when
the recent BTC velocity (momentum_15s / momentum_30s, already logged per tick)
points the SAME way as the bet:

    UP   bet -> keep iff momentum >  +eps   (BTC still rising)
    DOWN bet -> keep iff momentum <  -eps   (BTC still falling)

We do NOT re-derive a signal set. We take the system's ACTUAL decisions:
every gate_ticks row with signal_ready==1 (one per window = what the bot fired
or would have fired). For each we ask: does the velocity gate KILL it, and was
that entry a winner or a loser?

Net EV of the gate = $ saved by killing losers  -  $ forgone by killing winners.
A gate is only worth shipping if it kills losers worth more than the winners it
also kills (the maker-discount adverse-selection trap, but inverted).

Settlement truth (exact, Chainlink boundary): settle[N] = opening_price[N+1].
outcome_up = settle > open_N. Win = (bet side == outcome side).
For the 8 windows that actually traded LIVE, we override sim PnL with the real
profit column from trades.csv (real fills, real claims, real basis at the edge).

Honesty flags this script prints:
  - how many of the signal_ready entries have USABLE momentum (buffer not cold);
    early-run rows log momentum_15s==momentum_30s==0.0 because the markov sample
    buffer was empty -> the gate is INOPERATIVE there, it cannot filter what it
    never measured.
  - how many losers exist in the whole signal set (EV is dominated by a handful).
  - a threshold sweep, so the result is not one cherry-picked eps.
"""

from __future__ import annotations

import csv
from collections import defaultdict

GATE_TICKS = "logs/gate_ticks.csv"
TRADES = "logs/trades.csv"

TAKER = 0.07          # taker fee fraction (per $1 notional fee = TAKER*(1-p))
STAKE = 5.0           # $ per entry for non-live windows (matches live MAX_BET)
COLD_EPS = 0.003      # |momentum%| below this = treat as cold/unusable buffer


def f(row, k):
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
            op = f(wins[nxt][0], "opening_price")
            if op and op > 0:
                settle[ws] = op
    return settle


def load_live_pnl(path):
    """window_ts -> real profit ($) from live trades (ground truth)."""
    out = {}
    try:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("mode") or "").upper() != "LIVE":
                    continue
                try:
                    out[int(row["window_ts"])] = float(row["profit"])
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def sim_pnl(side_up, entry_px, win):
    """Per-entry $ PnL for a $STAKE taker buy held to resolution."""
    if entry_px <= 0 or entry_px >= 1:
        return 0.0
    shares = STAKE / entry_px
    fee = TAKER * (1.0 - entry_px) * STAKE  # taker fee on notional
    if win:
        return shares * 1.0 - STAKE - fee
    return -STAKE - fee


def collect_entries(wins, settle, live_pnl):
    """One record per signal_ready window: the system's real entry decision."""
    entries = []
    for ws, ticks in wins.items():
        if ws not in settle:
            continue
        op = f(ticks[0], "opening_price")
        if not op:
            continue
        settle_px = settle[ws]
        if settle_px == op:
            continue  # exact tie, undecidable
        outcome_up = settle_px > op
        # first signal_ready tick = the moment the bot commits
        row = next((t for t in ticks if (t.get("signal_ready") or "") in ("1", "True", "true")), None)
        if row is None:
            continue
        side = (row.get("candidate_side") or "").upper()
        if side not in ("UP", "DOWN"):
            continue
        side_up = side == "UP"
        entry_px = f(row, "candidate_market_price")
        m15 = f(row, "momentum_15s_pct")
        m30 = f(row, "momentum_30s_pct")
        if entry_px is None or m15 is None or m30 is None:
            continue
        win = (side_up == outcome_up)
        if ws in live_pnl:
            pnl = live_pnl[ws]          # real money
            src = "LIVE"
        else:
            pnl = sim_pnl(side_up, entry_px, win)
            src = "sim"
        entries.append(dict(ws=ws, side_up=side_up, entry_px=entry_px,
                            m15=m15, m30=m30, win=win, pnl=pnl, src=src))
    entries.sort(key=lambda e: e["ws"])
    return entries


def keep(entry, mom_key, eps):
    m = entry[mom_key]
    if entry["side_up"]:
        return m > eps
    return m < -eps


def report_filter(entries, mom_key, eps):
    kept, killed = [], []
    for e in entries:
        (kept if keep(e, mom_key, eps) else killed).append(e)
    k_los = [e for e in killed if not e["win"]]
    k_win = [e for e in killed if e["win"]]
    saved = -sum(e["pnl"] for e in k_los)      # losers killed -> + dollars
    forgone = sum(e["pnl"] for e in k_win)     # winners killed -> - dollars
    net = saved - forgone
    base_pnl = sum(e["pnl"] for e in entries)
    kept_pnl = sum(e["pnl"] for e in kept)
    base_wr = sum(e["win"] for e in entries) / len(entries) * 100 if entries else 0
    kept_wr = sum(e["win"] for e in kept) / len(kept) * 100 if kept else 0
    return dict(eps=eps, mom_key=mom_key, n_kept=len(kept), n_killed=len(killed),
                n_klos=len(k_los), n_kwin=len(k_win), saved=saved, forgone=forgone,
                net=net, base_pnl=base_pnl, kept_pnl=kept_pnl,
                base_wr=base_wr, kept_wr=kept_wr)


def main():
    wins = load_windows(GATE_TICKS)
    settle = build_settlement(wins)
    live_pnl = load_live_pnl(TRADES)
    entries = collect_entries(wins, settle, live_pnl)

    n = len(entries)
    losers = [e for e in entries if not e["win"]]
    usable15 = [e for e in entries if abs(e["m15"]) >= COLD_EPS]
    usable30 = [e for e in entries if abs(e["m30"]) >= COLD_EPS]
    live_n = sum(1 for e in entries if e["src"] == "LIVE")

    print("=" * 72)
    print("MOMENTUM-FILTER BACKTEST  (entry-velocity same-direction gate)")
    print("=" * 72)
    print(f"settleable windows           : {len(settle)}")
    print(f"system entries (signal_ready): {n}   (LIVE-confirmed PnL: {live_n})")
    print(f"  baseline WR                : {sum(e['win'] for e in entries)}/{n} "
          f"= {sum(e['win'] for e in entries)/n*100:.1f}%")
    print(f"  baseline total PnL         : ${sum(e['pnl'] for e in entries):+.2f}")
    print(f"  losers in set              : {len(losers)}  "
          f"(PnL each: {', '.join(f'${e['pnl']:+.2f}' for e in losers)})")
    print(f"  entries w/ USABLE m15 (>={COLD_EPS}%): {len(usable15)}/{n}  "
          f"-> {n-len(usable15)} cold/zero, gate inoperative there")
    print(f"  entries w/ USABLE m30 (>={COLD_EPS}%): {len(usable30)}/{n}")
    print()

    for mom_key in ("m15", "m30"):
        print(f"--- gate on {mom_key} (strict sign, eps sweep) ---")
        print(f"{'eps%':>6} {'kept':>5} {'killed':>7} {'kLoser':>7} {'kWin':>5} "
              f"{'$saved':>8} {'$forgone':>9} {'$NET':>8} {'keptWR':>7} {'keptPnL':>9}")
        for eps in (0.0, 0.003, 0.01, 0.02, 0.03):
            r = report_filter(entries, mom_key, eps)
            print(f"{eps:>6.3f} {r['n_kept']:>5} {r['n_killed']:>7} "
                  f"{r['n_klos']:>7} {r['n_kwin']:>5} "
                  f"${r['saved']:>7.2f} ${r['forgone']:>8.2f} ${r['net']:>7.2f} "
                  f"{r['kept_wr']:>6.1f}% ${r['kept_pnl']:>8.2f}")
        print()

    # restrict to USABLE-momentum subset (honest test: only where gate can act)
    print("--- gate on m30, eps=0.003, USABLE-momentum subset only ---")
    sub = usable30
    r = report_filter(sub, "m30", 0.003)
    print(f"subset n={len(sub)}  baseline WR={sum(e['win'] for e in sub)/len(sub)*100:.1f}%  "
          f"baseline PnL=${sum(e['pnl'] for e in sub):+.2f}")
    print(f"kept={r['n_kept']} killed={r['n_killed']} "
          f"(losers={r['n_klos']} winners={r['n_kwin']})  "
          f"$saved={r['saved']:+.2f} $forgone={r['forgone']:+.2f} NET=${r['net']:+.2f}  "
          f"keptWR={r['kept_wr']:.1f}% keptPnL=${r['kept_pnl']:+.2f}")
    print()

    # killed-entry detail at the headline setting
    print("--- detail: entries KILLED by m30 strict-sign (eps=0.003) ---")
    print(f"{'window':>11} {'side':>4} {'entry':>5} {'m30':>8} {'win':>4} {'pnl$':>7} {'src':>5}")
    for e in entries:
        if not keep(e, "m30", 0.003):
            print(f"{e['ws']:>11} {'UP' if e['side_up'] else 'DOWN':>4} "
                  f"{e['entry_px']:>5.2f} {e['m30']:>8.4f} "
                  f"{'W' if e['win'] else 'L':>4} {e['pnl']:>7.2f} {e['src']:>5}")


if __name__ == "__main__":
    main()
