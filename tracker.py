"""Quant-grade performance tracker for PolyBot.

Three log files, all append-only CSV:

1. signals.csv — Every signal the strategy evaluates (traded or not).
   Answers: Are our edges real? What are we missing?

2. trades.csv — Full lifecycle of every trade (entry → hold → exit → resolve).
   Answers: How well do we execute? Where does P&L leak?

3. executions.csv — Every API call with timing.
   Answers: How fast are we? Where's the latency?

Usage:
    tracker = Tracker(log_dir="logs")
    tracker.log_signal(...)       # Every evaluate() result
    tracker.log_trade_entry(...)  # On buy fill
    tracker.log_trade_exit(...)   # On sell/stop/TP
    tracker.log_trade_resolve(...)# At window close
    tracker.log_execution(...)    # Every API call
    tracker.session_summary()     # On shutdown
"""

import os
import csv
import time
from typing import Optional
from security import sanitize_exception_text


# ── Signal record ───────────────────────────────────────────────────

SIGNAL_FIELDS = [
    "timestamp", "window_ts", "window_time",
    # Market state
    "btc_price", "opening_price", "btc_delta_pct",
    "up_price", "down_price", "seconds_remaining",
    # Signal output
    "side", "true_prob", "market_price", "edge",
    "kelly_size", "markov_persistence", "fee_rate_bps", "fee_adjusted_edge",
    # What happened
    "action",           # "traded", "skipped_edge_gone", "skipped_below_min",
                        # "skipped_price_cap", "skipped_no_signal", etc.
    "skip_reason",      # "delta_too_small", "prob_below_min", "edge_below_min",
                        # "price_out_of_range", "edge_gone_at_market", or ""
    "actual_price",     # Real market price after preview (0 if not checked)
    "actual_edge",      # Edge at actual price
    "fill_price",       # What we actually paid (0 if not traded)
    "slippage",         # fill_price - market_price (0 if not traded)
    "book_best_bid", "book_best_ask", "book_worst_ask_at_cap",
    "book_cap_price", "book_depth_shares_at_cap", "book_required_shares",
    "book_depth_usd_at_cap", "book_age_ms", "book_depth_enough",
]

# ── Trade record ────────────────────────────────────────────────────

TRADE_FIELDS = [
    "timestamp", "window_ts", "window_time", "trade_id",
    "mode",
    # Entry
    "side", "entry_price", "entry_shares", "entry_cost",
    "edge_at_entry", "prob_at_entry", "btc_delta_at_entry",
    "seconds_remaining_at_entry",
    "entry_delta_pct",          # BTC delta at moment of entry
    "entry_seconds_remaining",  # T-minus at entry
    "entry_latency_ms",         # signal → fill confirmed
    # Hold
    "max_prob_during_hold",     # Peak probability while holding
    "min_prob_during_hold",     # Trough probability
    "max_sell_price_seen",      # Best exit we saw
    "min_sell_price_seen",      # Worst exit we saw
    # Exit
    "exit_type",                # "take-profit", "prob-stop", "price-stop",
                                # "forced-exit", "resolution", "hold-to-resolution"
    "exit_price", "exit_shares_sold", "exit_revenue",
    "residual_shares", "residual_value",
    "exit_latency_ms",
    # Resolution
    "btc_final_price", "btc_final_delta_pct",
    "final_price_source",
    "official_open_price", "official_close_price", "official_delta_pct",
    "official_winning_side",
    "won_resolution",           # Did BTC go our way?
    "resolution_payout",        # What resolution would have paid
    "resolution_method",        # "claim_sell", "balance_check", "official_csv", "dry_binance_fallback", "exited"
    "claim_result",             # "filled", "no_match", "inconclusive", "not_attempted"
    # P&L
    "profit", "return_pct",
    "profit_if_held",           # What we'd have made holding to resolution
]

# ── Execution record ────────────────────────────────────────────────

EXECUTION_FIELDS = [
    "timestamp", "window_ts",
    "action",                   # "buy", "sell", "get_price",
                                # "check_order", "cancel"
    "latency_ms",
    "success",
    "error",
    "details",                  # JSON-safe string with relevant params
]

