#!/usr/bin/env python3
"""
PolyBot v14 — Huge-edge FAK Taker + Hold-to-resolution

Strategy:
  - Brownian motion model with vol=0.12 (recalibrated from 0.08)
  - Entry gate: model confidence >= 80%, market price <= true_prob * 0.85
  - Position sizing: quarter-Kelly, $5–$25 per trade
  - Exit: hold all positions to resolution — no stops, no take-profit

Safety systems:
  1. CLOB health check: get_ok() before every trade; 3 consecutive
     failures halt trading and send Telegram alert. Auto-recovers
     when API comes back at next window boundary.
  2. Daily loss limit: if session P&L <= -DAILY_LOSS_LIMIT, halt trading.
  3. Balance-verified buys: snapshot collateral before/after; ghost fills
     caught even when API throws. Never cancels on timeout — returns
     UNVERIFIED_BUY for pending detection at next window boundary.
  4. Pending buy safety net: if buy unverified, check balance at next
     window boundary; retroactively track as filled if balance dropped.
  5. Window-boundary balance sync: real collateral balance overwrites internal
     tracking every 5 minutes. Corrects any accumulated drift.
  6. Minimum notional guard: skip sells below $5 notional; hold to
     resolution instead of hitting Polymarket's minimum-size rejection.
"""

import os
import sys
import time
import signal
import math
import statistics
from dotenv import load_dotenv

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from market import fetch_crypto_window_price, get_current_market, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from source_consensus import (
    PolymarketRtdsFeed,
    SourceConsensusConfig,
    SourceConsensusGate,
)
from strategy import (
    evaluate,
    estimate_true_probability,
    fee_adjusted_edge,
    get_skip_reason,
    kelly_bet_size,
    StrategyConfig,
    TradingStats,
)
from markov import MarkovPersistenceFilter
from security import sanitize_exception_text
from executor import Executor, MAX_BUY_PRICE, POLY_MIN_NOTIONAL, calculate_order_size
from telegram_notifier import TelegramNotifier
from tracker import Tracker
from clob_orderbook_cache import ClobOrderBookCache


POSITION_CHECK_INTERVAL = 3


def choose_fak_price_cap(
    *,
    true_prob: float,
    executable_price: float,
    fee_rate_bps: float,
    required_fee_edge: float,
    tick_size: float = 0.01,
    slippage_ticks: int = 1,
) -> float:
    """Return the highest FAK cap whose fee-adjusted edge still clears policy."""
    if executable_price <= 0:
        return 0.0
    tick = tick_size if tick_size > 0 else 0.01
    base = round(round(executable_price / tick) * tick, 6)
    cap = min(MAX_BUY_PRICE, 1.0 - tick, round(base + max(0, slippage_ticks) * tick, 6))
    if fee_adjusted_edge(true_prob, cap, fee_rate_bps) >= required_fee_edge:
        return cap
    return base


def compute_resolution_bankroll(bankroll_before_resolution: float, total_received: float) -> float:
    """Return post-resolution bankroll without double-counting P&L.

    Entry cost is deducted when the position opens. At resolution, the cash
    ledger changes only by gross received value (claim revenue, auto-resolution
    payout, or zero for a loss). P&L stats are recorded separately.
    """
    return round(float(bankroll_before_resolution) + float(total_received), 2)


def probability_for_held_side(
    btc_delta_pct: float,
    seconds_remaining: float,
    held_side: str,
    vol: float = 0.12,
) -> float:
    """Return Brownian probability for the side we actually hold.

    estimate_true_probability() uses abs(delta), so its return value is the
    probability that the current direction persists. It is not an unconditional
    UP probability. A negative delta means the helper is already estimating the
    DOWN probability.
    """
    direction_prob = estimate_true_probability(btc_delta_pct, seconds_remaining, vol=vol)
    current_side = "UP" if btc_delta_pct >= 0 else "DOWN"
    return direction_prob if current_side == held_side.upper() else 1.0 - direction_prob


def calibration_bucket(value: float, cuts: list[float], labels: list[str]) -> str:
    for cut, label in zip(cuts, labels):
        if value < cut:
            return label
    return labels[-1]


def calibration_buckets(
    *,
    seconds_remaining: float,
    abs_delta_pct: float,
    true_prob: float,
    market_price: float,
    fee_adjusted_edge_value: float,
) -> dict:
    """Compact forward-test buckets for calibration analysis."""
    return {
        "time_bucket": (
            "T>180" if seconds_remaining > 180 else
            "T120-180" if seconds_remaining > 120 else
            "T60-120" if seconds_remaining > 60 else
            "T<60"
        ),
        "delta_bucket": calibration_bucket(
            abs_delta_pct,
            [0.06, 0.10, 0.15, 0.25],
            ["d<0.06", "d0.06-0.10", "d0.10-0.15", "d0.15-0.25", "d>=0.25"],
        ),
        "prob_bucket": calibration_bucket(
            true_prob,
            [0.80, 0.85, 0.90, 0.95],
            ["p<0.80", "p0.80-0.85", "p0.85-0.90", "p0.90-0.95", "p>=0.95"],
        ),
        "price_bucket": calibration_bucket(
            market_price,
            [0.55, 0.65, 0.75, 0.85],
            ["q<0.55", "q0.55-0.65", "q0.65-0.75", "q0.75-0.85", "q>=0.85"],
        ),
        "edge_bucket": calibration_bucket(
            fee_adjusted_edge_value,
            [0.05, 0.10, 0.15, 0.20, 0.30],
            ["e<0.05", "e0.05-0.10", "e0.10-0.15", "e0.15-0.20", "e0.20-0.30", "e>=0.30"],
        ),
    }


