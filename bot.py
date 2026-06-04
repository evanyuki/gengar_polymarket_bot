#!/usr/bin/env python3
"""
PolyBot v15 — Chainlink-anchored FAK taker + 50% price stop

Strategy:
  - Brownian motion model with vol=0.12 (recalibrated from 0.08)
  - Entry gates: model prob >= MIN_PROB, fee-adjusted net edge >= edge_required,
    momentum_15s aligned with the signal side (anti-mean-reversion)
  - Position sizing: raw fractional Kelly as sanity budget, then CLOB minimum-share lot sizing
  - Exit: 50% price stop with resolution fallback

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
from executor import Executor, MAX_BUY_PRICE, POLY_MIN_ORDER_SHARES, plan_minimum_lot_order
from telegram_notifier import TelegramNotifier
from tracker import Tracker
from clob_orderbook_cache import ClobOrderBookCache


POSITION_CHECK_INTERVAL = 3


def choose_fak_price_cap(
    *,
    executable_price: float,
    tick_size: float = 0.01,
) -> float:
    """Return the current executable FAK cap; never pads above the live ask.

    Paying +N ticks was not a free fill improvement: for BTC 5m it directly
    worsens required win rate/payoff ratio. If the ask walks, skip and log it;
    do not chase. There is no slippage-chase parameter — the cap is structurally
    the executable price rounded to tick, bounded by MAX_BUY_PRICE.
    """
    if executable_price <= 0:
        return 0.0
    tick = tick_size if tick_size > 0 else 0.01
    return min(MAX_BUY_PRICE, 1.0 - tick, round(round(executable_price / tick) * tick, 6))


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
            entry_window_start=int(os.getenv("ENTRY_WINDOW_START", "240")),
            entry_window_end=int(os.getenv("ENTRY_WINDOW_END", "10")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_bet=float(os.getenv("MAX_BET", "25.0")),
            markov_persistence_threshold=float(os.getenv("MARKOV_PERSISTENCE_THRESHOLD", "0.87")),
            markov_medium_threshold=float(os.getenv("MARKOV_MEDIUM_THRESHOLD", "0.75")),
            markov_weak_min_transitions=int(os.getenv("MARKOV_WEAK_MIN_TRANSITIONS", "5")),
            high_price_edge_buffer_threshold=float(os.getenv("HIGH_PRICE_EDGE_BUFFER_THRESHOLD", "0.80")),
            high_price_min_edge=float(os.getenv("HIGH_PRICE_MIN_EDGE", "0.08")),
            require_momentum_align=os.getenv("ENTRY_REQUIRE_MOMENTUM_ALIGN", "true").lower() == "true",
            min_payoff_ratio=float(os.getenv("ENTRY_MIN_PAYOFF_RATIO", "0.15")),
            max_required_win_rate=float(os.getenv("ENTRY_MAX_REQUIRED_WR", "0.87")),
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
        # Cold-start warmup gate. Backtest (logs/gate_ticks.csv): entries taken
        # while realized_vol was the 0.12 fallback (< warm windows) AND/OR the
        # markov buffer was cold scored WR 54.5% / -$5.49 over n=11; warm-buffer
        # entries scored 92.1% / +$34.56 over n=38. Block entries until both the
        # tick buffer (momentum/persistence) and the window-delta buffer
        # (realized_vol) are real measurements, not session-start defaults.
        self._entry_require_warm: bool = os.getenv("ENTRY_REQUIRE_WARM", "true").lower() == "true"
        self._warm_min_samples: int = int(os.getenv("WARM_MIN_SAMPLES", "12"))
        self._warm_min_span_s: float = float(os.getenv("WARM_MIN_SPAN_S", "30"))
        # Warm-start realized_vol from Binance recent klines so the vol gate
        # opens on window 1 instead of idling ~30 min (6 live windows). Verified:
        # GET /api/v3/klines index1=open index4=close, weight 2, 5m candles share
        # the bot's wall-clock UTC 5-min grid (openTime%300==0). Fails soft.
        self._seed_vol_enabled: bool = os.getenv("SEED_VOL_FROM_KLINES", "true").lower() == "true"
        # Entry-price ceiling. Above this, the (1 - price) win payoff is too thin
        # to justify the forced 5-share CLOB notional: breakeven WR ~= price, so
        # buying at 0.90 needs ~90% real WR before fees. Skip those.
        self._entry_max_price: float = float(os.getenv("ENTRY_MAX_PRICE", "0.87"))
        # Skip when the CLOB minimum-order floor would force a stake more than
        # this multiple of the Kelly stake. quarter-Kelly sizes losses small vs
        # wins; the 5-share floor can inflate a high-price bet ~3x over Kelly,
        # turning one loss into 6-8 wiped wins. Skip when floor > Kelly x ratio.
        self._min_size_kelly_ratio: float = float(os.getenv("ENTRY_MIN_SIZE_KELLY_RATIO", "1.5"))
        self._current_fee_rate_bps: float = 0.0
        self._cached_up_fee_bps: float = 0.0
        self._cached_down_fee_bps: float = 0.0
        self._market_min_order_size: float = POLY_MIN_ORDER_SHARES
        self._market_tick_size: float = 0.01
        self._open_price_initial_delay: float = float(os.getenv("OPEN_PRICE_INITIAL_DELAY_SECONDS", "6"))
        self._open_price_wait_seconds: float = float(os.getenv("OPEN_PRICE_WAIT_SECONDS", "12"))
        self._open_price_retry_interval: float = float(os.getenv("OPEN_PRICE_RETRY_INTERVAL", "2"))
        self._entry_max_spread: float = float(os.getenv("ENTRY_MAX_SPREAD", "0.08"))
        self._entry_min_exit_price: float = float(os.getenv("ENTRY_MIN_EXIT_PRICE", "0.50"))
        # Price stop-loss. Data says this is a risk cap, not an EV booster:
        # Phase-2 observed 50% stops would have reduced DRY P&L, but it prevents
        # a position from riding a collapsed sell price all the way to zero when
        # the CLOB can actually sell the held shares. Disable only explicitly.
        self._stop_loss_enabled: bool = os.getenv("STOP_LOSS_ENABLED", "true").lower() == "true"
        self._stop_loss_price_fraction: float = float(os.getenv("STOP_LOSS_PRICE_FRACTION", "0.50"))
        self._source_consensus_config = SourceConsensusConfig(
            enabled=os.getenv("SOURCE_CONSENSUS_ENABLED", "true").lower() == "true",
            require_chainlink=os.getenv(
                "SOURCE_REQUIRE_CHAINLINK",
                "true" if not self.dry_run else "false",
            ).lower() == "true",
            stale_skip_seconds=float(os.getenv("SOURCE_STALE_SKIP_SEC", "30.0")),
            min_chainlink_delta_pct=float(os.getenv("CHAINLINK_MIN_DELTA_PCT", "0.07")),
        )
        self.source_consensus = SourceConsensusGate(self._source_consensus_config)
        self._orderbook_cache_enabled: bool = os.getenv("CLOB_ORDERBOOK_CACHE_ENABLED", "true").lower() == "true"
        self._orderbook_max_age: float = float(os.getenv("CLOB_ORDERBOOK_MAX_AGE_SEC", "1.0"))
        self.orderbook_cache = ClobOrderBookCache(max_book_age_seconds=self._orderbook_max_age)
        self._sdk_metadata_warmed: bool = False
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
        # _opening_price is the local Binance boundary tick retained for
        # diagnostics and momentum history only. _chainlink_open_price is the
        # Polymarket/Chainlink official openPrice and is the live entry anchor.
        # Direction/probability should be official-open anchored whenever a fresh
        # RTDS/Chainlink tick exists; Binance must not veto the final hot path.
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

        # Deferred trade resolutions (sub-$5 hold-to-resolution, or claim-sell
        # reported success but balance had not moved yet). Keyed by window_ts so
        # consecutive deferred windows never overwrite each other. Each is
        # resolved at a later boundary using the official Polymarket crypto-price
        # close vs open (settlement truth), NOT a racy wallet-balance delta.
        self._pending_phantoms: dict = {}

        # Pending buy (unverified — Polygon settlement too slow)
        self._pending_buy_side: str = ""
        self._pending_buy_price: float = 0.0
        self._pending_buy_amount: float = 0.0
        self._pending_buy_shares: float = 0.0
        self._pending_buy_token_id: str = ""
        self._pending_buy_edge: float = 0.0
        self._pending_buy_delta: float = 0.0
        self._balance_before_buy: float = 0.0
        # Full entry-context snapshot for an unverified buy, so a late-detected
        # fill still writes a complete trades.csv entry row (not just a
        # resolution against a stale _current_trade). Accounting fields are
        # filled from the actual wallet delta at detection time.
        self._pending_buy_entry: dict = {}

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
        import logging as _log
        _log.basicConfig(level=_log.INFO, format="[%(name)s] %(message)s")
        if not self.dry_run:
            # P0 (direct-default): CLOB order placement reaches the app layer
            # directly from this host — verified POST /order -> HTTP 401 (auth),
            # NOT 403 (Cloudflare). Direct cuts the order round-trip from
            # ~400-4900ms (Tor) to ~190ms, the root cause of the FAK no-fill /
            # price-walk losses seen in live logs. Tor is NOT started here; it is
            # lazily activated by executor._activate_tor_fallback() only if a
            # live order ever returns HTTP 403.
            print("\n🌐 CLOB direct connection (no Tor). "
                  "Tor = lazy fallback on CF 403.\n")

        kf = self.strategy_config.kelly_fraction
        mp = self.strategy_config.min_prob
        me = self.strategy_config.min_edge
        print("=" * 55)
        print(f"  PolyBot v15 — Chainlink-anchored FAK taker + 50% price stop")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% raw fraction | "
              f"Max bet: ${self.strategy_config.max_bet:.0f} | "
              f"CLOB min lot: {self._market_min_order_size:.0f} shares")
        print(f"  Min prob: {mp:.0%} | ε gap: {me:.0%} | Chainlink near-zero gate: {self._source_consensus_config.min_chainlink_delta_pct:.2f}%")
        print(
            f"  Markov: LOG ONLY (regime/persistence recorded; no entry gate, no size haircut) | "
            f"Payoff gate: min ratio≥{self.strategy_config.min_payoff_ratio:.2f}, "
            f"required WR≤{self.strategy_config.max_required_win_rate:.0%}"
        )
        print(
            f"  Momentum gate: {'ON (m15 must align with side)' if self.strategy_config.require_momentum_align else 'OFF'} "
            f"| markov size-haircut: BYPASSED (full quarter-Kelly)"
        )
        print(
            f"  Source gate: {'ON' if self._source_consensus_config.enabled else 'OFF'} | "
            f"Chainlink required={self._source_consensus_config.require_chainlink} | "
            f"CL near-zero<{self._source_consensus_config.min_chainlink_delta_pct:.2f}% | "
            f"stale skip>{self._source_consensus_config.stale_skip_seconds:.0f}s"
        )
        print(
            f"  CLOB price source: {'websocket cache' if self._orderbook_cache_enabled else 'REST'} "
            f"| max age {self._orderbook_max_age:.1f}s"
        )
        print(
            f"  Entry execution: FAK taker/no-order dry-run "
            f"| cap = current executable quote (no +tick chase)"
        )
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
        print(f"  Vol: dynamic (fallback=0.12, floor={self._vol_floor}, cap={self._vol_cap}, windows={self._rolling_vol_windows})")
        stop_label = (
            f"price-stop @{self._stop_loss_price_fraction:.0%} of entry + resolution fallback"
            if self._stop_loss_enabled else "hold to resolution"
        )
        print(f"  Exits: {stop_label}")
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

        self._seed_vol_from_klines()

        self.telegram.startup_alert({
            "dry_run": self.dry_run,
            "kelly_fraction": kf,
            "min_edge": self.strategy_config.min_edge,
            "max_bet": self.strategy_config.max_bet,
            "minimum_order_shares": self._market_min_order_size,
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

        # IDLE: official-open anchored signal. Binance direct WS remains useful
        # for local momentum/diagnostics, but trade side/probability are based on
        # RTDS Chainlink vs Polymarket/Chainlink openPrice when available.
        chainlink_price = self.rtds_feed.get_latest()
        get_rtds_binance = getattr(self.rtds_feed, "get_binance_latest", None)
        rtds_binance_price = get_rtds_binance() if callable(get_rtds_binance) else None
        entry_ref = self._entry_reference_price(binance_price=btc_price, chainlink=chainlink_price)
        if not self.dry_run and (entry_ref["price"] <= 0 or not entry_ref["side"]):
            # No live Binance fallback: settlement-source Chainlink/openPrice is
            # the only side/probability anchor. Missing/stale Chainlink means no trade.
            return
        signal_btc_price = entry_ref["price"]
        signal_opening_price = entry_ref["opening_price"]

        up_price, down_price = self._get_market_prices(signal_btc_price, seconds_remaining)

        realized_vol = self._compute_realized_vol()
        candidate_side = entry_ref["side"]
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
        # Momentum is computed before evaluate() so the momentum-confirmation gate
        # can use it. Also reused below for gate-tick logging.
        momentum_15s = self._markov_filter.price_change_pct(15, now=now)
        momentum_30s = self._markov_filter.price_change_pct(30, now=now)
        signal_result = evaluate(
            btc_price=signal_btc_price,
            opening_price=signal_opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            bankroll=self.stats.bankroll,
            config=self.strategy_config,
            realized_vol=realized_vol,
            markov_persistence=markov_persistence,
            markov_stats=markov_stats,
            fee_rate_bps=fee_rate_bps,
            momentum_15s_pct=momentum_15s,
        )
        candidate_market_price = up_price if candidate_side == "UP" else down_price
        opposite_market_price = down_price if candidate_side == "UP" else up_price
        btc_delta_pct = ((signal_btc_price - signal_opening_price) / signal_opening_price * 100) if signal_opening_price > 0 else 0.0
        # Model runs on the same anchor as the signal. In live trading that is
        # RTDS Chainlink vs official Polymarket openPrice, not Binance vs local
        # boundary tick. This removes the observed Binance-basis direction bug.
        model_delta_pct = btc_delta_pct
        true_prob = estimate_true_probability(model_delta_pct, seconds_remaining, vol=realized_vol)
        candidate_true_prob = true_prob
        raw_edge = candidate_true_prob - candidate_market_price
        net_edge = fee_adjusted_edge(candidate_true_prob, candidate_market_price, fee_rate_bps)
        chainlink_raw_price = float(getattr(chainlink_price, "price", 0.0) or 0.0) if chainlink_price else 0.0
        rtds_binance_raw_price = float(getattr(rtds_binance_price, "price", 0.0) or 0.0) if rtds_binance_price else 0.0
        chainlink_delta_pct = (
            (chainlink_raw_price - self._chainlink_open_price) / self._chainlink_open_price * 100.0
            if chainlink_raw_price > 0 and self._chainlink_open_price > 0 else 0.0
        )
        rtds_binance_delta_pct = (
            (rtds_binance_raw_price - self._opening_price) / self._opening_price * 100.0
            if rtds_binance_raw_price > 0 and self._opening_price > 0 else 0.0
        )
        signal_delta_pct = btc_delta_pct
        entry_observability = {
            "entry_signal_source": entry_ref["source"],
            "entry_signal_price": signal_btc_price,
            "entry_signal_open_price": signal_opening_price,
            "entry_signal_delta_pct": signal_delta_pct,
            "entry_chainlink_price": chainlink_raw_price,
            "entry_chainlink_delta_pct": chainlink_delta_pct,
            "entry_rtds_binance_price": rtds_binance_raw_price,
            "entry_rtds_binance_delta_pct": rtds_binance_delta_pct,
        }
        if signal_result:
            for key, value in entry_observability.items():
                setattr(signal_result, key, value)
        diagnostic_kelly = kelly_bet_size(
            true_prob=candidate_true_prob,
            market_price=candidate_market_price,
            bankroll=self.stats.bankroll,
            fraction=self.strategy_config.kelly_fraction,
            max_bet=self.strategy_config.max_bet,
            fee_rate_bps=fee_rate_bps,
        )
        gate_reason = "signal_ready" if signal_result else get_skip_reason(
            btc_price=signal_btc_price,
            opening_price=signal_opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            config=self.strategy_config,
            realized_vol=realized_vol,
            markov_persistence=markov_persistence,
            markov_stats=markov_stats,
            fee_rate_bps=fee_rate_bps,
            momentum_15s_pct=momentum_15s,
        )
        if candidate_market_price >= 0.90:
            book_state = "target_extreme_high_no_margin"
        elif candidate_market_price <= 0.10:
            book_state = "target_extreme_low_possible_wrong_side_or_illiquid"
        elif opposite_market_price >= 0.90 or opposite_market_price <= 0.10:
            book_state = "complement_extreme"
        else:
            book_state = "normal"
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
            opening_price=signal_opening_price,
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
            signal_source=entry_ref["source"],
            signal_price=signal_btc_price,
            signal_opening_price=signal_opening_price,
            signal_delta_pct=signal_delta_pct,
            signal_side=candidate_side,
            chainlink_price=chainlink_raw_price,
            chainlink_open_price=self._chainlink_open_price,
            chainlink_delta_pct=chainlink_delta_pct,
            rtds_binance_price=rtds_binance_raw_price,
            rtds_binance_open_price=self._opening_price,
            rtds_binance_delta_pct=rtds_binance_delta_pct,
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
            "momentum_15s": momentum_15s,
        }

        if signal_result:
            self._window_signals_detected += 1
            self._dry_overall_signals += 1
            self._dry_period_stats["signals"] += 1
            if self._entry_require_warm:
                warm, warm_reason = self._buffers_warm(now)
                if not warm:
                    print(f"  🧊 Buffers warming ({warm_reason}) — skip entry "
                          f"(samples={len(self._markov_filter._samples)}, "
                          f"vol_windows={len(self._recent_window_deltas)})")
                    self._log_source_skip(signal_result, seconds_remaining, source_decision, warm_reason)
                    # Do NOT set _trade_attempted: re-evaluate next tick so we can
                    # enter the moment buffers warm within the entry window.
                    return
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
            # SourceConsensus no longer downsizes based on Binance/RTDS. Binance is
            # diagnostic only; the settlement-source Chainlink gate either skips
            # unsafe entries or leaves raw Kelly sizing unchanged.
            self._execute_trade(signal_result, seconds_remaining)

        if now - self._last_status_print >= 30:
            self._last_status_print = now
            delta = ((btc_price - self._opening_price) / self._opening_price * 100) if self._opening_price > 0 else 0
            d = "↑" if delta > 0 else "↓" if delta < 0 else "→"
            chainlink_delta_for_state = (
                abs((chainlink_raw_price - self._chainlink_open_price) / self._chainlink_open_price * 100.0)
                if chainlink_raw_price > 0 and self._chainlink_open_price > 0 else None
            )
            if self._traded:
                state = "HOLDING"
            elif chainlink_delta_for_state is not None and chainlink_delta_for_state < self._source_consensus_config.min_chainlink_delta_pct:
                state = f"CLΔSMALL ({chainlink_delta_for_state:.3f}%<{self._source_consensus_config.min_chainlink_delta_pct:.3f}%)"
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

    def _entry_reference_price(self, *, binance_price: float, chainlink) -> dict:
        """Return the price/open anchor used for entry side and probability.

        The 5m market settles from Polymarket/Chainlink, so live entry is
        anchored to official openPrice plus the freshest RTDS Chainlink tick.
        Binance is retained only for dry-run fallback and diagnostics; live must
        not silently fall back to Binance for side/probability.
        """
        chainlink_price = float(getattr(chainlink, "price", 0.0) or 0.0) if chainlink else 0.0
        chainlink_age = getattr(chainlink, "age_seconds", None) if chainlink else None
        stale_limit = float(getattr(self.source_consensus.config, "stale_skip_seconds", 30.0) or 30.0)
        chainlink_fresh = (
            chainlink_price > 0
            and self._chainlink_open_price > 0
            and (chainlink_age is None or float(chainlink_age) <= stale_limit)
        )
        if chainlink_fresh:
            opening_price = float(self._chainlink_open_price)
            side = "UP" if chainlink_price >= opening_price else "DOWN"
            return {
                "price": chainlink_price,
                "opening_price": opening_price,
                "side": side,
                "source": "polymarket_rtds_chainlink",
                "age_seconds": chainlink_age,
            }

        if not self.dry_run:
            source = "missing_chainlink" if chainlink_price <= 0 or self._chainlink_open_price <= 0 else "stale_chainlink"
            return {
                "price": 0.0,
                "opening_price": float(self._chainlink_open_price or 0.0),
                "side": "",
                "source": source,
                "age_seconds": chainlink_age,
            }

        opening_price = float(self._opening_price or 0.0)
        side = "UP" if binance_price >= opening_price else "DOWN"
        return {
            "price": float(binance_price or 0.0),
            "opening_price": opening_price,
            "side": side,
            "source": "binance_fallback",
            "age_seconds": None,
        }

    # ── Active position management ──────────────────────────────────

    # ── Position monitoring + price-stop exit ───────────────────────

    def _manage_position(self, btc_price: float, seconds_remaining: float, now: float):
        """Monitor active position and fire the configured price stop.

        Resolution remains the fallback when the position is unsellable or the
        stop order fails; sell prices are also logged for calibration.
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
                    f"P&L ${self.stats.total_pnl:+.2f} [STOP/RES]"
                )
            return

        self._last_position_check = now

        # Get current sell price for stop-loss and diagnostics.
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

        if self._should_price_stop(current_sell_price):
            self._execute_price_stop(current_sell_price)
            return

        # Status line
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

    def _should_price_stop(self, current_sell_price: float) -> bool:
        if not self._stop_loss_enabled:
            return False
        if self._trade_price <= 0 or current_sell_price <= 0:
            return False
        return current_sell_price <= self._trade_price * self._stop_loss_price_fraction

    def _execute_price_stop(self, current_sell_price: float) -> None:
        """Exit a held position when the sell price collapses below stop threshold.

        This is a loss cap, not a claim about positive EV. LIVE sells are still
        subject to Polymarket minimum share/order constraints; if the held size
        is unsellable, the bot must hold to resolution rather than submit a
        guaranteed-rejected order.
        """
        if not self._traded or self._trade_shares <= 0:
            return

        stop_threshold = self._trade_price * self._stop_loss_price_fraction
        if not self.dry_run and self._trade_shares < self._market_min_order_size:
            print(
                f"  ⚠️  Price-stop hit (${current_sell_price:.3f} ≤ ${stop_threshold:.3f}) "
                f"but {self._trade_shares:.1f} shares < {self._market_min_order_size:.0f} min — hold to resolution"
            )
            return

        started = time.time()
        if self.dry_run:
            shares_sold = self._trade_shares
            exit_revenue = shares_sold * current_sell_price
            success = True
            error = ""
        else:
            result = self.executor.sell(self._trade_token_id, self._trade_shares, current_sell_price)
            success = result.success
            error = result.error
            shares_sold = result.shares if success else 0.0
            exit_revenue = result.amount_usd if success else 0.0

        latency_ms = (time.time() - started) * 1000.0
        self.tracker.log_execution(
            window_ts=self._current_window,
            action="price_stop_sell",
            latency_ms=latency_ms,
            success=success,
            error=error,
            details=f"sell_price={current_sell_price:.4f}; threshold={stop_threshold:.4f}",
        )
        if not success:
            print(f"  ❌ Price-stop sell failed: {error} — hold to resolution")
            return

        residual_shares = max(0.0, self._trade_shares - shares_sold)
        self._exit_revenue += exit_revenue
        self.tracker.log_trade_exit(
            exit_type="price-stop",
            exit_price=current_sell_price,
            exit_shares_sold=shares_sold,
            exit_revenue=exit_revenue,
            residual_shares=residual_shares,
            latency_ms=latency_ms,
        )

        print(
            f"  🛑 PRICE STOP: sold {shares_sold:.1f}/{self._trade_shares:.1f} "
            f"@ ${current_sell_price:.3f} (threshold ${stop_threshold:.3f}) "
            f"→ revenue ${exit_revenue:.2f}"
        )

        if residual_shares >= 1.0:
            # Hold the residual to resolution. Do NOT reduce _trade_cost here:
            # _record_resolution already applies _exit_revenue against the full
            # original cost (net_loss = cost - _exit_revenue / total = _exit_revenue
            # + settlement). Decrementing the cost basis too would double-count the
            # partial proceeds, silently understating losses / overstating wins.
            # Keeping the full cost basis also makes multiple partial stops sum
            # correctly via _exit_revenue accumulation.
            self._trade_shares = residual_shares
            return

        self._record_resolution(
            won=False,
            original_cost=self._trade_cost,
            remaining_shares=0.0,
            resolution_method="price_stop_50pct",
            claim_revenue=0.0,
            claim_result="price_stop_exit",
            final_price_source="exit_price_not_settlement",
        )
        self._traded = False
        self._trade_side = ""
        self._trade_price = 0.0
        self._trade_cost = 0.0
        self._trade_shares = 0.0
        self._trade_token_id = ""

    # ── Window management ───────────────────────────────────────────

    def _on_new_window(self, window_ts: int, closing_btc_price: float = 0.0):
        if self._current_window > 0:
            # Resolve any deferred trades from earlier windows using official
            # settlement (crypto-price close vs open), not a wallet-balance delta.
            # Must run before trade state is reset below.
            self._resolve_pending_phantoms()

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
                            # The buy DID go through — retroactively track it.
                            # Use the INTENDED integer share count (what we
                            # submitted), NOT spent/price: spent includes the
                            # CLOB fee, so dividing it back by price inflates the
                            # count (e.g. $4.05 / $0.79 = 5.1 for a 5-share order).
                            # Fee is the residual: spent - shares*price.
                            est_shares = (
                                float(int(self._pending_buy_shares))
                                if self._pending_buy_shares >= 1
                                else float(int(spent / self._pending_buy_price))
                                if self._pending_buy_price > 0 else 0.0
                            )
                            planned_notional = round(est_shares * self._pending_buy_price, 2)
                            est_fee = max(0.0, round(spent - planned_notional, 2))
                            print(f"\n  👻 LATE FILL: balance dropped ${spent:.2f} since buy attempt")
                            print(f"     Retroactively tracking: {est_shares:.0f} shares "
                                  f"{self._pending_buy_side} @ ${self._pending_buy_price:.3f} "
                                  f"(notional ${planned_notional:.2f}, fee≈${est_fee:.2f})")

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
                            # Stage the full entry row so the upcoming
                            # _resolve_previous_trade writes a complete trade
                            # (entry + resolution), not a resolution against a
                            # stale _current_trade. Shares stay the intended int;
                            # cash/notional/fee come from the actual wallet delta.
                            if self._pending_buy_entry:
                                self.tracker.log_trade_entry(
                                    **self._pending_buy_entry,
                                    entry_shares=est_shares,
                                    entry_cost=spent,
                                    mode="DRY" if self.dry_run else "LIVE",
                                    planned_order_notional_usd=planned_notional,
                                    actual_cash_spent_usd=spent,
                                    estimated_fee_usd=est_fee,
                                )

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
                    momentum_15s_pct=ctx.get("momentum_15s"),
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
        self._pending_buy_entry = {}
        self._window_signals_detected = 0
        self._entry_markov_state = ""
        self._entry_markov_persistence = 0.0
        self._entry_markov_threshold = self.strategy_config.markov_persistence_threshold
        self._entry_markov_passed = False
        self._window_open_price_missing = False
        self._sdk_metadata_warmed = False

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

    def _resolve_pending_phantoms(self) -> None:
        """Resolve deferred trades against official settlement, keyed per window.

        Settlement truth = Polymarket crypto-price (Chainlink) close vs open for
        the trade's OWN window (symbol=BTC, fiveminute). A wallet-balance delta
        is NOT used to decide win/loss: redemption can lag past the boundary and
        a new window's buy spends balance in between, so the old heuristic
        mislabeled real wins as losses. Win/loss now comes from the close.

        A window whose official price is not completed yet stays pending and is
        retried at the next boundary — never dropped, never guessed.
        """
        if not self._pending_phantoms:
            return
        for window_ts in sorted(self._pending_phantoms.keys()):
            pp = self._pending_phantoms[window_ts]
            official = self._fetch_official_window_price(window_ts, retries=2, delay=0.5)
            winning_side = self._official_winning_side(official)
            if not winning_side:
                # Settlement not available yet — keep pending, retry next boundary.
                print(f"  ⏳ Deferred {pp['side']} @{window_ts}: official price not "
                      f"settled yet — retry next boundary")
                continue

            official_open = float(official.open_price or 0.0)
            official_close = float(official.close_price or 0.0)
            won = (winning_side == pp["side"])
            shares = pp.get("shares", 0.0)
            cost = pp.get("cost", 0.0)
            exit_revenue = pp.get("exit_revenue", 0.0)
            if won:
                # Held to resolution: winning shares redeem at $1 each.
                profit = (shares * 1.0 + exit_revenue) - cost
                self.stats.record_win(profit)
                print(f"  ✅ Auto-resolved WIN +${profit:.2f} [{pp['side']}] | "
                      f"official {official_open:.2f}->{official_close:.2f} ({winning_side}) | "
                      f"P&L: ${self.stats.total_pnl:+.2f}")
                self.telegram.win_alert(profit, self.stats.total_pnl)
            else:
                net_loss = cost - exit_revenue
                profit = -net_loss
                self.stats.record_loss(net_loss)
                print(f"  ❌ Auto-resolved LOSS -${net_loss:.2f} [{pp['side']}] | "
                      f"official {official_open:.2f}->{official_close:.2f} ({winning_side}) | "
                      f"P&L: ${self.stats.total_pnl:+.2f}")
                self.telegram.loss_alert(net_loss, self.stats.total_pnl)

            self.tracker.resolve_pending_trade(
                window_ts=window_ts,
                btc_final_price=official_close,
                opening_price=official_open if official_open > 0 else pp.get("opening_price", 0.0),
                won=won,
                profit=profit,
                exit_revenue=exit_revenue,
                resolution_method="auto_resolution",
                claim_result="redeemed" if won else "expired_worthless",
                final_price_source="polymarket_crypto_price",
                official_open_price=official_open,
                official_close_price=official_close,
                official_completed=True,
            )
            del self._pending_phantoms[window_ts]

    # ── Market prices (cached, complement engine) ───────────────────

    def _refresh_market_metadata_and_orderbook(self, market) -> None:
        if not market:
            return
        try:
            if not self.dry_run and self.executor._initialized:
                metadata = self.executor.get_market_metadata(market.condition_id)
                self._market_min_order_size = metadata.minimum_order_size
                self._market_tick_size = metadata.minimum_tick_size
                self._cached_up_fee_bps = metadata.fee_rate_bps
                self._cached_down_fee_bps = metadata.fee_rate_bps
                warm_fn = getattr(self.executor, "warm_order_metadata", None)
                if callable(warm_fn):
                    warm_raw = warm_fn([market.token_id_up, market.token_id_down])
                    warm = warm_raw if isinstance(warm_raw, dict) else {}
                    self._sdk_metadata_warmed = bool(warm.get("tokens", 0))
                    if warm.get("errors"):
                        print(f"  ⚠️  SDK metadata warmup partial: {len(warm.get('errors', []))} errors")
            if self._orderbook_cache_enabled:
                self.orderbook_cache.subscribe([market.token_id_up, market.token_id_down])
                self.orderbook_cache.start()
                print(
                    f"  📡 CLOB orderbook cache subscribed "
                    f"(UP/DOWN, max_age={self._orderbook_max_age:.1f}s)"
                )
        except Exception as e:
            print(f"[market] Metadata/orderbook setup failed: {sanitize_exception_text(e)}")

    def _seed_vol_from_klines(self) -> None:
        """Warm-start realized_vol from Binance recent klines.

        Each candle's abs((close-open)/open*100) is the EXACT statistic the live
        path appends at window close (see `closing_delta`). Binance klines share
        the bot's wall-clock 5-min UTC boundaries (openTime % period == 0), so
        the seed is the same measurement observed early, letting the vol gate
        open on window 1 instead of idling ~30 min for 6 live windows.

        Fails soft: on any error the buffer stays empty and the warmup gate
        still guards entry. Drops the last (in-progress, unsettled) candle.
        """
        if not self._seed_vol_enabled or self._recent_window_deltas:
            return
        import json as _json
        import urllib.request as _url
        interval = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m"}.get(self.period, "5m")
        n = self._rolling_vol_windows
        endpoint = (
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
            f"&interval={interval}&limit={n + 1}"
        )
        min_needed = max(6, self._rolling_vol_windows // 2)
        try:
            with _url.urlopen(endpoint, timeout=5) as resp:
                raw = _json.load(resp)
            deltas = []
            for k in raw[:-1]:  # drop in-progress last candle
                o, c = float(k[1]), float(k[4])
                if o > 0:
                    deltas.append(abs((c - o) / o * 100.0))
            deltas = deltas[-n:]
            if len(deltas) >= min_needed:
                self._recent_window_deltas = deltas
                print(f"  🔥 Vol seeded from {len(deltas)} Binance {interval} klines "
                      f"→ realized_vol={self._compute_realized_vol():.4f} (vol gate open at window 1)")
            else:
                print(f"  ⚠️  Vol seed: only {len(deltas)} usable candles "
                      f"(<{min_needed}); cold start + warmup gate active")
        except Exception as e:
            print(f"  ⚠️  Vol seed failed ({e}); cold start + warmup gate active")

    def _buffers_warm(self, now: float = None) -> tuple[bool, str]:
        """Both cold-start buffers ready? Returns (warm, reason_if_cold).

        markov tick buffer -> momentum/persistence are real (not 0.0 zeros).
        window-delta buffer -> realized_vol is measured, not the 0.12 fallback
        that mis-selects mean-reverting outlier spikes (see backtest).
        """
        if not self._markov_filter.is_warm(
            min_samples=self._warm_min_samples,
            min_span_seconds=self._warm_min_span_s,
            now=now,
        ):
            return False, "markov_warming_up"
        min_vol_windows = max(6, self._rolling_vol_windows // 2)
        if len(self._recent_window_deltas) < min_vol_windows:
            return False, "vol_warming_up"
        return True, ""

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
        # DRY_RUN is quote-only, not synthetic-price paper trading. Use the same
        # real CLOB quote path as live and simply avoid submitting orders later.
        # If no fresh quote exists, return cached/zero and let gates skip; never
        # fabricate tanh prices to evaluate PnL.
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

        # Circuit breaker state only. Do not ping CLOB health on every buy: the
        # unauthenticated GET adds avoidable hot-path latency, while the FAK POST
        # itself plus buy-failure counter is the actionable health signal. When
        # halted, recovery is still probed once per new window in _on_new_window.
        if self._clob_halted:
            print(f"  🔌 CLOB HALTED — skipping trade ({self._consecutive_buy_failures} consecutive failures)")
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

        market = self._current_market
        if not market:
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
            print("  ⚠️  No current Polymarket market/token — skipping entry (DRY and LIVE both require real CLOB path)")
            return

        slug = f"btc-updown-{self.period}m-{self._current_window}"
        raw_kelly_usd = round(sig.kelly_size, 2)
        trade_amount = raw_kelly_usd
        planned_order_notional_usd = 0.0
        sizing_reason = "raw_kelly"

        print(f"\n  🎯 {sig.side} | Δ={sig.gap:.3f} | fee-edge={sig.fee_adjusted_edge:.3f} | "
              f"req={sig.edge_required:.3f} | prob={sig.true_prob:.2f} | "
              f"p(j*,j*)={sig.markov_persistence:.2f} [{sig.markov_regime}] | BTC Δ={sig.btc_delta_pct:+.3f}%")
        print(f"     Kelly: ${trade_amount:.2f} | mkt ${sig.market_price:.3f} | fee {sig.fee_rate_bps:.1f}bps | T-{seconds_remaining:.0f}s")

        source_decision = self.source_consensus.assess_snapshot(sig.side)
        if source_decision.reason == "source_snapshot_missing" and not self.dry_run:
            # LIVE hot path must not synchronously refresh source snapshots or
            # fallback to Binance. If RTDS/Chainlink snapshot is missing, skip.
            # Waiting here loses the execution race and can resurrect the old
            # wrong-source direction bug.
            pass
        elif source_decision.reason == "source_snapshot_missing":
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
        # FAK taker execution. evaluate() already cleared the edge gate
        # (net_edge >= edge_required). Below, the live ask is re-checked against
        # edge_required to catch price slippage between signal and execution.
        # Post-only/GTD maker entry was removed because it conflicts with the
        # latency edge and creates adverse-selection fills.
        hint_price = sig.market_price if self.dry_run else 0.0
        depth_snapshot = None
        entry_snapshot = None
        if market and (self.dry_run or self.executor._initialized):
            actual_price = 0.0
            if self._orderbook_cache_enabled:
                actual_price = self.orderbook_cache.get_market_price(token_id, "BUY", trade_amount)
            else:
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
            required_fee_edge = sig.edge_required
            price_cap = choose_fak_price_cap(
                executable_price=actual_price,
                tick_size=self._market_tick_size,
            )
            actual_fee_edge = fee_adjusted_edge(sig.true_prob, price_cap, sig.fee_rate_bps)
            print(
                f"  📊 FAK ask: ${actual_price:.3f} | cap ${price_cap:.3f} "
                f"(edge@cap: {sig.true_prob - price_cap:.3f}, fee-edge@cap: {actual_fee_edge:.3f}, "
                f"threshold: {required_fee_edge:.3f})"
            )
            if actual_fee_edge < required_fee_edge:
                print("  ⚠️  Edge gone at live ask (slippage) — skipping")
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
                    action="skipped_edge_gone_at_ask",
                    skip_reason="edge_below_required_at_live_ask",
                    actual_price=actual_price,
                    actual_edge=actual_edge,
                )
                return

            # P1b: entry-price ceiling. Above this the (1 - price) win payoff is
            # too thin to carry the forced 5-share notional — breakeven WR ~=
            # price, so buying at 0.89 needs ~89% real WR before fees.
            if actual_price > self._entry_max_price:
                print(f"  ⛔ Entry price ${actual_price:.3f} > max ${self._entry_max_price:.2f} "
                      f"— payoff too thin for forced min size, skipping")
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
                    action="skipped_entry_price_above_max",
                    skip_reason="entry_price_above_max",
                    actual_price=actual_price,
                    actual_edge=actual_edge,
                )
                return

            # Live sizing is minimum-share-lot aware. Polymarket's
            # minimum_order_size is shares (BTC 5m commonly 5), not a fixed $5
            # notional. Raw Kelly is only the sanity budget; do not floor it with
            # a fake MIN_BET dollar value.
            lot_plan = plan_minimum_lot_order(
                price=price_cap,
                raw_kelly_usd=trade_amount,
                minimum_order_shares=self._market_min_order_size,
                max_bet_usd=self.strategy_config.max_bet,
                max_floor_to_kelly_ratio=self._min_size_kelly_ratio,
            )
            if not lot_plan.executable:
                print(
                    f"  ⛔ Minimum-share lot not executable ({lot_plan.reason}): "
                    f"min {lot_plan.minimum_order_shares:.0f} shares = ${lot_plan.minimum_cost_usd:.2f} "
                    f"@ cap ${price_cap:.3f}; raw Kelly ${trade_amount:.2f}; "
                    f"max ${self.strategy_config.max_bet:.2f}; ratio {self._min_size_kelly_ratio:.1f}x"
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
                    action="skipped_minimum_share_lot",
                    skip_reason=lot_plan.reason,
                    actual_price=actual_price,
                    actual_edge=actual_edge,
                )
                return

            if abs(lot_plan.amount_usd - trade_amount) > 0.005:
                print(
                    f"  ℹ️  Minimum-share lot sizing: raw Kelly ${trade_amount:.2f} → "
                    f"{lot_plan.shares} shares / ${lot_plan.amount_usd:.2f} "
                    f"({lot_plan.reason}; min {lot_plan.minimum_order_shares:.0f} shares)"
                )
            trade_amount = lot_plan.amount_usd
            sig.kelly_size = trade_amount
            planned_order_notional_usd = lot_plan.amount_usd
            sizing_reason = lot_plan.reason
            planned_shares = float(lot_plan.shares)
            _planned_spend = lot_plan.amount_usd
            entry_snapshot = None
            current_sell_price = 0.0
            if self._orderbook_cache_enabled and planned_shares > 0:
                get_entry_snapshot = getattr(self.orderbook_cache, "get_entry_snapshot", None)
                if callable(get_entry_snapshot):
                    entry_snapshot = get_entry_snapshot(
                        token_id,
                        buy_usd_amount=trade_amount,
                        sell_shares_amount=planned_shares,
                        required_buy_shares=planned_shares,
                        cap_price=price_cap,
                    )
                    depth_snapshot = getattr(entry_snapshot, "depth", None)
                    current_sell_price = float(getattr(entry_snapshot, "executable_sell_price", 0.0) or 0.0)
                    executable_buy_price = float(getattr(entry_snapshot, "executable_buy_price", 0.0) or 0.0)
                    if executable_buy_price > 0 and executable_buy_price != actual_price:
                        actual_price = executable_buy_price
                else:
                    depth_snapshot = self.orderbook_cache.get_buy_depth_snapshot(
                        token_id,
                        required_shares=planned_shares,
                        cap_price=price_cap,
                    )
                if depth_snapshot is None:
                    print("  ⚠️  Atomic CLOB snapshot unavailable before FAK entry — skipping")
                    return
                print(
                    f"  📚 Atomic book: depth≤cap {depth_snapshot.cumulative_shares:.0f}/"
                    f"{planned_shares:.0f} shares | best ask ${depth_snapshot.best_ask:.3f} "
                    f"| worst ${depth_snapshot.worst_price:.3f} | sell ${current_sell_price:.3f} "
                    f"| age {depth_snapshot.book_age_ms:.0f}ms"
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

            # No final Binance recheck here. The signal has already been built
            # from official-open anchored Chainlink, and source risk is handled by
            # SourceConsensusGate. A last-millisecond Binance veto both adds
            # latency and can reject the correct settlement-side trade.

            exit_probe = max(trade_amount, self._market_min_order_size, 1.0)
            if current_sell_price <= 0:
                current_sell_price = (
                    0.0 if self._orderbook_cache_enabled
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

        # Option A forensics anchor: wall-time just before submit, so a no-fill
        # can report book-read -> order-land latency separately from book age.
        t_fak_submit = time.time()
        # Pass the window-open on-chain balance as a hint so executor.buy() skips
        # its ~250ms pre-signing balance GET (no position open yet this window →
        # hint == real balance). Falls back to a live fetch when <=0.
        balance_hint = self._last_real_balance if self._last_real_balance > 0 else self.stats.bankroll
        latency_timing = {
            "signal_ready_ts": t_fak_submit,
            "final_book_snapshot_ts": float(getattr(entry_snapshot, "snapshot_ts", 0.0) or 0.0),
            "book_age_ms": float(getattr(depth_snapshot, "book_age_ms", 0.0) or 0.0),
            "book_hash": str(getattr(entry_snapshot, "book_hash", "") or ""),
            "sdk_warmed": bool(self._sdk_metadata_warmed),
        }
        try:
            result = self.executor.buy(
                token_id=token_id, amount_usd=trade_amount, price=hint_price,
                balance_hint=balance_hint, timing=latency_timing,
            )
        except TypeError:
            # Test doubles / older executors may not expose the new timing kwarg.
            result = self.executor.buy(
                token_id=token_id, amount_usd=trade_amount, price=hint_price,
                balance_hint=balance_hint,
            )

        if result.success:
            result.raw_kelly_usd = raw_kelly_usd
            if planned_order_notional_usd <= 0:
                planned_order_notional_usd = round(result.shares * result.price, 2)
            result.planned_order_notional_usd = result.planned_order_notional_usd or planned_order_notional_usd
            result.actual_cash_spent_usd = result.actual_cash_spent_usd or result.amount_usd
            result.estimated_fee_usd = result.estimated_fee_usd or max(
                0.0, result.actual_cash_spent_usd - result.planned_order_notional_usd
            )
            result.sizing_reason = result.sizing_reason or sizing_reason
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
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct, entry_price=result.price)

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
                entry_signal_source=getattr(sig, "entry_signal_source", "polymarket_rtds_chainlink" if source_decision.chainlink_price else "binance_fallback"),
                entry_signal_price=getattr(sig, "entry_signal_price", source_decision.signal_price or 0.0),
                entry_signal_open_price=getattr(sig, "entry_signal_open_price", self._chainlink_open_price if source_decision.chainlink_price else self._opening_price),
                entry_signal_delta_pct=getattr(sig, "entry_signal_delta_pct", sig.btc_delta_pct),
                entry_chainlink_price=source_decision.chainlink_price or getattr(sig, "entry_chainlink_price", 0.0) or 0.0,
                entry_chainlink_delta_pct=source_decision.chainlink_delta_pct or getattr(sig, "entry_chainlink_delta_pct", 0.0) or 0.0,
                entry_rtds_binance_price=source_decision.rtds_binance_price or getattr(sig, "entry_rtds_binance_price", 0.0) or 0.0,
                entry_rtds_binance_delta_pct=source_decision.rtds_binance_delta_pct or getattr(sig, "entry_rtds_binance_delta_pct", 0.0) or 0.0,
                raw_kelly_usd=raw_kelly_usd,
                planned_order_notional_usd=result.planned_order_notional_usd,
                actual_cash_spent_usd=result.actual_cash_spent_usd,
                estimated_fee_usd=result.estimated_fee_usd,
                sizing_reason=result.sizing_reason,
            )

            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.shares:.0f} shares @ "
                  f"${result.price:.3f} = ${result.amount_usd:.2f}")
            print(
                f"     Exit policy: "
                f"{'price-stop @ ' + format(self._stop_loss_price_fraction, '.0%') + ' of entry' if self._stop_loss_enabled else 'hold to resolution'}"
            )

            self.telegram.trade_alert(
                side=sig.side, price=result.price, amount=result.amount_usd,
                market_slug=slug, dry_run=self.dry_run,
                edge=sig.edge, kelly_size=sig.kelly_size,
                raw_kelly_usd=raw_kelly_usd,
                planned_order_notional_usd=result.planned_order_notional_usd,
                actual_cash_spent_usd=result.actual_cash_spent_usd,
                estimated_fee_usd=result.estimated_fee_usd,
                shares=result.shares,
                sizing_reason=result.sizing_reason,
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
                # Snapshot the full entry context NOW — sig/source_decision are
                # out of scope by the next window boundary. Shares/cost/notional/
                # fee are deliberately omitted: they get filled from the ACTUAL
                # wallet delta when the late fill is detected.
                self._pending_buy_entry = {
                    "window_ts": self._current_window,
                    "side": sig.side,
                    "entry_price": result.price,
                    "edge": sig.edge,
                    "prob": sig.true_prob,
                    "btc_delta": sig.btc_delta_pct,
                    "seconds_remaining": seconds_remaining,
                    "entry_delta_pct": sig.btc_delta_pct,
                    "entry_seconds_remaining": seconds_remaining,
                    "entry_signal_source": getattr(sig, "entry_signal_source", "polymarket_rtds_chainlink" if source_decision.chainlink_price else "binance_fallback"),
                    "entry_signal_price": getattr(sig, "entry_signal_price", source_decision.signal_price or 0.0),
                    "entry_signal_open_price": getattr(sig, "entry_signal_open_price", self._chainlink_open_price if source_decision.chainlink_price else self._opening_price),
                    "entry_signal_delta_pct": getattr(sig, "entry_signal_delta_pct", sig.btc_delta_pct),
                    "entry_chainlink_price": source_decision.chainlink_price or getattr(sig, "entry_chainlink_price", 0.0) or 0.0,
                    "entry_chainlink_delta_pct": source_decision.chainlink_delta_pct or getattr(sig, "entry_chainlink_delta_pct", 0.0) or 0.0,
                    "entry_rtds_binance_price": source_decision.rtds_binance_price or getattr(sig, "entry_rtds_binance_price", 0.0) or 0.0,
                    "entry_rtds_binance_delta_pct": source_decision.rtds_binance_delta_pct or getattr(sig, "entry_rtds_binance_delta_pct", 0.0) or 0.0,
                    "raw_kelly_usd": raw_kelly_usd,
                    "sizing_reason": result.sizing_reason or sizing_reason,
                }
                print(f"  ⏳ Buy sent but unverified — will detect via balance sync")
            else:
                print(f"  ❌ Buy failed: {result.error}")
                # Option A: on FAK no-fill, immediately re-read the book to learn
                # WHERE the ask went (race vs walk vs vanished). Decides whether a
                # wider FAK cap (option B) would help or is futile (adverse pull).
                if (result.error == "fak_no_fill_liquidity_gone"
                        and self._orderbook_cache_enabled):
                    self._fak_no_fill_forensics(
                        token_id, hint_price, depth_snapshot, t_fak_submit
                    )
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

    # ── Option A: FAK no-fill forensics ─────────────────────────────

    def _fak_no_fill_forensics(self, token_id, price_cap, pre_snapshot, t_submit):
        """After a FAK no-fill, immediately re-read the book to learn WHERE the
        ask went. Three causes look identical in the live log but need different
        fixes:
          RACE      — depth still sits at/below the old cap → pure timing; the
                      ask was there, the order just lost the race (latency).
          WALK_<=Nt — ask walked up <=N ticks → cap was too tight; a wider FAK
                      slippage cap (option B) would have filled.
          VANISHED  — no ask within +N ticks → MMs pulled on the same oracle
                      (adverse selection); paying up only buys a worse fill.
        Writes one row to logs/fak_no_fill_forensics.csv for post-run analysis.
        """
        try:
            tick = self._market_tick_size if self._market_tick_size > 0 else 0.01
            req = pre_snapshot.required_shares if pre_snapshot else 0.0
            if not req or req <= 0:
                req = float(self._market_min_order_size)
            span_ticks = 5
            wide_cap = round(price_cap + span_ticks * tick, 6)
            post = self.orderbook_cache.get_buy_depth_snapshot(
                token_id, required_shares=req, cap_price=price_cap
            )
            post_wide = self.orderbook_cache.get_buy_depth_snapshot(
                token_id, required_shares=req, cap_price=wide_cap
            )
            elapsed_ms = (time.time() - t_submit) * 1000.0 if t_submit else 0.0
            pre_ask = pre_snapshot.best_ask if pre_snapshot else 0.0
            post_ask = post.best_ask
            walk_ticks = (
                round((post_ask - pre_ask) / tick)
                if (pre_ask > 0 and post_ask > 0) else 0
            )
            if post.cumulative_shares + 1e-9 >= req:
                cause = "RACE"
            elif post_wide.cumulative_shares + 1e-9 >= req:
                cause = f"WALK_<={span_ticks}t"
            else:
                cause = "VANISHED"
            print(
                f"  🔬 no-fill forensics [{cause}]: elapsed {elapsed_ms:.0f}ms | "
                f"ask {pre_ask:.3f}→{post_ask:.3f} ({walk_ticks:+d}t) | "
                f"post depth≤cap {post.cumulative_shares:.0f} | "
                f"≤cap+{span_ticks}t {post_wide.cumulative_shares:.0f} | "
                f"req {req:.0f} | post age {post.book_age_ms:.0f}ms"
            )
            self._append_no_fill_forensics_row(
                token_id=token_id, cause=cause, elapsed_ms=round(elapsed_ms, 1),
                price_cap=price_cap, wide_cap=wide_cap,
                pre_ask=pre_ask, post_ask=round(post_ask, 6), walk_ticks=walk_ticks,
                pre_depth=(round(pre_snapshot.cumulative_shares, 2) if pre_snapshot else 0.0),
                pre_age_ms=(round(pre_snapshot.book_age_ms, 1) if pre_snapshot else 0.0),
                post_depth_at_cap=round(post.cumulative_shares, 2),
                post_depth_wide=round(post_wide.cumulative_shares, 2),
                req=req, post_age_ms=round(post.book_age_ms, 1),
            )
        except Exception as e:
            print(f"  🔬 no-fill forensics failed: {sanitize_exception_text(e)}")

    def _append_no_fill_forensics_row(self, **row):
        header = [
            "window_ts", "token_id", "cause", "elapsed_ms", "price_cap",
            "wide_cap", "pre_ask", "post_ask", "walk_ticks", "pre_depth",
            "pre_age_ms", "post_depth_at_cap", "post_depth_wide", "req",
            "post_age_ms",
        ]
        try:
            log_dir = getattr(self.tracker, "log_dir", "logs")
            path = os.path.join(log_dir, "fak_no_fill_forensics.csv")
            row = {"window_ts": self._current_window, **row}
            exists = os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if not exists:
                    f.write(",".join(header) + "\n")
                f.write(",".join(str(row.get(k, "")) for k in header) + "\n")
        except Exception as e:
            print(f"  🔬 forensics CSV write failed: {sanitize_exception_text(e)}")

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
        # Sub-$5 hold-to-resolution, or a claim that the balance hasn't confirmed
        # yet. Resolved at a later boundary against official crypto-price close
        # vs open for THIS window (see _resolve_pending_phantoms). Stash the
        # tracker's entry snapshot per window_ts so the next window's entry can
        # never overwrite it before this trade's own row is written.
        if won is None:
            if not live_token:
                print(
                    "  ⚠️  Cannot determine LIVE resolution without a live token/executor — "
                    "leaving trade unresolved for CSV/balance reconciliation"
                )
                return
            else:
                window_ts = self._current_window
                print(f"  ⏳ Resolution deferred to official settlement (window {window_ts})")
                self._pending_phantoms[window_ts] = {
                    "cost": original_cost,
                    "exit_revenue": self._exit_revenue,
                    "shares": remaining_shares,
                    "side": self._trade_side,
                    "token_id": self._trade_token_id,
                    "window_ts": window_ts,
                    "opening_price": self._opening_price,
                }
                self.tracker.stash_pending_trade(window_ts)
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
            # Session-lifetime averages from the trade CSVs (live + dry). NOT
            # self.stats.hourly.*, which resets every hour and would cover only
            # the final partial hour while the counts above are lifetime.
            **self.tracker.session_trade_averages(),
        )

        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