GATE_TICK_FIELDS = [
    "timestamp", "window_ts", "window_time",
    "btc_price", "signal_btc_price", "opening_price", "btc_delta_pct",
    "up_price", "down_price", "seconds_remaining",
    "candidate_side", "candidate_market_price", "opposite_market_price",
    "true_prob", "raw_edge", "fee_adjusted_edge", "kelly_size",
    "markov_persistence", "markov_same", "markov_total",
    "markov_directional_samples", "markov_flat_samples", "markov_threshold",
    "realized_vol", "fee_rate_bps",
    "source_action", "source_reason", "chainlink_age_seconds", "rtds_binance_age_seconds",
    "source_gap_bps", "direct_vs_rtds_binance_gap_bps",
    "momentum_15s_pct", "momentum_30s_pct", "entry_spread", "book_imbalance",
    "time_bucket", "delta_bucket", "prob_bucket", "price_bucket", "edge_bucket",
    "gate_reason", "signal_ready", "extreme_book", "book_state",
]

SESSION_FIELDS = [
    "start_time", "end_time",
    "start_balance", "end_balance",
    "real_pnl",                 # end_balance - start_balance
    "tracked_pnl",              # from stats
    "pnl_drift",                # real_pnl - tracked_pnl
    "trades", "wins", "losses", "win_rate",
    "avg_entry_price", "avg_edge", "avg_delta",
]

DRY_RUN_SESSION_FIELDS = [
    "window_ts", "window_time", "window_end_time",
    "signals_detected", "traded", "side",
    "entry_price", "entry_cost", "entry_shares",
    "markov_state", "markov_persistence", "markov_threshold",
    "won_resolution", "simulated_profit",
    "opening_price", "final_price", "btc_final_delta_pct",
    "threshold_trades", "threshold_wins", "threshold_win_rate",
]