class PolyBot:
    def __init__(self):
        load_dotenv()

        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        self.period = int(os.getenv("MARKET_PERIOD", "5"))

        self.strategy_config = StrategyConfig(
            min_edge=float(os.getenv("MIN_EDGE", "0.05")),
            min_prob=float(os.getenv("MIN_PROB", "0.80")),
            min_btc_delta=float(os.getenv("MIN_BTC_DELTA", "0.06")),
            entry_window_start=int(os.getenv("ENTRY_WINDOW_START", "240")),
            entry_window_end=int(os.getenv("ENTRY_WINDOW_END", "10")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            min_bet=float(os.getenv("MIN_BET", "5.0")),
            max_bet=float(os.getenv("MAX_BET", "25.0")),
            markov_persistence_threshold=float(os.getenv("MARKOV_PERSISTENCE_THRESHOLD", "0.87")),
            markov_medium_threshold=float(os.getenv("MARKOV_MEDIUM_THRESHOLD", "0.75")),
            markov_weak_min_transitions=int(os.getenv("MARKOV_WEAK_MIN_TRANSITIONS", "5")),
            markov_medium_edge=float(os.getenv("MARKOV_MEDIUM_EDGE", "0.07")),
            markov_weak_edge=float(os.getenv("MARKOV_WEAK_EDGE", "0.08")),
            markov_insufficient_edge=float(os.getenv("MARKOV_INSUFFICIENT_EDGE", "0.10")),
            markov_medium_size_multiplier=float(os.getenv("MARKOV_MEDIUM_SIZE_MULTIPLIER", "0.50")),
            markov_weak_size_multiplier=float(os.getenv("MARKOV_WEAK_SIZE_MULTIPLIER", "0.35")),
            markov_insufficient_size_multiplier=float(os.getenv("MARKOV_INSUFFICIENT_SIZE_MULTIPLIER", "0.25")),
            high_price_edge_buffer_threshold=float(os.getenv("HIGH_PRICE_EDGE_BUFFER_THRESHOLD", "0.80")),
            high_price_min_edge=float(os.getenv("HIGH_PRICE_MIN_EDGE", "0.08")),
        )
        initial_bankroll = float(os.getenv("BANKROLL", "100.0"))
        self._daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "30.0"))
        self._rolling_vol_windows = int(os.getenv("ROLLING_VOL_WINDOWS", "12"))
        self._vol_floor = float(os.getenv("VOL_FLOOR", "0.06"))
        self._vol_cap = float(os.getenv("VOL_CAP", "0.30"))
        self._vol_fallback = 0.12  # used until enough windows accumulate
        self._markov_filter = MarkovPersistenceFilter(
            lookback_seconds=float(os.getenv("MARKOV_LOOKBACK_SECONDS", "60")),
            tick_threshold_pct=float(os.getenv("MARKOV_TICK_THRESHOLD_PCT", "0.005")),
            min_transitions=int(os.getenv("MARKOV_MIN_TRANSITIONS", "8")),
        )
        self._current_fee_rate_bps: float = 0.0
        self._cached_up_fee_bps: float = 0.0
        self._cached_down_fee_bps: float = 0.0
        self._market_min_order_size: float = POLY_MIN_NOTIONAL
        self._market_tick_size: float = 0.01
        self._open_price_initial_delay: float = float(os.getenv("OPEN_PRICE_INITIAL_DELAY_SECONDS", "6"))
        self._open_price_wait_seconds: float = float(os.getenv("OPEN_PRICE_WAIT_SECONDS", "12"))
        self._open_price_retry_interval: float = float(os.getenv("OPEN_PRICE_RETRY_INTERVAL", "2"))
        self._huge_edge_min: float = float(os.getenv("ENTRY_HUGE_EDGE_MIN", "0.18"))
        self._fak_slippage_ticks: int = int(os.getenv("ENTRY_FAK_SLIPPAGE_TICKS", "1"))
        self._entry_max_spread: float = float(os.getenv("ENTRY_MAX_SPREAD", "0.08"))
        self._entry_min_exit_price: float = float(os.getenv("ENTRY_MIN_EXIT_PRICE", "0.50"))
        self._source_consensus_config = SourceConsensusConfig(
            enabled=os.getenv("SOURCE_CONSENSUS_ENABLED", "true").lower() == "true",
            require_chainlink=os.getenv(
                "SOURCE_REQUIRE_CHAINLINK",
                "true" if not self.dry_run else "false",
            ).lower() == "true",
            require_rtds_binance=os.getenv(
                "SOURCE_REQUIRE_RTDS_BINANCE",
                "true" if not self.dry_run else "false",
            ).lower() == "true",
            stale_downsize_seconds=float(os.getenv("SOURCE_STALE_DOWNSIZE_SEC", "10.0")),
            stale_skip_seconds=float(os.getenv("SOURCE_STALE_SKIP_SEC", "30.0")),
            downsize_factor=float(os.getenv("SOURCE_DOWNSIZE_FACTOR", "0.50")),
            max_rtds_source_gap_bps=float(os.getenv("SOURCE_RTDS_SOURCE_GAP_BPS", "25.0")),
            max_direct_vs_rtds_binance_gap_bps=float(os.getenv("SOURCE_DIRECT_VS_RTDS_BINANCE_GAP_BPS", "6.0")),
            min_chainlink_delta_pct=float(os.getenv("CHAINLINK_MIN_DELTA_PCT", "0.02")),
        )
        self.source_consensus = SourceConsensusGate(self._source_consensus_config)
        self._orderbook_cache_enabled: bool = os.getenv("CLOB_ORDERBOOK_CACHE_ENABLED", "true").lower() == "true"
        self._orderbook_max_age: float = float(os.getenv("CLOB_ORDERBOOK_MAX_AGE_SEC", "1.0"))
        self.orderbook_cache = ClobOrderBookCache(max_book_age_seconds=self._orderbook_max_age)
        self._current_market = None

        self.price_feed = BinancePriceFeed()
        self.rtds_feed = PolymarketRtdsFeed("BTC")
        self.executor = Executor(
            private_key=os.getenv("PRIVATE_KEY", ""),
            safe_address=os.getenv("SAFE_ADDRESS", ""),
            dry_run=self.dry_run,
        )
        self.telegram = TelegramNotifier()
        self.tracker = Tracker(
            log_dir=os.getenv("LOG_DIR", "logs"),
            log_executions=os.getenv("LOG_EXECUTIONS", "false").lower() == "true",
        )
        self.stats = TradingStats(bankroll=initial_bankroll)
        self.stats.hourly.hour_start = time.time()

        self._running = False
        self._current_window: int = 0
        # _opening_price is the Binance window-open (de-biased signal anchor):
        # the bot's own Binance price at the window boundary. All Binance-delta
        # signal/model/log math anchors to this. _chainlink_open_price is the
        # Chainlink/Polymarket settlement open, used only for Chainlink-side
        # source-consensus checks and settlement reconciliation. They differ by a
        # near-constant Binance/Chainlink basis (~14bps) — never mix them.
        self._opening_price: float = 0.0
        self._chainlink_open_price: float = 0.0
        self._window_open_price_missing: bool = False
        self._last_hour_check: int = 0
        self._dry_run_started_at: float = time.time()
        self._dry_run_max_seconds: float = float(os.getenv("DRY_RUN_MAX_HOURS", "24")) * 3600
        self._summary_interval_seconds: float = float(os.getenv("DRY_RUN_SUMMARY_HOURS", "6")) * 3600
        self._last_dry_run_summary_at: float = self._dry_run_started_at
        self._dry_overall_signals: int = 0
        self._dry_threshold_trades: int = 0
        self._dry_threshold_wins: int = 0
        self._dry_period_stats: dict = self._new_dry_period_stats()

        # Trade state
        self._traded: bool = False
        self._trade_attempted: bool = False
        self._trade_side: str = ""
        self._trade_price: float = 0.0
        self._trade_cost: float = 0.0
        self._trade_shares: float = 0.0
        self._trade_token_id: str = ""
        self._window_signals_detected: int = 0
        self._entry_markov_state: str = ""
        self._entry_markov_persistence: float = 0.0
        self._entry_markov_threshold: float = self.strategy_config.markov_persistence_threshold
        self._entry_markov_passed: bool = False

        # Resolution diagnostics
        self._exit_revenue: float = 0.0
        self._last_position_check: float = 0.0
        self._last_status_print: float = 0.0
        self._last_tick_context: dict = {}   # last entry-window state, for window-end signal logging
        self._session_start_time: float = time.time()
        self._recent_window_deltas: list = []  # rolling abs(close_delta_pct) per window
        self._last_sell_price_seen: float = 0.0  # last observed sell price during hold period

        # Pending phantom verification (claim sell reported success but balance didn't move yet)
        # Resolved at next window boundary once Polygon settlement has had time to land.
        self._pending_phantom: dict = {}

        # Pending buy (unverified — Polygon settlement too slow)
        self._pending_buy_side: str = ""
        self._pending_buy_price: float = 0.0
        self._pending_buy_amount: float = 0.0
        self._pending_buy_shares: float = 0.0
        self._pending_buy_token_id: str = ""
        self._pending_buy_edge: float = 0.0
        self._pending_buy_delta: float = 0.0
        self._balance_before_buy: float = 0.0

        # Unclaimed
        self._unclaimed_winnings: float = 0.0

        # Real balance tracking (source of truth)
        self._session_start_balance: float = 0.0
        self._last_real_balance: float = 0.0

        # Price cache
        self._cached_up: float = 0.50
        self._cached_down: float = 0.50
        self._price_last_fetched: float = 0.0
        self._PRICE_REFRESH: float = 5.0

        # Circuit breaker — detects CLOB API degradation
        self._consecutive_buy_failures: int = 0
        self._clob_halted: bool = False
        self._HALT_AFTER_FAILURES: int = 3
        self._daily_loss_halted: bool = False

    def start(self):
        if not self.dry_run:
            from proxy import ensure_tor, apply_proxy
            import logging as _log
            _log.basicConfig(level=_log.INFO, format="[%(name)s] %(message)s")
            print("\n🧅 Starting Tor proxy for CLOB API...")
            proxy_url = ensure_tor()
            apply_proxy(proxy_url)
            print(f"✅ Tor active: {proxy_url}\n")

        kf = self.strategy_config.kelly_fraction
        mp = self.strategy_config.min_prob
        me = self.strategy_config.min_edge
        print("=" * 55)
        print(f"  PolyBot v14 — Hold-to-resolution + huge-edge FAK taker")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% fraction | "
              f"Bets: ${self.strategy_config.min_bet:.0f}–${self.strategy_config.max_bet:.0f}")
        print(f"  Min prob: {mp:.0%} | ε gap: {me:.0%} | Min BTC delta: {self.strategy_config.min_btc_delta:.2f}%")
        print(
            f"  Markov risk: strong ≥{self.strategy_config.markov_persistence_threshold:.0%}; "
            f"medium ≥{self.strategy_config.markov_medium_threshold:.0%} needs "
            f"edge≥{self.strategy_config.markov_medium_edge:.0%} size×{self.strategy_config.markov_medium_size_multiplier:.2f}; "
            f"insufficient needs edge≥{self.strategy_config.markov_insufficient_edge:.0%} "
            f"size×{self.strategy_config.markov_insufficient_size_multiplier:.2f}"
        )
        print(
            f"  Source gate: {'ON' if self._source_consensus_config.enabled else 'OFF'} | "
            f"RTDS Binance required={self._source_consensus_config.require_rtds_binance} | "
            f"RTDS/CL abnormal gap>{self._source_consensus_config.max_rtds_source_gap_bps:.1f}bps | "
            f"stale skip>{self._source_consensus_config.stale_skip_seconds:.0f}s"
        )
        print(
            f"  CLOB price source: {'websocket cache' if self._orderbook_cache_enabled else 'REST'} "
            f"| max age {self._orderbook_max_age:.1f}s"
        )
        print(
            f"  Entry execution: FAK taker only | huge fee-edge ≥{self._huge_edge_min:.0%} "
            f"| cap +{self._fak_slippage_ticks} tick if edge survives"
        )
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
        print(f"  Vol: dynamic (fallback=0.12, floor={self._vol_floor}, cap={self._vol_cap}, windows={self._rolling_vol_windows})")
        print(f"  Exits: hold to resolution")
        print(f"  Daily loss limit: ${self._daily_loss_limit:.0f}")
        print(f"  Bankroll: ${self.stats.bankroll:.2f}")
        print("=" * 55)

        if not self.dry_run:
            if not self.executor.initialize():
                print("\n❌ Failed to initialize. Check credentials.")
                return
            balance = self.executor.get_collateral_balance()
            print(f"  Collateral balance: ${balance:.2f}")
            self.stats.bankroll = balance
            self._session_start_balance = balance
            self._last_real_balance = balance
            self.tracker.set_session_balance(balance)
        else:
            print("  [dry run — no wallet connection]")
            self._session_start_balance = self.stats.bankroll
            self._last_real_balance = self.stats.bankroll
            self.tracker.set_session_balance(self.stats.bankroll)

        self.price_feed.start()
        self.rtds_feed.start()
        print("\n⏳ Waiting for BTC price...")
        price = self.price_feed.wait_for_price(timeout=30)
        if not price:
            print("❌ No price feed. Check internet.")
            return
        print(f"✅ BTC: ${price:,.2f} ({self.price_feed.state.source})")

        self.telegram.startup_alert({
            "dry_run": self.dry_run,
            "kelly_fraction": kf,
            "min_edge": self.strategy_config.min_edge,
            "min_bet": self.strategy_config.min_bet,
            "max_bet": self.strategy_config.max_bet,
            "entry_start": self.strategy_config.entry_window_start,
            "entry_end": self.strategy_config.entry_window_end,
        })

        self._running = True
        self._last_hour_check = int(time.time() // 3600)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        print("\n🚀 Running. Ctrl+C to stop.\n")
        self._main_loop()

    def _new_dry_period_stats(self) -> dict:
        return {
            "start": time.time(),
            "signals": 0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "threshold_trades": 0,
            "threshold_wins": 0,
        }

    def _markov_state_label(self, side: str, persistence: float, threshold: float) -> str:
        verdict = "PASS" if persistence >= threshold else "FAIL"
        return f"{side}|p={persistence:.2f}|threshold={threshold:.2f}|{verdict}"

    def _dry_summary_dict(self, stats: dict, hours: float) -> dict:
        trades = stats["trades"]
        threshold_trades = stats["threshold_trades"]
        return {
            "hours": hours,
            "signals": stats["signals"],
            "trades": trades,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": (stats["wins"] / trades * 100) if trades else 0.0,
            "pnl": stats["pnl"],
            "threshold_trades": threshold_trades,
            "threshold_win_rate": (
                stats["threshold_wins"] / threshold_trades * 100
                if threshold_trades else 0.0
            ),
        }

    def _log_completed_dry_run_window(
        self, won: bool | str = "", simulated_profit: float = 0.0, final_price: float = 0.0
    ):
        if not self.dry_run or self._current_window <= 0:
            return
        period_secs = PERIOD_SECONDS[self.period]
        btc_final_delta_pct = (
            (final_price - self._opening_price) / self._opening_price * 100
            if self._opening_price > 0 and final_price > 0 else 0.0
        )
        self.tracker.log_dry_run_session(
            window_ts=self._current_window,
            window_end_ts=self._current_window + period_secs,
            signals_detected=self._window_signals_detected,
            traded=self._traded,
            side=self._trade_side if self._traded else "",
            entry_price=self._trade_price if self._traded else 0.0,
            entry_cost=self._trade_cost if self._traded else 0.0,
            entry_shares=self._trade_shares if self._traded else 0.0,
            markov_state=self._entry_markov_state if self._traded else "",
            markov_persistence=self._entry_markov_persistence if self._traded else 0.0,
            markov_threshold=self._entry_markov_threshold,
            simulated_profit=simulated_profit,
            won_resolution=won,
            opening_price=self._opening_price,
            final_price=final_price,
            btc_final_delta_pct=btc_final_delta_pct,
            threshold_trades=self._dry_threshold_trades,
            threshold_wins=self._dry_threshold_wins,
        )

    def _main_loop(self):
        while self._running:
            try:
                self._tick()
                self._check_hourly_summary()
                if (
                    self.dry_run
                    and self._dry_run_max_seconds > 0
                    and time.time() - self._dry_run_started_at >= self._dry_run_max_seconds
                ):
                    print("\n🧪 24h dry run complete — stopping automatically.")
                    self._handle_shutdown(signal.SIGTERM, None)
            except Exception as e:
                print(f"[error] {e}")
                self.telegram.error_alert(str(e))
            time.sleep(0.1)

    def _tick(self):
        now = time.time()
        period_secs = PERIOD_SECONDS[self.period]
        window_ts = int(now) - (int(now) % period_secs)

        btc_price, is_fresh = self.price_feed.get_price()
        if not is_fresh or btc_price <= 0:
            return
        self._markov_filter.update(btc_price, now=now)

        if window_ts != self._current_window:
            self._on_new_window(window_ts, closing_btc_price=btc_price)

        seconds_remaining = (window_ts + period_secs) - now

        # Capture the Binance window-open if the boundary tick was missing.
        if self._opening_price <= 0:
            self._opening_price = btc_price
            print(f"  📌 BN open: ${btc_price:,.2f}")
        # Live trading needs the Chainlink settlement open for source-consensus.
        if not self.dry_run and self._window_open_price_missing:
            return

        # HOLDING: active position management
        if self._traded:
            self._manage_position(btc_price, seconds_remaining, now)
            return

        # Already done
        if self._traded or self._trade_attempted:
            return

        # IDLE: Binance direct WS is the low-latency signal; RTDS Binance and
        # RTDS Chainlink are risk sources, compared directly.
        chainlink_price = self.rtds_feed.get_latest()
        get_rtds_binance = getattr(self.rtds_feed, "get_binance_latest", None)
        rtds_binance_price = get_rtds_binance() if callable(get_rtds_binance) else None
        signal_btc_price = btc_price

        up_price, down_price = self._get_market_prices(signal_btc_price, seconds_remaining)

        realized_vol = self._compute_realized_vol()
        candidate_side = "UP" if signal_btc_price >= self._opening_price else "DOWN"
        source_decision = self.source_consensus.update_snapshot(
            binance_price=btc_price,
            opening_price=self._chainlink_open_price,
            intended_side=candidate_side,
            chainlink=chainlink_price,
            rtds_binance=rtds_binance_price,
            binance_opening_price=self._opening_price,
        )
        markov_persistence = self._markov_filter.persistence(candidate_side)
        markov_stats = self._markov_filter.transition_stats(candidate_side)
        fee_rate_bps = self._cached_up_fee_bps if candidate_side == "UP" else self._cached_down_fee_bps
        signal_result = evaluate(
            btc_price=signal_btc_price,
            opening_price=self._opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            bankroll=self.stats.bankroll,
            config=self.strategy_config,
            realized_vol=realized_vol,
            markov_persistence=markov_persistence,
            markov_stats=markov_stats,
            fee_rate_bps=fee_rate_bps,
        )
        candidate_market_price = up_price if candidate_side == "UP" else down_price
        opposite_market_price = down_price if candidate_side == "UP" else up_price
        btc_delta_pct = ((signal_btc_price - self._opening_price) / self._opening_price * 100) if self._opening_price > 0 else 0.0
        # Model runs on the de-biased Binance move (same anchor as the signal),
        # NOT Chainlink-vs-open: the ~14bps basis would otherwise poison the
        # probability and force model_side to disagree with the signal.
        model_delta_pct = btc_delta_pct
        true_prob = estimate_true_probability(model_delta_pct, seconds_remaining, vol=realized_vol)
        candidate_true_prob = true_prob
        raw_edge = candidate_true_prob - candidate_market_price
        net_edge = fee_adjusted_edge(candidate_true_prob, candidate_market_price, fee_rate_bps)
        diagnostic_kelly = kelly_bet_size(
            true_prob=candidate_true_prob,
            market_price=candidate_market_price,
            bankroll=self.stats.bankroll,
            fraction=self.strategy_config.kelly_fraction,
            min_bet=self.strategy_config.min_bet,
            max_bet=self.strategy_config.max_bet,
            fee_rate_bps=fee_rate_bps,
        )
        gate_reason = "signal_ready" if signal_result else get_skip_reason(
            btc_price=signal_btc_price,
            opening_price=self._opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            config=self.strategy_config,
            realized_vol=realized_vol,
            markov_persistence=markov_persistence,
            markov_stats=markov_stats,
            fee_rate_bps=fee_rate_bps,
        )
        if candidate_market_price >= 0.90:
            book_state = "target_extreme_high_no_margin"
        elif candidate_market_price <= 0.10:
            book_state = "target_extreme_low_possible_wrong_side_or_illiquid"
        elif opposite_market_price >= 0.90 or opposite_market_price <= 0.10:
            book_state = "complement_extreme"
        else:
            book_state = "normal"
        momentum_15s = self._markov_filter.price_change_pct(15, now=now)
        momentum_30s = self._markov_filter.price_change_pct(30, now=now)
        entry_spread = abs((candidate_market_price + opposite_market_price) - 1.0)
        book_imbalance = candidate_market_price - opposite_market_price
        buckets = calibration_buckets(
            seconds_remaining=seconds_remaining,
            abs_delta_pct=abs(model_delta_pct),
            true_prob=candidate_true_prob,
            market_price=candidate_market_price,
            fee_adjusted_edge_value=net_edge,
        )
        self.tracker.log_gate_tick(
            window_ts=self._current_window,
            btc_price=btc_price,
            signal_btc_price=signal_btc_price,
            opening_price=self._opening_price,
            up_price=up_price,
            down_price=down_price,
            seconds_remaining=seconds_remaining,
            candidate_side=candidate_side,
            candidate_market_price=candidate_market_price,
            opposite_market_price=opposite_market_price,
            true_prob=candidate_true_prob,
            raw_edge=raw_edge,
            fee_adjusted_edge=net_edge,
            kelly_size=diagnostic_kelly,
            markov_persistence=markov_persistence,
            markov_stats=markov_stats,
            markov_threshold=self.strategy_config.markov_persistence_threshold,
            realized_vol=realized_vol,
            fee_rate_bps=fee_rate_bps,
            source_decision=source_decision,
            gate_reason=gate_reason,
            signal_ready=bool(signal_result),
            extreme_book=book_state != "normal",
            book_state=book_state,
            momentum_15s_pct=momentum_15s,
            momentum_30s_pct=momentum_30s,
            entry_spread=entry_spread,
            book_imbalance=book_imbalance,
            **buckets,
        )

        # Store context for window-end no-trade signal logging
        self._last_tick_context = {
            "btc_price": signal_btc_price,
            "raw_btc_price": btc_price,
            "up_price": up_price,
            "down_price": down_price,
            "seconds_remaining": seconds_remaining,
            "window_ts": self._current_window,
            "signal": signal_result,
            "markov_persistence": markov_persistence,
            "fee_rate_bps": fee_rate_bps,
            "source_decision": source_decision,
        }

        if signal_result:
            self._window_signals_detected += 1
            self._dry_overall_signals += 1
            self._dry_period_stats["signals"] += 1
            if source_decision.should_skip:
                print(
                    f"  ⚠️  Source risk gate — skipping ({source_decision.reason}; "
                    f"rtds_gap={source_decision.source_gap_bps if source_decision.source_gap_bps is not None else 0:+.1f}bps, "
                    f"direct_vs_rtds={source_decision.direct_vs_rtds_binance_gap_bps if source_decision.direct_vs_rtds_binance_gap_bps is not None else 0:.1f}bps, "
                    f"cl_age={source_decision.chainlink_age_seconds if source_decision.chainlink_age_seconds is not None else -1:.1f}s)"
                )
                self._log_source_skip(signal_result, seconds_remaining, source_decision, "skipped_source_disagreement")
                self._trade_attempted = True
                return
            if source_decision.action == "downsize":
                old_size = signal_result.kelly_size
                signal_result.kelly_size = round(max(
                    self.strategy_config.min_bet,
                    signal_result.kelly_size * source_decision.size_multiplier,
                ), 2)
                print(
                    f"  ⚠️  Source risk downsize ({source_decision.reason}): "
                    f"${old_size:.2f} → ${signal_result.kelly_size:.2f}"
                )
            self._execute_trade(signal_result, seconds_remaining)

        if now - self._last_status_print >= 30:
            self._last_status_print = now
            delta = ((btc_price - self._opening_price) / self._opening_price * 100) if self._opening_price > 0 else 0
            d = "↑" if delta > 0 else "↓" if delta < 0 else "→"
            if self._traded:
                state = "HOLDING"
            elif self._opening_price > 0 and abs(delta) < self.strategy_config.min_btc_delta:
                state = f"ΔSMALL ({abs(delta):.3f}%<{self.strategy_config.min_btc_delta:.3f}%)"
            else:
                state = "IDLE"
            n = len(self._recent_window_deltas)
            vol_label = f"{self._compute_realized_vol():.3f}({'r' if n >= 6 else f'fb,n={n}'})"
            markov_stats = self._markov_filter.transition_stats(candidate_side)
            markov_label = (
                f"{markov_persistence:.2f}"
                f"({markov_stats['same']}/{markov_stats['total']},"
                f"dir={markov_stats['directional_samples']},"
                f"flat={markov_stats['flat_samples']})"
            )
            print(
                f"  ⏱  T-{seconds_remaining:5.1f}s | "
                f"BTC ${btc_price:,.2f} {d}{abs(delta):.3f}% | "
                f"UP ${up_price:.3f} DN ${down_price:.3f} | "
                f"Mkv {markov_label} | "
                f"vol={vol_label} | P&L ${self.stats.total_pnl:+.2f} [{state}]"
            )

    # ── Active position management ──────────────────────────────────

    # ── Position monitoring (hold to resolution) ────────────────────

    def _manage_position(self, btc_price: float, seconds_remaining: float, now: float):
        """Monitor only — all trades hold to resolution. No stops.
        Tracker logs hold-period stats for future optimization.
        """
        if self._opening_price <= 0:
            return

        btc_delta_pct = ((btc_price - self._opening_price) / self._opening_price) * 100
        chainlink_price = self.rtds_feed.get_latest()
        chainlink_delta_pct = (
            ((chainlink_price.price - self._chainlink_open_price) / self._chainlink_open_price) * 100
            if chainlink_price and self._chainlink_open_price > 0
            else None
        )
        # Held-side probability uses the same de-biased Binance move as entry.
        model_delta_pct = btc_delta_pct
        our_prob = probability_for_held_side(
            model_delta_pct,
            seconds_remaining,
            self._trade_side,
            vol=self._compute_realized_vol(),
        )

        # Throttled check
        if now - self._last_position_check < POSITION_CHECK_INTERVAL:
            if now - self._last_status_print >= 30:
                self._last_status_print = now
                d = "↑" if btc_delta_pct > 0 else "↓" if btc_delta_pct < 0 else "→"
                ref_label = (
                    f"CL {chainlink_delta_pct:+.3f}%/{chainlink_price.age_seconds:.1f}s"
                    if chainlink_delta_pct is not None and chainlink_price
                    else "CL n/a"
                )
                print(
                    f"  ⏱  T-{seconds_remaining:5.1f}s | "
                    f"BN {d}{abs(btc_delta_pct):.3f}% | {ref_label} | "
                    f"Prob: {our_prob:.2f} | "
                    f"P&L ${self.stats.total_pnl:+.2f} [HOLDING→RES]"
                )
            return

        self._last_position_check = now

        # Get current sell price (for tracking only)
        if self.dry_run:
            current_sell_price = round(max(our_prob, 0.01), 2)
        else:
            sell_probe = round(self._trade_shares * self._trade_price, 2)
            current_sell_price = self.executor.get_market_price(
                self._trade_token_id, "SELL", max(sell_probe, 1.0)
            )

        if current_sell_price <= 0:
            return

        self._last_sell_price_seen = current_sell_price

        # Track hold-period extremes
        self.tracker.update_hold_stats(our_prob, current_sell_price)

        current_value = self._trade_shares * current_sell_price
        unrealized_pnl = current_value - self._trade_cost
        return_pct = (current_sell_price - self._trade_price) / self._trade_price if self._trade_price > 0 else 0

        # Hold-to-resolution policy: no stop-loss/prob-stop exits here. Sell
        # price is observed only for diagnostics and post-session calibration.

        # Status line (monitoring only — no exits)
        d = "↑" if btc_delta_pct > 0 else "↓" if btc_delta_pct < 0 else "→"
        ref_label = (
            f"CL {chainlink_delta_pct:+.3f}%/{chainlink_price.age_seconds:.1f}s"
            if chainlink_delta_pct is not None and chainlink_price
            else "CL n/a"
        )
        pnl_emoji = "📈" if unrealized_pnl > 0 else "📉"
        print(
            f"  {pnl_emoji} T-{seconds_remaining:5.1f}s | "
            f"BN {d}{abs(btc_delta_pct):.3f}% | {ref_label} | "
            f"Prob: {our_prob:.2f} | "
            f"Sell: ${current_sell_price:.3f} | "
            f"PnL: ${unrealized_pnl:+.2f} ({return_pct:+.0%})"
        )

    # ── Window management ───────────────────────────────────────────

    def _on_new_window(self, window_ts: int, closing_btc_price: float = 0.0):
        if self._current_window > 0:
            # Resolve any pending phantom sell from the previous window.
            # Must run before trade state is reset below.
            # Balance is fetched once here and reused by the sync below.
            if self._pending_phantom:
                pp = self._pending_phantom
                if not self.dry_run and self.executor._initialized:
                    real_bal = self.executor.get_collateral_balance()
                    if real_bal > 0:
                        balance_increase = max(0.0, real_bal - pp["pre_sell_balance"])
                        if balance_increase > pp["expected_revenue"] * 0.50:
                            # Settlement landed — it was a real win
                            profit = balance_increase - pp["cost"]
                            self.stats.record_win(profit)
                            self.stats.bankroll = real_bal
                            self._last_real_balance = real_bal
                            print(f"  ✅ Phantom resolved: WIN +${profit:.2f} [phantom_resolved] | "
                                  f"P&L: ${self.stats.total_pnl:+.2f} | Bank: ${self.stats.bankroll:.2f}")
                            self.telegram.win_alert(profit, self.stats.total_pnl)
                            official = self._fetch_official_window_price(pp["window_ts"], retries=2, delay=0.5)
                            official_open = float(official.open_price or 0.0) if official else 0.0
                            official_close = float(official.close_price or 0.0) if official else 0.0
                            final_price = official_close if official_close > 0 else 0.0
                            final_open = official_open if official_open > 0 else pp["opening_price"]
                            self.tracker.log_trade_resolve(
                                btc_final_price=final_price,
                                opening_price=final_open,
                                won=True,
                                profit=profit,
                                exit_revenue=pp["exit_revenue"],
                                resolution_method="phantom_resolved",
                                claim_result="phantom_resolved",
                                final_price_source="polymarket_crypto_price" if official_close > 0 else "unknown",
                                official_open_price=official_open,
                                official_close_price=official_close,
                                official_completed=bool(official.completed) if official else False,
                            )
                        else:
                            # Balance still hasn't moved — genuine loss
                            net_loss = pp["cost"] - pp["exit_revenue"]
                            self.stats.record_loss(net_loss)
                            self.stats.bankroll = real_bal
                            self._last_real_balance = real_bal
                            print(f"  ❌ Phantom confirmed: LOSS -${net_loss:.2f} [phantom_confirmed] | "
                                  f"P&L: ${self.stats.total_pnl:+.2f} | Bank: ${self.stats.bankroll:.2f}")
                            self.telegram.loss_alert(net_loss, self.stats.total_pnl)
                            official = self._fetch_official_window_price(pp["window_ts"], retries=2, delay=0.5)
                            official_open = float(official.open_price or 0.0) if official else 0.0
                            official_close = float(official.close_price or 0.0) if official else 0.0
                            final_price = official_close if official_close > 0 else 0.0
                            final_open = official_open if official_open > 0 else pp["opening_price"]
                            self.tracker.log_trade_resolve(
                                btc_final_price=final_price,
                                opening_price=final_open,
                                won=False,
                                profit=-net_loss,
                                exit_revenue=pp["exit_revenue"],
                                resolution_method="phantom_confirmed",
                                claim_result="phantom_confirmed",
                                final_price_source="polymarket_crypto_price" if official_close > 0 else "unknown",
                                official_open_price=official_open,
                                official_close_price=official_close,
                                official_completed=bool(official.completed) if official else False,
                            )
                        self._pending_phantom = {}
                else:
                    # Dry run or executor not ready — treat as loss
                    net_loss = pp["cost"] - pp["exit_revenue"]
                    self.stats.record_loss(net_loss)
                    self._pending_phantom = {}

            # Record closing delta for rolling vol calculation
            if self._opening_price > 0 and closing_btc_price > 0:
                closing_delta = abs((closing_btc_price - self._opening_price) / self._opening_price * 100)
                self._recent_window_deltas.append(closing_delta)
                if len(self._recent_window_deltas) > self._rolling_vol_windows:
                    self._recent_window_deltas.pop(0)
            # Detect pending buy that settled after our verification timeout
            if self._pending_buy_side and not self._traded:
                if not self.dry_run and self.executor._initialized:
                    real_bal = self.executor.get_collateral_balance()
                    if real_bal > 0 and self._balance_before_buy > 0:
                        spent = self._balance_before_buy - real_bal
                        if spent > 1.0:
                            # The buy DID go through — retroactively track it
                            est_shares = spent / self._pending_buy_price if self._pending_buy_price > 0 else 0
                            print(f"\n  👻 LATE FILL: balance dropped ${spent:.2f} since buy attempt")
                            print(f"     Retroactively tracking: ~{est_shares:.0f} shares "
                                  f"{self._pending_buy_side} @ ${self._pending_buy_price:.3f}")

                            self._traded = True
                            self._trade_side = self._pending_buy_side
                            self._trade_price = self._pending_buy_price
                            self._trade_cost = spent
                            self._trade_shares = est_shares
                            self._trade_token_id = self._pending_buy_token_id
                            self.stats.bankroll = real_bal
                            self._last_real_balance = real_bal
                            self.stats.hourly.record_trade(
                                self._pending_buy_edge, self._pending_buy_delta)

            self.stats.hourly.record_window(self._traded)
            if self._traded:
                self._resolve_previous_trade()
            elif not self._trade_attempted and self._last_tick_context:
                # Log the no-trade signal for this window using last tick state
                ctx = self._last_tick_context
                skip_reason = get_skip_reason(
                    btc_price=ctx["btc_price"],
                    opening_price=self._opening_price,
                    up_market_price=ctx["up_price"],
                    down_market_price=ctx["down_price"],
                    seconds_remaining=ctx["seconds_remaining"],
                    config=self.strategy_config,
                    realized_vol=self._compute_realized_vol(),
                    markov_persistence=ctx.get("markov_persistence", 1.0),
                    fee_rate_bps=ctx.get("fee_rate_bps", 0.0),
                )
                sig = ctx.get("signal")
                self.tracker.log_signal(
                    window_ts=ctx["window_ts"],
                    btc_price=ctx["btc_price"],
                    opening_price=self._opening_price,
                    up_price=ctx["up_price"],
                    down_price=ctx["down_price"],
                    seconds_remaining=ctx["seconds_remaining"],
                    side=sig.side if sig else "",
                    true_prob=sig.true_prob if sig else 0.0,
                    market_price=sig.market_price if sig else 0.0,
                    edge=sig.edge if sig else 0.0,
                    kelly_size=sig.kelly_size if sig else 0.0,
                    markov_persistence=sig.markov_persistence if sig else ctx.get("markov_persistence", 0.0),
                    fee_rate_bps=sig.fee_rate_bps if sig else ctx.get("fee_rate_bps", 0.0),
                    fee_adjusted_edge=sig.fee_adjusted_edge if sig else 0.0,
                    action="no_signal",
                    skip_reason=skip_reason,
                )
                self._log_completed_dry_run_window(
                    won="", simulated_profit=0.0, final_price=closing_btc_price
                )

            # Sync real balance at window boundary (catches any drift)
            if not self.dry_run and self.executor._initialized:
                real_bal = self.executor.get_collateral_balance()
                if real_bal > 0:
                    drift = abs(real_bal - self.stats.bankroll)
                    if drift > 0.50:
                        print(f"  🔄 Balance sync: ${self.stats.bankroll:.2f} → "
                              f"${real_bal:.2f} (drift ${drift:.2f})")
                    self.stats.bankroll = real_bal
                    self._last_real_balance = real_bal

        self._current_window = window_ts
        # Binance window-open = the boundary tick (price at the instant the window
        # flipped). This is the de-biased anchor for all Binance-delta signals.
        self._opening_price = float(closing_btc_price) if closing_btc_price and closing_btc_price > 0 else 0.0
        self._chainlink_open_price = 0.0
        self._traded = False
        self._trade_attempted = False
        self._exit_revenue = 0.0
        self._last_position_check = 0.0
        self._last_status_print = 0.0
        self._last_sell_price_seen = 0.0
        self._cached_up = 0.50
        self._cached_down = 0.50

        self._cached_up_fee_bps = 0.0
        self._cached_down_fee_bps = 0.0
        self._price_last_fetched = 0.0
        self._pending_buy_side = ""
        self._pending_buy_price = 0.0
        self._pending_buy_amount = 0.0
        self._pending_buy_shares = 0.0
        self._pending_buy_token_id = ""
        self._pending_buy_edge = 0.0
        self._pending_buy_delta = 0.0
        self._balance_before_buy = 0.0
        self._window_signals_detected = 0
        self._entry_markov_state = ""
        self._entry_markov_persistence = 0.0
        self._entry_markov_threshold = self.strategy_config.markov_persistence_threshold
        self._entry_markov_passed = False
        self._window_open_price_missing = False

        market = self._wait_for_official_open_price(window_ts)
        self._current_market = market
        if market:
            self._refresh_market_metadata_and_orderbook(market)
        if market and market.opening_price:
            self._chainlink_open_price = market.opening_price
            print(f"  📌 Polymarket/Chainlink openPrice: ${self._chainlink_open_price:,.2f}")
        else:
            self._chainlink_open_price = 0.0
            self._window_open_price_missing = True
            if self.dry_run:
                print("  ⚠️  Polymarket openPrice unavailable after retry — dry-run Chainlink source-consensus checks degraded")
            else:
                self._trade_attempted = True
                print("  ⚠️  Polymarket openPrice unavailable after retry — skipping LIVE trading for this window")

        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {self.stats.total_trades} | "
              f"W/L: {self.stats.wins}/{self.stats.losses} | "
              f"P&L: ${self.stats.total_pnl:+.2f}")
        print(f"{'─' * 55}")

        # Circuit breaker auto-recovery: ping CLOB each new window
        if self._clob_halted and not self.dry_run and self.executor._initialized:
            try:
                self.executor.client.get_ok()
                self._clob_halted = False
                self._consecutive_buy_failures = 0
                print(f"  ✅ CLOB recovered (health check OK) — resuming trades")
            except Exception:
                print(f"  🔌 CLOB health check still failing — staying halted")

    def _wait_for_official_open_price(self, window_ts: int):
        """Poll briefly for Polymarket's official fiveminute openPrice.

        The crypto-price endpoint can lag the exact 5-minute boundary by several
        seconds. Live trading should not immediately substitute Binance's first
        local tick for the official open price.
        """
        target_first_query_at = float(window_ts) + max(0.0, self._open_price_initial_delay)
        now = time.time()
        if now < target_first_query_at:
            time.sleep(target_first_query_at - now)

        deadline = float(window_ts) + max(
            self._open_price_initial_delay,
            self._open_price_wait_seconds,
        )
        attempt = 0
        last_market = None

        while True:
            attempt += 1
            last_market = get_current_market(self.period)
            if last_market and last_market.opening_price:
                if attempt > 1:
                    print(f"  ✅ Polymarket openPrice available after {attempt} attempts")
                return last_market

            now = time.time()
            if now >= deadline:
                return last_market

            wait = max(0.1, min(self._open_price_retry_interval, deadline - now))
            time.sleep(wait)

    def _fetch_official_window_price(self, window_ts: int, retries: int = 3, delay: float = 1.0):
        """Fetch official Polymarket/Chainlink open/close for a resolved window."""
        last = None
        for attempt in range(max(1, retries)):
            last = fetch_crypto_window_price("BTC", window_ts)
            if last and last.open_price and last.close_price and last.completed:
                return last
            if attempt < retries - 1:
                time.sleep(delay)
        return last

    @staticmethod
    def _official_winning_side(official) -> str:
        if not official or not official.open_price or not official.close_price or not official.completed:
            return ""
        return "UP" if official.close_price >= official.open_price else "DOWN"

    # ── Market prices (cached, complement engine) ───────────────────

    def _refresh_market_metadata_and_orderbook(self, market) -> None:
        if not market or self.dry_run or not self.executor._initialized:
            return
        try:
            metadata = self.executor.get_market_metadata(market.condition_id)
            self._market_min_order_size = metadata.minimum_order_size
            self._market_tick_size = metadata.minimum_tick_size
            self._cached_up_fee_bps = metadata.fee_rate_bps
            self._cached_down_fee_bps = metadata.fee_rate_bps
            if self._orderbook_cache_enabled:
                self.orderbook_cache.subscribe([market.token_id_up, market.token_id_down])
                self.orderbook_cache.start()
                print(
                    f"  📡 CLOB orderbook cache subscribed "
                    f"(UP/DOWN, max_age={self._orderbook_max_age:.1f}s)"
                )
        except Exception as e:
            print(f"[market] Metadata/orderbook setup failed: {sanitize_exception_text(e)}")

    def _compute_realized_vol(self) -> float:
        """Rolling std dev of recent window closing deltas.

        Returns the realized vol to pass into the Brownian motion model.
        Falls back to 0.12 until at least 6 windows have accumulated.
        Floored/capped to prevent extreme values breaking the model.
        """
        min_samples = max(6, self._rolling_vol_windows // 2)
        if len(self._recent_window_deltas) >= min_samples:
            vol = statistics.stdev(self._recent_window_deltas)
            return max(self._vol_floor, min(self._vol_cap, vol))
        return self._vol_fallback

    def _get_chainlink_price_for_window(self):
        """Return latest Polymarket/Chainlink, with REST fallback.

        The RTDS stream is the preferred live source. REST price-history is a
        coarse fallback so source-consensus decisions do not silently degrade to
        Binance-only logic.
        """
        ref = self.rtds_feed.get_latest()
        if ref and ref.age_seconds <= self._source_consensus_config.stale_downsize_seconds:
            return ref
        if self._current_window > 0:
            period_secs = PERIOD_SECONDS[self.period]
            rest_chainlink = self.rtds_feed.update_from_rest(
                self._current_window, self._current_window + period_secs
            )
            if rest_chainlink:
                return rest_chainlink
        return ref

    def _log_source_skip(self, sig, seconds_remaining: float, decision, action: str):
        btc_approx = decision.signal_price or (
            self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
        )
        self.tracker.log_signal(
            window_ts=self._current_window,
            btc_price=btc_approx,
            opening_price=self._opening_price,
            up_price=self._cached_up,
            down_price=self._cached_down,
            seconds_remaining=seconds_remaining,
            side=sig.side,
            true_prob=sig.true_prob,
            market_price=sig.market_price,
            edge=sig.edge,
            kelly_size=sig.kelly_size,
            markov_persistence=sig.markov_persistence,
            fee_rate_bps=sig.fee_rate_bps,
            fee_adjusted_edge=sig.fee_adjusted_edge,
            action=action,
            skip_reason=decision.reason,
            actual_price=sig.market_price,
            actual_edge=sig.edge,
        )

    def _get_market_prices(self, btc_price: float, seconds_remaining: float) -> tuple:
        if self.dry_run or not self.executor._initialized:
            if self._opening_price <= 0:
                return 0.50, 0.50
            delta_pct = (btc_price - self._opening_price) / self._opening_price
            time_factor = 1 - (seconds_remaining / PERIOD_SECONDS[self.period])
            lag_factor = min(time_factor * 0.7, 0.85)
            implied = 0.5 + lag_factor * math.tanh(delta_pct * 500) * 0.45
            up = round(min(max(implied, 0.02), 0.98), 3)
            self._cached_up_fee_bps = 0.0
            self._cached_down_fee_bps = 0.0
            return up, round(1.0 - up, 3)

        now = time.time()
        market = self._current_market
        try:
            if not market:
                market = get_current_market(self.period, include_open_price=False)
                self._current_market = market
                if market:
                    self._refresh_market_metadata_and_orderbook(market)
            if not market:
                return self._cached_up, self._cached_down

            probe_amount = max(self._market_min_order_size * 0.50, 1.0)
            up_price = 0.0
            down_price = 0.0
            if self._orderbook_cache_enabled:
                up_price = self.orderbook_cache.get_market_price(market.token_id_up, "BUY", probe_amount)
                down_price = self.orderbook_cache.get_market_price(market.token_id_down, "BUY", probe_amount)

            if up_price <= 0 or down_price <= 0:
                # Fallback only for display/diagnostics when the websocket cache is
                # cold. The trade hot path below will not block on this REST path.
                if now - self._price_last_fetched < self._PRICE_REFRESH:
                    return self._cached_up, self._cached_down
                up_price = up_price or self.executor.get_market_price(market.token_id_up, "BUY", probe_amount)
                down_price = down_price or self.executor.get_market_price(market.token_id_down, "BUY", probe_amount)

            if up_price <= 0 and down_price <= 0:
                return self._cached_up, self._cached_down
            if up_price <= 0:
                up_price = round(1.0 - down_price, 3)
            if down_price <= 0:
                down_price = round(1.0 - up_price, 3)

            self._cached_up = up_price
            self._cached_down = down_price
            self._price_last_fetched = now

            return up_price, down_price

        except Exception as e:
            print(f"[price] Error: {sanitize_exception_text(e)}")
            return self._cached_up, self._cached_down

    # ── Entry ───────────────────────────────────────────────────────

    def _execute_trade(self, sig, seconds_remaining: float):
        self._trade_attempted = True

        # ── Circuit breaker: CLOB health check ───────────────────
        if self._clob_halted:
            print(f"  🔌 CLOB HALTED — skipping trade ({self._consecutive_buy_failures} consecutive failures)")
            return

        if not self.dry_run and self.executor._initialized:
            try:
                self.executor.client.get_ok()
            except Exception as e:
                self._consecutive_buy_failures += 1
                print(f"  🔌 CLOB health check failed: {e}")
                if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                    self._clob_halted = True
                    msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                           f"consecutive health check failures — stopping trades until recovery")
                    print(f"\n  {msg}")
                    self.telegram.status_update({"alert": msg})
                return

        # ── Daily loss limit ─────────────────────────────────────
        if self._daily_loss_halted:
            print(f"  🛑 DAILY LOSS LIMIT — session P&L ${self.stats.total_pnl:+.2f} "
                  f"exceeds -${self._daily_loss_limit:.0f}")
            return

        session_pnl = self.stats.bankroll - self._session_start_balance
        if session_pnl <= -self._daily_loss_limit:
            self._daily_loss_halted = True
            msg = (f"🛑 DAILY LOSS LIMIT HIT: ${session_pnl:+.2f} "
                   f"(limit -${self._daily_loss_limit:.0f}) — stopping trades")
            print(f"\n  {msg}")
            self.telegram.status_update({"alert": msg})
            return

        market = self._current_market if not self.dry_run else None
        if not market and not self.dry_run:
            try:
                market = get_current_market(self.period, include_open_price=False)
            except TypeError:
                market = get_current_market(self.period)
            self._current_market = market
            if market:
                self._refresh_market_metadata_and_orderbook(market)
        token_id = ""
        if market:
            token_id = market.token_id_up if sig.side == "UP" else market.token_id_down
        else:
            token_id = f"DRY-{sig.side}-{self._current_window}"

        slug = f"btc-updown-{self.period}m-{self._current_window}"
        trade_amount = round(sig.kelly_size, 2)

        print(f"\n  🎯 {sig.side} | Δ={sig.gap:.3f} | fee-edge={sig.fee_adjusted_edge:.3f} | "
              f"req={sig.edge_required:.3f} | prob={sig.true_prob:.2f} | "
              f"p(j*,j*)={sig.markov_persistence:.2f} [{sig.markov_regime}] | BTC Δ={sig.btc_delta_pct:+.3f}%")
        print(f"     Kelly: ${trade_amount:.2f} | mkt ${sig.market_price:.3f} | fee {sig.fee_rate_bps:.1f}bps | T-{seconds_remaining:.0f}s")

        source_decision = self.source_consensus.assess_snapshot(sig.side)
        if source_decision.reason == "source_snapshot_missing":
            latest_btc, latest_fresh = self.price_feed.get_price()
            latest_chainlink = self.rtds_feed.get_latest()
            get_rtds_binance = getattr(self.rtds_feed, "get_binance_latest", None)
            latest_rtds_binance = get_rtds_binance() if callable(get_rtds_binance) else None
            if latest_fresh and latest_btc > 0 and self._opening_price > 0:
                source_decision = self.source_consensus.update_snapshot(
                    binance_price=latest_btc,
                    opening_price=self._chainlink_open_price,
                    intended_side=sig.side,
                    chainlink=latest_chainlink,
                    rtds_binance=latest_rtds_binance,
                    binance_opening_price=self._opening_price,
                )
        if source_decision.should_skip:
            print(
                f"  ⚠️  Source risk gate before entry — skipping "
                f"({source_decision.reason}; rtds_gap="
                f"{source_decision.source_gap_bps if source_decision.source_gap_bps is not None else 0:+.1f}bps, "
                f"direct_vs_rtds="
                f"{source_decision.direct_vs_rtds_binance_gap_bps if source_decision.direct_vs_rtds_binance_gap_bps is not None else 0:.1f}bps, "
                f"cl_age={source_decision.chainlink_age_seconds if source_decision.chainlink_age_seconds is not None else -1:.1f}s)"
            )
            self._log_source_skip(sig, seconds_remaining, source_decision, "skipped_source_disagreement")
            return
        if source_decision.action == "downsize":
            old_amount = trade_amount
            trade_amount = round(max(
                self.strategy_config.min_bet,
                trade_amount * source_decision.size_multiplier,
            ), 2)
            sig.kelly_size = trade_amount
            print(
                f"  ⚠️  Source risk downsize snapshot ({source_decision.reason}): "
                f"${old_amount:.2f} → ${trade_amount:.2f}"
            )

        if self.dry_run:
            required_fee_edge = max(sig.edge_required, self._huge_edge_min)
            if sig.fee_adjusted_edge < required_fee_edge:
                print(
                    f"  ⚠️  Dry-run signal not huge enough for FAK policy — skipping "
                    f"(fee-edge {sig.fee_adjusted_edge:.3f} < {required_fee_edge:.3f})"
                )
                self.tracker.log_signal(
                    window_ts=self._current_window,
                    btc_price=self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0,
                    opening_price=self._opening_price,
                    up_price=self._cached_up,
                    down_price=self._cached_down,
                    seconds_remaining=seconds_remaining,
                    side=sig.side,
                    true_prob=sig.true_prob,
                    market_price=sig.market_price,
                    edge=sig.edge,
                    kelly_size=sig.kelly_size,
                    markov_persistence=sig.markov_persistence,
                    fee_rate_bps=sig.fee_rate_bps,
                    fee_adjusted_edge=sig.fee_adjusted_edge,
                    action="skipped_not_huge_edge",
                    skip_reason="fee_adjusted_edge_below_huge_taker_threshold",
                    actual_price=sig.market_price,
                    actual_edge=sig.edge,
                )
                return

        # Huge-edge-only execution: this strategy captures oracle/repricing lag.
        # If the fee-adjusted edge is not large enough to justify immediate
        # taker execution, skip. Post-only/GTD maker entry was removed because it
        # conflicts with the latency edge and creates adverse-selection fills.
        hint_price = sig.market_price if self.dry_run else 0.0
        depth_snapshot = None
        if not self.dry_run and self.executor._initialized:
            actual_price = 0.0
            if self._orderbook_cache_enabled:
                actual_price = self.orderbook_cache.get_market_price(token_id, "BUY", trade_amount)
            if actual_price <= 0:
                actual_price = self.executor.get_market_price(token_id, "BUY", trade_amount)
            if actual_price <= 0:
                print("  ⚠️  No executable CLOB ask before FAK entry — skipping")
                self.tracker.log_signal(
                    window_ts=self._current_window,
                    btc_price=self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0,
                    opening_price=self._opening_price,
                    up_price=self._cached_up,
                    down_price=self._cached_down,
                    seconds_remaining=seconds_remaining,
                    side=sig.side,
                    true_prob=sig.true_prob,
                    market_price=sig.market_price,
                    edge=sig.edge,
                    kelly_size=sig.kelly_size,
                    markov_persistence=sig.markov_persistence,
                    fee_rate_bps=sig.fee_rate_bps,
                    fee_adjusted_edge=sig.fee_adjusted_edge,
                    action="skipped_orderbook_stale",
                    skip_reason="clob_fak_price_unavailable",
                    actual_price=0.0,
                    actual_edge=0.0,
                )
                return

            actual_edge = sig.true_prob - actual_price
            required_fee_edge = max(sig.edge_required, self._huge_edge_min)
            price_cap = choose_fak_price_cap(
                true_prob=sig.true_prob,
                executable_price=actual_price,
                fee_rate_bps=sig.fee_rate_bps,
                required_fee_edge=required_fee_edge,
                tick_size=self._market_tick_size,
                slippage_ticks=self._fak_slippage_ticks,
            )
            actual_fee_edge = fee_adjusted_edge(sig.true_prob, price_cap, sig.fee_rate_bps)
            print(
                f"  📊 FAK ask: ${actual_price:.3f} | cap ${price_cap:.3f} "
                f"(edge@cap: {sig.true_prob - price_cap:.3f}, fee-edge@cap: {actual_fee_edge:.3f}, "
                f"threshold: {required_fee_edge:.3f})"
            )
            if actual_fee_edge < required_fee_edge:
                print("  ⚠️  Edge is not huge enough for taker execution — skipping")
                btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
                self.tracker.log_signal(
                    window_ts=self._current_window,
                    btc_price=btc_approx,
                    opening_price=self._opening_price,
                    up_price=self._cached_up,
                    down_price=self._cached_down,
                    seconds_remaining=seconds_remaining,
                    side=sig.side,
                    true_prob=sig.true_prob,
                    market_price=actual_price,
                    edge=actual_edge,
                    kelly_size=sig.kelly_size,
                    markov_persistence=sig.markov_persistence,
                    fee_rate_bps=sig.fee_rate_bps,
                    fee_adjusted_edge=actual_fee_edge,
                    action="skipped_not_huge_edge",
                    skip_reason="fee_adjusted_edge_below_huge_taker_threshold",
                    actual_price=actual_price,
                    actual_edge=actual_edge,
                )
                return

            # Ensure budget buys at least the market's minimum share size.
            min_live_amount = round(float(self._market_min_order_size) * price_cap, 2)
            if trade_amount < min_live_amount:
                if min_live_amount <= self.strategy_config.max_bet:
                    print(
                        f"  ℹ️  Raising FAK amount to CLOB minimum size: "
                        f"${trade_amount:.2f} → ${min_live_amount:.2f} "
                        f"({self._market_min_order_size:.0f} shares @ cap ${price_cap:.3f})"
                    )
                    trade_amount = min_live_amount
                    sig.kelly_size = trade_amount
                else:
                    print(
                        f"  ⚠️  CLOB minimum size too large — skipping "
                        f"(${min_live_amount:.2f} required > max ${self.strategy_config.max_bet:.2f})"
                    )
                    btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
                    self.tracker.log_signal(
                        window_ts=self._current_window,
                        btc_price=btc_approx,
                        opening_price=self._opening_price,
                        up_price=self._cached_up,
                        down_price=self._cached_down,
                        seconds_remaining=seconds_remaining,
                        side=sig.side,
                        true_prob=sig.true_prob,
                        market_price=actual_price,
                        edge=actual_edge,
                        kelly_size=trade_amount,
                        markov_persistence=sig.markov_persistence,
                        fee_rate_bps=sig.fee_rate_bps,
                        fee_adjusted_edge=actual_fee_edge,
                        action="skipped_size_too_small",
                        skip_reason="clob_min_size_exceeds_max_bet",
                        actual_price=actual_price,
                        actual_edge=actual_edge,
                    )
                    return

            planned_shares, _planned_spend = calculate_order_size(price_cap, trade_amount)
            if self._orderbook_cache_enabled and planned_shares > 0:
                depth_snapshot = self.orderbook_cache.get_buy_depth_snapshot(
                    token_id,
                    required_shares=planned_shares,
                    cap_price=price_cap,
                )
                print(
                    f"  📚 Ask depth≤cap: {depth_snapshot.cumulative_shares:.0f}/"
                    f"{planned_shares:.0f} shares | best ask ${depth_snapshot.best_ask:.3f} "
                    f"| worst ${depth_snapshot.worst_price:.3f} | age {depth_snapshot.book_age_ms:.0f}ms"
                )
                if not depth_snapshot.enough:
                    btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
                    self.tracker.log_signal(
                        window_ts=self._current_window,
                        btc_price=btc_approx,
                        opening_price=self._opening_price,
                        up_price=self._cached_up,
                        down_price=self._cached_down,
                        seconds_remaining=seconds_remaining,
                        side=sig.side,
                        true_prob=sig.true_prob,
                        market_price=price_cap,
                        edge=sig.true_prob - price_cap,
                        kelly_size=trade_amount,
                        markov_persistence=sig.markov_persistence,
                        fee_rate_bps=sig.fee_rate_bps,
                        fee_adjusted_edge=actual_fee_edge,
                        action="skipped_liquidity_gone",
                        skip_reason="insufficient_depth_at_fak_cap",
                        actual_price=price_cap,
                        actual_edge=sig.true_prob - price_cap,
                        book_best_bid=depth_snapshot.best_bid,
                        book_best_ask=depth_snapshot.best_ask,
                        book_worst_ask_at_cap=depth_snapshot.worst_price,
                        book_cap_price=depth_snapshot.cap_price,
                        book_depth_shares_at_cap=depth_snapshot.cumulative_shares,
                        book_required_shares=depth_snapshot.required_shares,
                        book_depth_usd_at_cap=depth_snapshot.cumulative_usd,
                        book_age_ms=depth_snapshot.book_age_ms,
                        book_depth_enough=depth_snapshot.enough,
                    )
                    print("  ⚠️  Liquidity gone before FAK entry — skipping")
                    return

            latest_btc, latest_fresh = self.price_feed.get_price()
            if latest_fresh and self._opening_price > 0 and latest_btc > 0:
                # Final hot-path recheck uses raw Binance only. Do not apply a
                # Source risk is handled by SourceConsensusGate against RTDS Binance/Chainlink directly.
                latest_delta_pct = (latest_btc - self._opening_price) / self._opening_price * 100
                latest_side = "UP" if latest_btc >= self._opening_price else "DOWN"
                recheck_vol = self._compute_realized_vol()
                latest_our_prob = probability_for_held_side(
                    latest_delta_pct,
                    seconds_remaining,
                    sig.side,
                    vol=recheck_vol,
                )
                if latest_side != sig.side or latest_our_prob < self.strategy_config.min_prob:
                    if latest_side != sig.side:
                        action = "skipped_btc_reversed"
                        skip_reason = "btc_reversed_before_entry"
                        message = "BTC reversed before entry"
                    else:
                        action = "skipped_prob_below_min"
                        skip_reason = "prob_below_min_at_final_recheck"
                        message = "Final recheck probability fell below min before entry"
                    print(
                        f"  ⚠️  {message} — skipping "
                        f"(now {latest_side}, prob={latest_our_prob:.2f}, "
                        f"min={self.strategy_config.min_prob:.2f}, vol={recheck_vol:.4f}, "
                        f"Δ={latest_delta_pct:+.3f}%)"
                    )
                    self.tracker.log_signal(
                        window_ts=self._current_window,
                        btc_price=latest_btc,
                        opening_price=self._opening_price,
                        up_price=self._cached_up,
                        down_price=self._cached_down,
                        seconds_remaining=seconds_remaining,
                        side=sig.side,
                        true_prob=latest_our_prob,
                        market_price=price_cap,
                        edge=latest_our_prob - price_cap,
                        kelly_size=sig.kelly_size,
                        markov_persistence=sig.markov_persistence,
                        fee_rate_bps=sig.fee_rate_bps,
                        fee_adjusted_edge=fee_adjusted_edge(latest_our_prob, price_cap, sig.fee_rate_bps),
                        action=action,
                        skip_reason=skip_reason,
                        actual_price=price_cap,
                        actual_edge=latest_our_prob - price_cap,
                    )
                    return

            exit_probe = max(trade_amount, self._market_min_order_size, 1.0)
            current_sell_price = (
                self.orderbook_cache.get_market_price(token_id, "SELL", exit_probe)
                if self._orderbook_cache_enabled
                else self.executor.get_market_price(token_id, "SELL", exit_probe)
            )
            if current_sell_price > 0:
                spread = price_cap - current_sell_price
                if spread > self._entry_max_spread or current_sell_price < self._entry_min_exit_price:
                    print(
                        f"  ⚠️  Reverse/thin orderbook — skipping "
                        f"(buy cap ${price_cap:.3f}, sell ${current_sell_price:.3f}, spread {spread:.3f})"
                    )
                    btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
                    self.tracker.log_signal(
                        window_ts=self._current_window,
                        btc_price=btc_approx,
                        opening_price=self._opening_price,
                        up_price=self._cached_up,
                        down_price=self._cached_down,
                        seconds_remaining=seconds_remaining,
                        side=sig.side,
                        true_prob=sig.true_prob,
                        market_price=price_cap,
                        edge=sig.true_prob - price_cap,
                        kelly_size=sig.kelly_size,
                        markov_persistence=sig.markov_persistence,
                        fee_rate_bps=sig.fee_rate_bps,
                        fee_adjusted_edge=actual_fee_edge,
                        action="skipped_reverse_orderbook",
                        skip_reason="reverse_orderbook_before_entry",
                        actual_price=price_cap,
                        actual_edge=sig.true_prob - price_cap,
                    )
                    return

            hint_price = price_cap

        result = self.executor.buy(token_id=token_id, amount_usd=trade_amount, price=hint_price)

        if result.success:
            self._consecutive_buy_failures = 0  # Reset circuit breaker
            self._traded = True
            self._trade_side = sig.side
            self._trade_price = result.price
            self._trade_cost = result.amount_usd
            self._trade_shares = result.shares
            self._trade_token_id = token_id
            self._entry_markov_persistence = sig.markov_persistence
            self._entry_markov_threshold = self.strategy_config.markov_persistence_threshold
            self._entry_markov_passed = sig.markov_persistence >= self._entry_markov_threshold
            self._entry_markov_state = self._markov_state_label(
                sig.side, sig.markov_persistence, self._entry_markov_threshold
            )

            self.stats.bankroll -= result.amount_usd
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)

            btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
            self.tracker.log_signal(
                window_ts=self._current_window,
                btc_price=btc_approx,
                opening_price=self._opening_price,
                up_price=self._cached_up,
                down_price=self._cached_down,
                seconds_remaining=seconds_remaining,
                side=sig.side,
                true_prob=sig.true_prob,
                market_price=sig.market_price,
                edge=sig.edge,
                kelly_size=sig.kelly_size,
                markov_persistence=sig.markov_persistence,
                fee_rate_bps=sig.fee_rate_bps,
                fee_adjusted_edge=sig.fee_adjusted_edge,
                action="traded",
                actual_price=result.price,
                actual_edge=sig.true_prob - result.price,
                fill_price=result.price,
                book_best_bid=depth_snapshot.best_bid if depth_snapshot else 0.0,
                book_best_ask=depth_snapshot.best_ask if depth_snapshot else 0.0,
                book_worst_ask_at_cap=depth_snapshot.worst_price if depth_snapshot else 0.0,
                book_cap_price=depth_snapshot.cap_price if depth_snapshot else 0.0,
                book_depth_shares_at_cap=depth_snapshot.cumulative_shares if depth_snapshot else 0.0,
                book_required_shares=depth_snapshot.required_shares if depth_snapshot else 0.0,
                book_depth_usd_at_cap=depth_snapshot.cumulative_usd if depth_snapshot else 0.0,
                book_age_ms=depth_snapshot.book_age_ms if depth_snapshot else 0.0,
                book_depth_enough=depth_snapshot.enough if depth_snapshot else False,
            )
            self.tracker.log_trade_entry(
                window_ts=self._current_window,
                side=sig.side,
                entry_price=result.price,
                entry_shares=result.shares,
                entry_cost=result.amount_usd,
                edge=sig.edge,
                prob=sig.true_prob,
                btc_delta=sig.btc_delta_pct,
                seconds_remaining=seconds_remaining,
                entry_delta_pct=sig.btc_delta_pct,
                entry_seconds_remaining=seconds_remaining,
                mode="DRY" if self.dry_run else "LIVE",
            )

            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.shares:.0f} shares @ "
                  f"${result.price:.3f} = ${result.amount_usd:.2f}")
            print(f"     Exit policy: hold to resolution")

            self.telegram.trade_alert(
                side=sig.side, price=result.price, amount=result.amount_usd,
                market_slug=slug, dry_run=self.dry_run,
                edge=sig.edge, kelly_size=sig.kelly_size,
            )
        else:
            if result.error == "UNVERIFIED_BUY":
                # Order likely filled but Polygon hasn't settled.
                # Save details — window boundary sync will detect the fill.
                self._pending_buy_side = sig.side
                self._pending_buy_price = result.price
                self._pending_buy_amount = result.amount_usd
                self._pending_buy_shares = result.shares
                self._pending_buy_token_id = token_id
                self._pending_buy_edge = sig.edge
                self._pending_buy_delta = sig.btc_delta_pct
                self._balance_before_buy = self.stats.bankroll
                print(f"  ⏳ Buy sent but unverified — will detect via balance sync")
            else:
                print(f"  ❌ Buy failed: {result.error}")
                # Circuit breaker: track consecutive API failures
                err = str(result.error).lower()
                if "request exception" in err or "service not ready" in err or "status_code=none" in err:
                    self._consecutive_buy_failures += 1
                    if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                        self._clob_halted = True
                        msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                               f"consecutive API failures — stopping trades until restart")
                        print(f"\n  {msg}")
                        self.telegram.status_update({"alert": msg})

    # ── Resolve hold-to-resolution trade ────────────────────────────

    def _resolve_previous_trade(self):
        original_cost = self._trade_cost
        remaining_shares = self._trade_shares

        # ── Dry run: prefer official Polymarket/Chainlink close ───────
        if self.dry_run:
            official = self._fetch_official_window_price(self._current_window, retries=2, delay=0.5)
            official_side = self._official_winning_side(official)
            if official_side:
                won = official_side == self._trade_side
                self._record_resolution(
                    won=won,
                    original_cost=original_cost,
                    remaining_shares=remaining_shares,
                    resolution_method="dry_official_chainlink",
                    claim_revenue=0.0,
                    final_price_source="polymarket_crypto_price",
                    official=official,
                )
                return

            # Fallback only when official data is unavailable/incomplete. Keep the
            # method label explicit so calibration never confuses Binance with the
            # settlement oracle.
            btc_price, _ = self.price_feed.get_price()
            if self._opening_price <= 0 or btc_price <= 0:
                return
            won = (btc_price >= self._opening_price) == (self._trade_side == "UP")
            self._record_resolution(
                won=won,
                original_cost=original_cost,
                remaining_shares=remaining_shares,
                resolution_method="dry_binance_fallback_official_unavailable",
                claim_revenue=0.0,
                final_price_source="binance_fallback",
                official=official,
            )
            return

        # ── Live: attempt claim sell first — result is the truth ─────
        # Binance price and oracle can disagree when BTC is near the opening
        # price at resolution. The claim sell result is ground truth:
        #   - Sell succeeds at ~$0.99 → shares had value → won
        #   - "no match" or near-zero fill → shares worthless → lost
        won = None
        claim_revenue = 0.0
        claim_result = "not_attempted"
        resolution_method = "claim_sell"

        claim_notional = remaining_shares * 0.99
        live_token = (self._trade_token_id
                      and not self._trade_token_id.startswith("DRY-")
                      and self.executor._initialized)

        # Live resolution truth is claim/balance/official settlement evidence.
        # Do not classify a live loss from transient market price alone; Binance
        # and top-of-book prices are diagnostics, not final truth.

        pre_sell_balance = 0.0
        if live_token and claim_notional >= 5.0:
            print(f"  💰 Claiming: sell {remaining_shares:.0f} shares @ $0.99...")
            pre_sell_balance = self.executor.get_collateral_balance()
            claim = self.executor.sell(
                token_id=self._trade_token_id,
                shares=remaining_shares,
                price=0.99,
            )
            if claim.success and claim.amount_usd > remaining_shares * 0.50:
                # API says success — verify with balance check to catch phantom fills
                time.sleep(2)
                post_sell_balance = self.executor.get_collateral_balance()
                balance_increase = max(0.0, post_sell_balance - pre_sell_balance) if (
                    pre_sell_balance > 0 and post_sell_balance > 0
                ) else claim.amount_usd
                if balance_increase > remaining_shares * 0.99 * 0.50:
                    # Balance confirmed — real fill
                    won = True
                    claim_revenue = claim.amount_usd
                    claim_result = "filled"
                else:
                    # API said success but no collateral arrived yet — defer to next window
                    print(f"  ⏳ Possible phantom sell "
                          f"(api=${claim.amount_usd:.2f}, balance_increase=${balance_increase:.2f})"
                          f" — deferring to next window balance sync")
            elif "no match" in claim.error.lower() or (
                claim.success and claim.amount_usd < remaining_shares * 0.10
            ):
                # No buyers for these shares → shares worthless → definitive loss
                won = False
                claim_result = "no_match"
            elif "not enough balance" in claim.error.lower():
                # Tracked share count is slightly above on-chain balance (rounding).
                # Retry with one fewer share to clear the discrepancy.
                retry_shares = int(remaining_shares) - 1
                print(f"  🔄 Rounding fix: retrying claim with {retry_shares} shares...")
                if retry_shares > 0 and float(retry_shares) * 0.99 >= 5.0:
                    retry = self.executor.sell(
                        token_id=self._trade_token_id,
                        shares=float(retry_shares),
                        price=0.99,
                    )
                    if retry.success and retry.amount_usd > retry_shares * 0.50:
                        time.sleep(2)
                        post_bal = self.executor.get_collateral_balance()
                        balance_increase = max(0.0, post_bal - pre_sell_balance)
                        if balance_increase > float(retry_shares) * 0.99 * 0.50:
                            won = True
                            claim_revenue = retry.amount_usd
                            claim_result = "filled"
                        # else: retry succeeded but balance unconfirmed — fall to defer
                # else: retry failed or too small — fall to defer (won still None)
            # else: any other error — fall to defer (won still None)
        else:
            if live_token and claim_notional < 5.0:
                print(f"  💰 {remaining_shares:.0f} shares below $5 min — deferring to auto-resolution")

        # ── Deferred fallback ────────────────────────────────────────
        # The old balance check fired before auto-resolution settled on-chain.
        # Any unresolved case is now deferred to the next window boundary
        # (~5 min), where Polygon settlement is guaranteed to have landed.
        if won is None:
            if not live_token:
                print(
                    "  ⚠️  Cannot determine LIVE resolution without a live token/executor — "
                    "leaving trade unresolved for CSV/balance reconciliation"
                )
                return
            else:
                if pre_sell_balance <= 0:
                    pre_sell_balance = self.executor.get_collateral_balance()
                print(f"  ⏳ Resolution deferred to next window balance sync")
                self._pending_phantom = {
                    "pre_sell_balance": pre_sell_balance,
                    "expected_revenue": remaining_shares * 0.99,
                    "cost": original_cost,
                    "exit_revenue": self._exit_revenue,
                    "shares": remaining_shares,
                    "side": self._trade_side,
                    "token_id": self._trade_token_id,
                    "window_ts": self._current_window,
                    "opening_price": self._opening_price,
                }
                return

        if won is None:
            return

        self._record_resolution(
            won=won,
            original_cost=original_cost,
            remaining_shares=remaining_shares,
            resolution_method=resolution_method,
            claim_revenue=claim_revenue,
            claim_result=claim_result,
        )

    def _record_resolution(
        self, won: bool, original_cost: float, remaining_shares: float,
        resolution_method: str, claim_revenue: float, claim_result: str = "not_attempted",
        final_price_source: str = "", official=None,
    ):
        """Apply win/loss to stats, print result, alert Telegram, log to tracker."""
        bankroll_before_resolution = self.stats.bankroll
        if won:
            if claim_revenue > 0:
                settlement_received = claim_revenue
            else:
                settlement_received = remaining_shares * 1.0
                self._unclaimed_winnings += settlement_received
            total_received = self._exit_revenue + settlement_received
            profit = total_received - original_cost
            self.stats.record_win(profit)
            self.stats.bankroll = compute_resolution_bankroll(
                bankroll_before_resolution, settlement_received
            )
            partial_note = f" (partial exit ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            claimed_note = " (claimed)" if claim_revenue > 0 else " (unclaimed)"
            print(f"  ✅ WIN{partial_note}{claimed_note} +${profit:.2f} [{resolution_method}] | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            net_loss = original_cost - self._exit_revenue
            profit = -net_loss
            self.stats.record_loss(net_loss)
            self.stats.bankroll = compute_resolution_bankroll(
                bankroll_before_resolution, 0.0
            )
            partial_note = f" (partial exit ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            print(f"  ❌ LOSS{partial_note} -${net_loss:.2f} [{resolution_method}] | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.loss_alert(net_loss, self.stats.total_pnl)

        if self.dry_run:
            self._dry_period_stats["trades"] += 1
            self._dry_period_stats["pnl"] += profit
            if won:
                self._dry_period_stats["wins"] += 1
            else:
                self._dry_period_stats["losses"] += 1
            if self._entry_markov_passed:
                self._dry_threshold_trades += 1
                self._dry_period_stats["threshold_trades"] += 1
                if won:
                    self._dry_threshold_wins += 1
                    self._dry_period_stats["threshold_wins"] += 1

        if official is None:
            official = self._fetch_official_window_price(self._current_window, retries=2, delay=0.5)

        official_open = float(official.open_price or 0.0) if official else 0.0
        official_close = float(official.close_price or 0.0) if official else 0.0
        official_completed = bool(official.completed) if official else False

        binance_price, _ = self.price_feed.get_price()
        final_price = official_close if official_close > 0 else binance_price
        final_open = official_open if official_open > 0 else self._opening_price
        if official_close > 0:
            final_price_source = "polymarket_crypto_price"
        elif not final_price_source:
            final_price_source = "binance_reference_fallback_not_settlement"

        self._log_completed_dry_run_window(
            won=won, simulated_profit=profit, final_price=final_price
        )
        self.tracker.log_trade_resolve(
            btc_final_price=final_price,
            opening_price=final_open,
            won=won,
            profit=profit,
            # For LIVE claim sells, this is the actual collateral received at
            # resolution. Previously claim_revenue was omitted, so a winning
            # LIVE row could show profit without the corresponding received cash.
            exit_revenue=self._exit_revenue + claim_revenue,
            resolution_method=resolution_method,
            claim_result=claim_result,
            final_price_source=final_price_source,
            official_open_price=official_open,
            official_close_price=official_close,
            official_completed=official_completed,
        )

    # ── Hourly + shutdown ───────────────────────────────────────────

    def _check_hourly_summary(self):
        if self.dry_run:
            now = time.time()
            if now - self._last_dry_run_summary_at < self._summary_interval_seconds:
                return
            window_hours = (now - self._dry_period_stats["start"]) / 3600
            overall_hours = (now - self._dry_run_started_at) / 3600
            overall_stats = {
                "signals": self._dry_overall_signals,
                "trades": self.stats.total_trades,
                "wins": self.stats.wins,
                "losses": self.stats.losses,
                "pnl": self.stats.total_pnl,
                "threshold_trades": self._dry_threshold_trades,
                "threshold_wins": self._dry_threshold_wins,
            }
            window_summary = self._dry_summary_dict(self._dry_period_stats, window_hours)
            overall_summary = self._dry_summary_dict(overall_stats, overall_hours)

            print(f"\n{'═' * 55}")
            print(f"  🧪 6H DRY RUN SUMMARY")
            print(f"  Signals: {window_summary['signals']} | "
                  f"Trades: {window_summary['trades']} | "
                  f"WR: {window_summary['win_rate']:.1f}% | "
                  f"P&L: ${window_summary['pnl']:+.2f}")
            print(f"  p(j*,j*) WR: {window_summary['threshold_win_rate']:.1f}% "
                  f"({window_summary['threshold_trades']} trades)")
            print(f"  Overall: signals {overall_summary['signals']} | "
                  f"trades {overall_summary['trades']} | "
                  f"P&L ${overall_summary['pnl']:+.2f}")
            print(f"{'═' * 55}\n")
            self.telegram.six_hour_dry_run_summary(window_summary, overall_summary)
            self._dry_period_stats = self._new_dry_period_stats()
            self._last_dry_run_summary_at = now
            return

        current_hour = int(time.time() // 3600)
        if current_hour != self._last_hour_check:
            self._last_hour_check = current_hour
            h = self.stats.hourly.to_dict()
            o = self.stats.to_dict()

            # Sync real balance for accuracy
            if not self.dry_run and self.executor._initialized:
                real_bal = self.executor.get_collateral_balance()
                if real_bal > 0:
                    self.stats.bankroll = real_bal
                    self._last_real_balance = real_bal
                    o["bankroll"] = real_bal

            real_pnl = self.stats.bankroll - self._session_start_balance

            print(f"\n{'═' * 55}")
            print(f"  📊 HOURLY SUMMARY")
            print(f"  This hour: {h['trades']} trades | "
                  f"{h['wins']}W/{h['losses']}L | "
                  f"P&L: ${h['pnl']:+.2f}")
            if h['trades'] > 0:
                print(f"  Avg edge: {h['avg_edge']*100:.1f}%")
            print(f"  Windows: {h['windows_seen']} seen, "
                  f"{h['windows_skipped']} skipped")
            print(f"  Overall: {o['total_trades']} trades | "
                  f"P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
            print(f"  💰 Real P&L (balance): ${real_pnl:+.2f} "
                  f"(${self._session_start_balance:.2f} → ${self.stats.bankroll:.2f})")
            if self._unclaimed_winnings > 0:
                print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
            print(f"{'═' * 55}\n")
            self.telegram.hourly_summary(h, o)
            self.stats.hourly.reset()

    def _handle_shutdown(self, signum, frame):
        _ = (signum, frame)
        print(f"\n\n🛑 Shutting down...")
        self._running = False
        self.price_feed.stop()
        self.rtds_feed.stop()
        if self.executor._initialized:
            self.executor.cancel_all()

        # Final real balance sync
        if not self.dry_run and self.executor._initialized:
            real_bal = self.executor.get_collateral_balance()
            if real_bal > 0:
                self.stats.bankroll = real_bal
                self._last_real_balance = real_bal

        real_pnl = self.stats.bankroll - self._session_start_balance
        o = self.stats.to_dict()
        print(f"\n{'═' * 55}")
        print(f"  FINAL: {o['total_trades']} trades | "
              f"{o['wins']}W/{o['losses']}L | "
              f"WR: {o['win_rate']:.1f}%")
        print(f"  Tracked P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
        print(f"  💰 Real P&L: ${real_pnl:+.2f} "
              f"(${self._session_start_balance:.2f} → ${self.stats.bankroll:.2f})")
        if self._unclaimed_winnings > 0:
            print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
        print(f"{'═' * 55}")

        if self.dry_run:
            now = time.time()
            period_hours = (now - self._dry_period_stats["start"]) / 3600
            overall_hours = (now - self._dry_run_started_at) / 3600
            overall_stats = {
                "signals": self._dry_overall_signals,
                "trades": self.stats.total_trades,
                "wins": self.stats.wins,
                "losses": self.stats.losses,
                "pnl": self.stats.total_pnl,
                "threshold_trades": self._dry_threshold_trades,
                "threshold_wins": self._dry_threshold_wins,
            }
            self.telegram.six_hour_dry_run_summary(
                self._dry_summary_dict(self._dry_period_stats, period_hours),
                self._dry_summary_dict(overall_stats, overall_hours),
            )

        self.telegram.status_update(o)

        self.tracker.log_session(
            start_time=self._session_start_time,
            end_time=time.time(),
            start_balance=self._session_start_balance,
            end_balance=self.stats.bankroll,
            tracked_pnl=o["pnl"],
            trades=o["total_trades"],
            wins=o["wins"],
            losses=o["losses"],
            avg_entry_price=self.stats.hourly.avg_edge,   # proxy via hourly stats
            avg_edge=self.stats.hourly.avg_edge,
            avg_delta=self.stats.hourly.avg_delta,
        )

        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