class Tracker:
    def __init__(self, log_dir: str = "logs", log_executions: bool = False):
        self.log_dir = log_dir
        self.log_executions = log_executions
        os.makedirs(log_dir, exist_ok=True)

        self._signal_path = os.path.join(log_dir, "signals.csv")
        self._trade_path = os.path.join(log_dir, "trades.csv")
        self._dry_trade_path = os.path.join(log_dir, "dry_run_trades.csv")
        self._exec_path = os.path.join(log_dir, "executions.csv")
        self._session_path = os.path.join(log_dir, "sessions.csv")
        self._dry_run_session_path = os.path.join(log_dir, "dry_run_sessions.csv")
        self._gate_tick_path = os.path.join(log_dir, "gate_ticks.csv")

        self._ensure_headers(self._signal_path, SIGNAL_FIELDS)
        self._ensure_headers(self._trade_path, TRADE_FIELDS)
        self._ensure_headers(self._dry_trade_path, TRADE_FIELDS)
        self._ensure_headers(self._session_path, SESSION_FIELDS)
        self._ensure_headers(self._dry_run_session_path, DRY_RUN_SESSION_FIELDS)
        self._ensure_headers(self._gate_tick_path, GATE_TICK_FIELDS)
        if self.log_executions:
            self._ensure_headers(self._exec_path, EXECUTION_FIELDS)

        # In-memory state for current trade
        self._current_trade: dict = {}
        self._current_trade_path: str = self._trade_path
        self._trade_counter: int = 0
        # Per-window holding slots for deferred (sub-$5 / hold-to-resolution)
        # trades. Keyed by window_ts so a later window's entry can never clobber
        # a deferred trade's entry snapshot before its resolution writes a row.
        self._pending_trades: dict = {}

        # Session stats
        self._session_start = time.time()
        self._session_start_balance: float = 0.0
        self._signals_total: int = 0
        self._signals_traded: int = 0
        self._signals_skipped_edge: int = 0
        self._signals_skipped_min: int = 0
        self._signals_skipped_cap: int = 0
        self._total_slippage: float = 0.0
        self._slippage_count: int = 0
        self._total_latency_ms: float = 0.0
        self._latency_count: int = 0

    def set_session_balance(self, balance: float):
        self._session_start_balance = balance

    # ── Signal logging ──────────────────────────────────────────────

    def log_signal(
        self,
        window_ts: int,
        btc_price: float,
        opening_price: float,
        up_price: float,
        down_price: float,
        seconds_remaining: float,
        # Signal (None if no signal)
        side: str = "",
        true_prob: float = 0.0,
        market_price: float = 0.0,
        edge: float = 0.0,
        kelly_size: float = 0.0,
        markov_persistence: float = 0.0,
        fee_rate_bps: float = 0.0,
        fee_adjusted_edge: float = 0.0,
        # Outcome
        action: str = "no_signal",
        skip_reason: str = "",
        actual_price: float = 0.0,
        actual_edge: float = 0.0,
        fill_price: float = 0.0,
        book_best_bid: float = 0.0,
        book_best_ask: float = 0.0,
        book_worst_ask_at_cap: float = 0.0,
        book_cap_price: float = 0.0,
        book_depth_shares_at_cap: float = 0.0,
        book_required_shares: float = 0.0,
        book_depth_usd_at_cap: float = 0.0,
        book_age_ms: float = 0.0,
        book_depth_enough: bool = False,
    ):
        self._signals_total += 1
        if action == "traded":
            self._signals_traded += 1
        elif "edge" in action:
            self._signals_skipped_edge += 1
        elif "min" in action:
            self._signals_skipped_min += 1
        elif "cap" in action:
            self._signals_skipped_cap += 1

        slippage = fill_price - market_price if fill_price > 0 and market_price > 0 else 0.0
        if fill_price > 0:
            self._total_slippage += slippage
            self._slippage_count += 1

        btc_delta_pct = ((btc_price - opening_price) / opening_price * 100) if opening_price > 0 else 0

        row = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "window_time": time.strftime("%H:%M", time.localtime(window_ts)),
            "btc_price": round(btc_price, 2),
            "opening_price": round(opening_price, 2),
            "btc_delta_pct": round(btc_delta_pct, 4),
            "up_price": round(up_price, 3),
            "down_price": round(down_price, 3),
            "seconds_remaining": round(seconds_remaining, 1),
            "side": side,
            "true_prob": round(true_prob, 4),
            "market_price": round(market_price, 4),
            "edge": round(edge, 4),
            "kelly_size": round(kelly_size, 2),
            "markov_persistence": round(markov_persistence, 4),
            "fee_rate_bps": round(fee_rate_bps, 4),
            "fee_adjusted_edge": round(fee_adjusted_edge, 4),
            "action": action,
            "skip_reason": skip_reason,
            "actual_price": round(actual_price, 4),
            "actual_edge": round(actual_edge, 4),
            "fill_price": round(fill_price, 4),
            "slippage": round(slippage, 4),
            "book_best_bid": round(book_best_bid, 4),
            "book_best_ask": round(book_best_ask, 4),
            "book_worst_ask_at_cap": round(book_worst_ask_at_cap, 4),
            "book_cap_price": round(book_cap_price, 4),
            "book_depth_shares_at_cap": round(book_depth_shares_at_cap, 4),
            "book_required_shares": round(book_required_shares, 4),
            "book_depth_usd_at_cap": round(book_depth_usd_at_cap, 4),
            "book_age_ms": round(book_age_ms, 1),
            "book_depth_enough": int(bool(book_depth_enough)),
        }
        self._append_row(self._signal_path, row, SIGNAL_FIELDS)

    def log_gate_tick(
        self,
        window_ts: int,
        btc_price: float,
        signal_btc_price: float,
        opening_price: float,
        up_price: float,
        down_price: float,
        seconds_remaining: float,
        candidate_side: str,
        candidate_market_price: float,
        opposite_market_price: float,
        true_prob: float,
        raw_edge: float,
        fee_adjusted_edge: float,
        kelly_size: float,
        markov_persistence: float,
        markov_stats: Optional[dict] = None,
        markov_threshold: float = 0.0,
        realized_vol: float = 0.0,
        fee_rate_bps: float = 0.0,
        source_decision=None,
        gate_reason: str = "",
        signal_ready: bool = False,
        extreme_book: bool = False,
        book_state: str = "",
        momentum_15s_pct: float = 0.0,
        momentum_30s_pct: float = 0.0,
        entry_spread: float = 0.0,
        book_imbalance: float = 0.0,
        time_bucket: str = "",
        delta_bucket: str = "",
        prob_bucket: str = "",
        price_bucket: str = "",
        edge_bucket: str = "",
    ):
        markov_stats = markov_stats or {}
        btc_delta_pct = ((signal_btc_price - opening_price) / opening_price * 100) if opening_price > 0 else 0.0
        row = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "window_time": time.strftime("%H:%M", time.localtime(window_ts)),
            "btc_price": round(btc_price, 2),
            "signal_btc_price": round(signal_btc_price, 2),
            "opening_price": round(opening_price, 2),
            "btc_delta_pct": round(btc_delta_pct, 4),
            "up_price": round(up_price, 3),
            "down_price": round(down_price, 3),
            "seconds_remaining": round(seconds_remaining, 1),
            "candidate_side": candidate_side,
            "candidate_market_price": round(candidate_market_price, 4),
            "opposite_market_price": round(opposite_market_price, 4),
            "true_prob": round(true_prob, 4),
            "raw_edge": round(raw_edge, 4),
            "fee_adjusted_edge": round(fee_adjusted_edge, 4),
            "kelly_size": round(kelly_size, 2),
            "markov_persistence": round(markov_persistence, 4),
            "markov_same": int(markov_stats.get("same", 0)),
            "markov_total": int(markov_stats.get("total", 0)),
            "markov_directional_samples": int(markov_stats.get("directional_samples", 0)),
            "markov_flat_samples": int(markov_stats.get("flat_samples", 0)),
            "markov_threshold": round(markov_threshold, 4),
            "realized_vol": round(realized_vol, 4),
            "fee_rate_bps": round(fee_rate_bps, 4),
            "source_action": getattr(source_decision, "action", ""),
            "source_reason": getattr(source_decision, "reason", ""),
            "chainlink_age_seconds": round(getattr(source_decision, "chainlink_age_seconds", 0.0) or 0.0, 3),
            "rtds_binance_age_seconds": round(getattr(source_decision, "rtds_binance_age_seconds", 0.0) or 0.0, 3),
            "source_gap_bps": round(getattr(source_decision, "source_gap_bps", 0.0) or 0.0, 4),
            "direct_vs_rtds_binance_gap_bps": round(getattr(source_decision, "direct_vs_rtds_binance_gap_bps", 0.0) or 0.0, 4),
            "momentum_15s_pct": round(momentum_15s_pct, 4),
            "momentum_30s_pct": round(momentum_30s_pct, 4),
            "entry_spread": round(entry_spread, 4),
            "book_imbalance": round(book_imbalance, 4),
            "time_bucket": time_bucket,
            "delta_bucket": delta_bucket,
            "prob_bucket": prob_bucket,
            "price_bucket": price_bucket,
            "edge_bucket": edge_bucket,
            "gate_reason": gate_reason,
            "signal_ready": int(bool(signal_ready)),
            "extreme_book": int(bool(extreme_book)),
            "book_state": book_state,
        }
        self._append_row(self._gate_tick_path, row, GATE_TICK_FIELDS)

    # ── Trade lifecycle ─────────────────────────────────────────────

    def log_trade_entry(
        self,
        window_ts: int,
        side: str,
        entry_price: float,
        entry_shares: float,
        entry_cost: float,
        edge: float,
        prob: float,
        btc_delta: float,
        seconds_remaining: float,
        latency_ms: float = 0.0,
        entry_delta_pct: float = 0.0,
        entry_seconds_remaining: float = 0.0,
        mode: str = "LIVE",
    ):
        mode = "DRY" if str(mode).upper() in {"DRY", "DRY_RUN", "PAPER"} else "LIVE"
        self._trade_counter += 1
        self._current_trade_path = self._dry_trade_path if mode == "DRY" else self._trade_path
        self._current_trade = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "window_time": time.strftime("%H:%M", time.localtime(window_ts)),
            "trade_id": self._trade_counter,
            "mode": mode,
            "side": side,
            "entry_price": round(entry_price, 4),
            "entry_shares": round(entry_shares, 1),
            "entry_cost": round(entry_cost, 2),
            "edge_at_entry": round(edge, 4),
            "prob_at_entry": round(prob, 4),
            "btc_delta_at_entry": round(btc_delta, 4),
            "seconds_remaining_at_entry": round(seconds_remaining, 1),
            "entry_latency_ms": round(latency_ms, 0),
            "entry_delta_pct": round(entry_delta_pct, 4),
            "entry_seconds_remaining": round(entry_seconds_remaining, 1),
            # Hold tracking — updated live
            "max_prob_during_hold": round(prob, 4),
            "min_prob_during_hold": round(prob, 4),
            "max_sell_price_seen": 0.0,
            "min_sell_price_seen": 999.0,
        }

    def update_hold_stats(self, prob: float, sell_price: float):
        """Call on each position check tick to track hold-period extremes."""
        if not self._current_trade:
            return
        if prob > self._current_trade["max_prob_during_hold"]:
            self._current_trade["max_prob_during_hold"] = round(prob, 4)
        if prob < self._current_trade["min_prob_during_hold"]:
            self._current_trade["min_prob_during_hold"] = round(prob, 4)
        if sell_price > 0:
            if sell_price > self._current_trade["max_sell_price_seen"]:
                self._current_trade["max_sell_price_seen"] = round(sell_price, 4)
            if sell_price < self._current_trade["min_sell_price_seen"]:
                self._current_trade["min_sell_price_seen"] = round(sell_price, 4)

    def log_trade_exit(
        self,
        exit_type: str,
        exit_price: float,
        exit_shares_sold: float,
        exit_revenue: float,
        residual_shares: float,
        latency_ms: float = 0.0,
    ):
        if not self._current_trade:
            return
        self._current_trade["exit_type"] = exit_type
        self._current_trade["exit_price"] = round(exit_price, 4)
        self._current_trade["exit_shares_sold"] = round(exit_shares_sold, 1)
        self._current_trade["exit_revenue"] = round(exit_revenue, 2)
        self._current_trade["residual_shares"] = round(residual_shares, 1)
        self._current_trade["residual_value"] = round(residual_shares * exit_price, 2)
        self._current_trade["exit_latency_ms"] = round(latency_ms, 0)

    def log_trade_resolve(
        self,
        btc_final_price: float,
        opening_price: float,
        won: bool,
        profit: float,
        exit_revenue: float = 0.0,
        resolution_method: str = "unresolved",
        claim_result: str = "not_attempted",
        final_price_source: str = "unknown",
        official_open_price: float = 0.0,
        official_close_price: float = 0.0,
        official_completed: bool = False,
    ):
        if not self._current_trade:
            return
        self._finalize_trade_row(
            self._current_trade, self._current_trade_path,
            btc_final_price, opening_price, won, profit, exit_revenue,
            resolution_method, claim_result, final_price_source,
            official_open_price, official_close_price, official_completed,
        )
        self._current_trade = {}
        self._current_trade_path = self._trade_path

    def stash_pending_trade(self, window_ts: int) -> None:
        """Move the in-flight trade into a per-window holding slot.

        Hold-to-resolution + sub-$5 deferral resolves a trade one or two window
        boundaries AFTER entry. Before this, the single-slot `_current_trade`
        was overwritten by the NEXT window's entry before the deferred
        resolution wrote its row, so the deferred row was written with the wrong
        (later) window's entry data. Stashing per window_ts keeps each deferred
        trade's entry snapshot intact until its own resolution.
        """
        if not self._current_trade:
            return
        self._pending_trades[int(window_ts)] = (
            self._current_trade_path, self._current_trade,
        )
        self._current_trade = {}
        self._current_trade_path = self._trade_path

    def has_pending_trade(self, window_ts: int) -> bool:
        return int(window_ts) in self._pending_trades

    def resolve_pending_trade(
        self,
        window_ts: int,
        btc_final_price: float,
        opening_price: float,
        won: bool,
        profit: float,
        exit_revenue: float = 0.0,
        resolution_method: str = "auto_resolution",
        claim_result: str = "redeemed",
        final_price_source: str = "polymarket_crypto_price",
        official_open_price: float = 0.0,
        official_close_price: float = 0.0,
        official_completed: bool = False,
    ) -> bool:
        """Resolve a previously stashed deferred trade, keyed to its own window.

        Writes the row from the stashed entry snapshot, so it can never be
        clobbered by a later window's entry. Returns True if a row was written.
        """
        entry = self._pending_trades.pop(int(window_ts), None)
        if not entry:
            return False
        path, trade = entry
        self._finalize_trade_row(
            trade, path,
            btc_final_price, opening_price, won, profit, exit_revenue,
            resolution_method, claim_result, final_price_source,
            official_open_price, official_close_price, official_completed,
        )
        return True

    def _finalize_trade_row(
        self, trade: dict, path: str,
        btc_final_price: float, opening_price: float, won: bool, profit: float,
        exit_revenue: float, resolution_method: str, claim_result: str,
        final_price_source: str, official_open_price: float,
        official_close_price: float, official_completed: bool,
    ) -> None:
        entry_cost = trade.get("entry_cost", 0)
        entry_shares = trade.get("entry_shares", 0)

        btc_delta = ((btc_final_price - opening_price) / opening_price * 100) if opening_price > 0 else 0
        official_delta = (
            (official_close_price - official_open_price) / official_open_price * 100
            if official_open_price > 0 and official_close_price > 0
            else 0.0
        )
        official_winning_side = ""
        if official_open_price > 0 and official_close_price > 0 and official_completed:
            official_winning_side = "UP" if official_close_price >= official_open_price else "DOWN"
        resolution_payout = entry_shares * 1.0 if won else 0.0
        profit_if_held = resolution_payout - entry_cost

        trade["btc_final_price"] = round(btc_final_price, 2)
        trade["btc_final_delta_pct"] = round(btc_delta, 4)
        trade["final_price_source"] = final_price_source
        trade["official_open_price"] = round(official_open_price, 2) if official_open_price > 0 else 0.0
        trade["official_close_price"] = round(official_close_price, 2) if official_close_price > 0 else 0.0
        trade["official_delta_pct"] = round(official_delta, 4)
        trade["official_winning_side"] = official_winning_side
        trade["won_resolution"] = won
        trade["resolution_payout"] = round(resolution_payout, 2)
        trade["resolution_method"] = resolution_method
        trade["claim_result"] = claim_result
        trade["profit"] = round(profit, 2)
        trade["return_pct"] = round((profit / entry_cost * 100) if entry_cost > 0 else 0, 2)
        trade["profit_if_held"] = round(profit_if_held, 2)

        # Set defaults for missing exit fields (held to resolution)
        if "exit_type" not in trade:
            trade["exit_type"] = "resolution"
            trade["exit_price"] = 0.0
            trade["exit_shares_sold"] = 0.0
            trade["exit_revenue"] = round(exit_revenue, 2)
            trade["residual_shares"] = round(entry_shares, 1)
            trade["residual_value"] = 0.0
            trade["exit_latency_ms"] = 0

        # Fix min_sell_price sentinel
        if trade.get("min_sell_price_seen", 999) >= 999:
            trade["min_sell_price_seen"] = 0.0

        # Write the complete trade record
        self._append_row(path, trade, TRADE_FIELDS)

    # ── Execution logging ───────────────────────────────────────────

    def log_execution(
        self,
        window_ts: int,
        action: str,
        latency_ms: float,
        success: bool,
        error: str = "",
        details: str = "",
    ):
        self._total_latency_ms += latency_ms
        self._latency_count += 1

        if not self.log_executions:
            return

        row = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "action": action,
            "latency_ms": round(latency_ms, 1),
            "success": success,
            "error": sanitize_exception_text(error),
            "details": sanitize_exception_text(details)[:200],  # Truncate long error messages
        }
        self._append_row(self._exec_path, row, EXECUTION_FIELDS)

    # ── Session summary ─────────────────────────────────────────────

    def session_summary(self, final_balance: float) -> dict:
        runtime_min = (time.time() - self._session_start) / 60
        real_pnl = final_balance - self._session_start_balance
        avg_slippage = (self._total_slippage / self._slippage_count
                        if self._slippage_count > 0 else 0)
        avg_latency = (self._total_latency_ms / self._latency_count
                       if self._latency_count > 0 else 0)
        fill_rate = (self._signals_traded / self._signals_total * 100
                     if self._signals_total > 0 else 0)

        summary = {
            "runtime_minutes": round(runtime_min, 1),
            "signals_total": self._signals_total,
            "signals_traded": self._signals_traded,
            "signals_skipped_edge": self._signals_skipped_edge,
            "signals_skipped_min": self._signals_skipped_min,
            "signals_skipped_cap": self._signals_skipped_cap,
            "fill_rate_pct": round(fill_rate, 1),
            "avg_slippage": round(avg_slippage, 4),
            "avg_latency_ms": round(avg_latency, 1),
            "session_start_balance": round(self._session_start_balance, 2),
            "session_end_balance": round(final_balance, 2),
            "real_pnl": round(real_pnl, 2),
        }

        print(f"\n{'═' * 55}")
        print(f"  📊 SESSION ANALYTICS")
        print(f"  Runtime: {runtime_min:.0f}min | "
              f"Signals: {self._signals_total} "
              f"({self._signals_traded} traded, "
              f"{self._signals_skipped_edge} edge-gone, "
              f"{self._signals_skipped_min} below-min, "
              f"{self._signals_skipped_cap} price-cap)")
        print(f"  Fill rate: {fill_rate:.0f}% | "
              f"Avg slippage: {avg_slippage:+.4f} | "
              f"Avg latency: {avg_latency:.0f}ms")
        print(f"  Real P&L: ${real_pnl:+.2f} "
              f"(${self._session_start_balance:.2f} → ${final_balance:.2f})")
        print(f"  Logs: {self.log_dir}/")
        print(f"{'═' * 55}")

        return summary

    # ── Session logging ─────────────────────────────────────────────

    def log_session(
        self,
        start_time: float,
        end_time: float,
        start_balance: float,
        end_balance: float,
        tracked_pnl: float,
        trades: int,
        wins: int,
        losses: int,
        avg_entry_price: float = 0.0,
        avg_edge: float = 0.0,
        avg_delta: float = 0.0,
    ):
        real_pnl = end_balance - start_balance
        win_rate = (wins / trades * 100) if trades > 0 else 0.0
        row = {
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
            "start_balance": round(start_balance, 2),
            "end_balance": round(end_balance, 2),
            "real_pnl": round(real_pnl, 2),
            "tracked_pnl": round(tracked_pnl, 2),
            "pnl_drift": round(real_pnl - tracked_pnl, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "avg_entry_price": round(avg_entry_price, 4),
            "avg_edge": round(avg_edge, 4),
            "avg_delta": round(avg_delta, 4),
        }
        self._append_row(self._session_path, row, SESSION_FIELDS)

    def log_dry_run_session(
        self,
        window_ts: int,
        window_end_ts: int,
        signals_detected: int,
        traded: bool,
        side: str = "",
        entry_price: float = 0.0,
        entry_cost: float = 0.0,
        entry_shares: float = 0.0,
        markov_state: str = "",
        markov_persistence: float = 0.0,
        markov_threshold: float = 0.0,
        simulated_profit: float = 0.0,
        won_resolution: bool | str = "",
        opening_price: float = 0.0,
        final_price: float = 0.0,
        btc_final_delta_pct: float = 0.0,
        threshold_trades: int = 0,
        threshold_wins: int = 0,
    ):
        """Append one completed 5-minute dry-run market session."""
        threshold_win_rate = (
            threshold_wins / threshold_trades * 100 if threshold_trades > 0 else 0.0
        )
        row = {
            "window_ts": window_ts,
            "window_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(window_ts)),
            "window_end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(window_end_ts)),
            "signals_detected": int(signals_detected),
            "traded": bool(traded),
            "side": side,
            "entry_price": round(entry_price, 4),
            "entry_cost": round(entry_cost, 2),
            "entry_shares": round(entry_shares, 4),
            "markov_state": markov_state,
            "markov_persistence": round(markov_persistence, 4),
            "markov_threshold": round(markov_threshold, 4),
            "won_resolution": won_resolution,
            "simulated_profit": round(simulated_profit, 2),
            "opening_price": round(opening_price, 2),
            "final_price": round(final_price, 2),
            "btc_final_delta_pct": round(btc_final_delta_pct, 4),
            "threshold_trades": threshold_trades,
            "threshold_wins": threshold_wins,
            "threshold_win_rate": round(threshold_win_rate, 1),
        }
        self._append_row(self._dry_run_session_path, row, DRY_RUN_SESSION_FIELDS)

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_headers(self, path: str, fields: list):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
            return

        # Schema migration: incompatible old CSVs are reset. Mixing legacy
        # adjusted/basis rows with RTDS-source rows corrupts analysis.
        try:
            with open(path, newline="") as f:
                existing_fields = csv.DictReader(f).fieldnames or []
            if existing_fields == fields:
                return
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
        except Exception as e:
            print(f"[tracker] Header migration failed for {path}: {e}")

    def _append_row(self, path: str, row: dict, fields: list):
        # Only write fields that exist in the schema
        clean_row = {k: row.get(k, "") for k in fields}
        try:
            with open(path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writerow(clean_row)
        except Exception as e:
            print(f"[tracker] Write failed: {e}")
