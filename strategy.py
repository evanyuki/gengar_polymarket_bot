"""Strategy engine for the oracle lag scalper.

Features:
- Brownian motion probability estimation
- Kelly criterion position sizing (quarter-Kelly default)
- Hourly stats tracking for Telegram summaries
"""

import time
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeSignal:
    side: str
    confidence: float
    btc_delta_pct: float
    market_price: float
    edge: float
    true_prob: float
    seconds_remaining: float
    kelly_size: float
    gap: float = 0.0
    fee_adjusted_edge: float = 0.0
    fee_rate_bps: float = 0.0
    markov_persistence: float = 0.0
    markov_regime: str = "markov_strong"
    edge_required: float = 0.05
    payoff_ratio: float = 0.0
    required_win_rate: float = 0.0
    # Delta used by the probability model. In live this is the settlement-source
    # move: RTDS Chainlink current price vs Polymarket/Chainlink official
    # openPrice. Binance-local delta is retained only as degraded fallback and
    # diagnostic context.
    model_delta_pct: float = 0.0


@dataclass
class StrategyConfig:
    min_edge: float = 0.05          # Minimum edge (prob - price) — must be meaningful
    min_prob: float = 0.80          # Minimum model probability to consider entry
    entry_window_start: int = 240
    entry_window_end: int = 10
    max_price: float = 0.90
    min_price: float = 0.50
    kelly_fraction: float = 0.25    # Quarter-Kelly (conservative)
    # No dollar MIN_BET: live Polymarket minimum_order_size is a share count,
    # handled by executor.plan_minimum_lot_order() after the live executable ask.
    max_bet: float = 25.0           # Hard cap per trade
    # Markov is diagnostics only. It classifies regimes for logging/calibration;
    # it does not gate entries and has no size multipliers.
    markov_persistence_threshold: float = 0.87
    markov_medium_threshold: float = 0.75
    markov_weak_min_transitions: int = 5
    high_price_edge_buffer_threshold: float = 0.80
    high_price_min_edge: float = 0.08
    # Momentum-confirmation gate. The Brownian model assumes independent
    # increments, but BTC mean-reverts at sub-minute scale, so entries whose
    # last-15s move is FADING (momentum opposite the signal side) are the loss
    # generator. Official-settlement backtest (90 signal_ready windows, Polymarket
    # crypto-price): m15-aligned won 88.5% (+0.570/trade) vs m15-fading 72.7%
    # (-0.359/trade). When True, require the move still pushing in the signal
    # direction. Replaces the former price-floor gate (a blunt proxy that also
    # discarded cheap winners: px<0.78 & m15-aligned won 81.5% / +0.609/trade).
    require_momentum_align: bool = False
    # Binary payoff guard: effective price q is the breakeven required WR;
    # payoff ratio is (1-q)/q. This attacks the actual loss mode: thin odds.
    min_payoff_ratio: float = 0.0
    max_required_win_rate: float = 1.0


@dataclass
class MarkovRiskModifier:
    regime: str


@dataclass
class HourlyStats:
    """Tracks metrics for the current hour. Resets every hour."""
    hour_start: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    windows_seen: int = 0
    windows_skipped: int = 0
    edges: list = field(default_factory=list)
    deltas: list = field(default_factory=list)
    entry_prices: list = field(default_factory=list)
    trade_profits: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100) if self.trades > 0 else 0.0

    @property
    def avg_edge(self) -> float:
        return sum(self.edges) / len(self.edges) if self.edges else 0.0

    @property
    def avg_delta(self) -> float:
        return sum(abs(d) for d in self.deltas) / len(self.deltas) if self.deltas else 0.0

    @property
    def avg_entry_price(self) -> float:
        return sum(self.entry_prices) / len(self.entry_prices) if self.entry_prices else 0.0

    @property
    def best_trade(self) -> float:
        return max(self.trade_profits) if self.trade_profits else 0.0

    @property
    def worst_trade(self) -> float:
        return min(self.trade_profits) if self.trade_profits else 0.0

    def record_trade(self, edge: float, delta: float, entry_price: float = 0.0):
        self.trades += 1
        self.edges.append(edge)
        self.deltas.append(delta)
        if entry_price > 0:
            self.entry_prices.append(entry_price)

    def record_result(self, profit: float, won: bool):
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.pnl += profit
        self.trade_profits.append(profit)

    def record_window(self, traded: bool):
        self.windows_seen += 1
        if not traded:
            self.windows_skipped += 1

    def reset(self):
        self.hour_start = time.time()
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.pnl = 0.0
        self.windows_seen = 0
        self.windows_skipped = 0
        self.edges.clear()
        self.deltas.clear()
        self.entry_prices.clear()
        self.trade_profits.clear()

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "pnl": self.pnl,
            "windows_seen": self.windows_seen,
            "windows_skipped": self.windows_skipped,
            "avg_edge": self.avg_edge,
            "avg_delta": self.avg_delta,
            "avg_entry_price": self.avg_entry_price,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
        }


@dataclass
class TradingStats:
    """Overall lifetime stats with embedded hourly tracker."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 100.0
    hourly: HourlyStats = field(default_factory=HourlyStats)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

    def record_win(self, profit: float):
        self.total_trades += 1
        self.wins += 1
        self.total_pnl += profit
        self.bankroll += profit
        self.hourly.record_result(profit, won=True)

    def record_loss(self, loss: float):
        self.total_trades += 1
        self.losses += 1
        self.total_pnl -= abs(loss)
        self.bankroll -= abs(loss)
        self.hourly.record_result(-abs(loss), won=False)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "pnl": self.total_pnl,
            "bankroll": self.bankroll,
        }


def fee_rate_decimal(fee_rate_bps: float = 0.0) -> float:
    """Convert basis points from CLOB v2 metadata into a decimal rate."""
    return max(0.0, float(fee_rate_bps or 0.0)) / 10_000.0


def effective_market_price(market_price: float, fee_rate_bps: float = 0.0) -> float:
    """Fee-aware effective entry cost per share.

    Polymarket's documented fee formula is:
        fee = C × feeRate × p × (1 - p)
    where C is shares and p is price. Per share, the fee is therefore
    feeRate × p × (1 - p). Do not model fees as price × (1 + feeRate): that
    overstates high-price fees and can incorrectly force Kelly size to zero.

    Entry formula still uses the requested raw gap:
        Δ⁽ʷ⁾ = p̂⁽ʷ⁾ − q⁽ʷ⁾, q = market price

    This helper is for EV/Kelly sizing so fees reduce size without changing
    the raw entry-gap definition.
    """
    fee_rate = fee_rate_decimal(fee_rate_bps)
    return market_price + fee_rate * market_price * (1.0 - market_price)


def fee_adjusted_edge(true_prob: float, market_price: float, fee_rate_bps: float = 0.0) -> float:
    return true_prob - effective_market_price(market_price, fee_rate_bps)


def payoff_ratio(market_price: float, fee_rate_bps: float = 0.0) -> float:
    effective_price = effective_market_price(market_price, fee_rate_bps)
    if effective_price <= 0 or effective_price >= 1:
        return 0.0
    return (1.0 - effective_price) / effective_price


def required_win_rate(market_price: float, fee_rate_bps: float = 0.0) -> float:
    return min(max(effective_market_price(market_price, fee_rate_bps), 0.0), 1.0)


def kelly_fraction(true_prob: float, market_price: float, fee_rate_bps: float = 0.0) -> float:
    """Explicit Kelly criterion: f* = p − (1−p)/b.

    For a binary market bought at q, b is net odds. With fees, q is replaced
    only for sizing by the effective entry cost; the entry signal still uses
    raw Δ⁽ʷ⁾ = p̂⁽ʷ⁾ − q⁽ʷ⁾.
    """
    effective_price = effective_market_price(market_price, fee_rate_bps)
    if effective_price <= 0 or effective_price >= 1:
        return 0.0
    b = (1.0 - effective_price) / effective_price
    return true_prob - (1.0 - true_prob) / b


def kelly_bet_size(
    true_prob: float,
    market_price: float,
    bankroll: float,
    fraction: float = 0.25,
    max_bet: float = 25.0,
    fee_rate_bps: float = 0.0,
) -> float:
    """Calculate fee-aware Kelly bet size using fractional Kelly."""
    kelly_f = kelly_fraction(true_prob, market_price, fee_rate_bps)

    if kelly_f <= 0:
        return 0.0

    bet = bankroll * kelly_f * fraction
    return min(bet, max_bet)


def estimate_true_probability(
    btc_delta_pct: float, seconds_remaining: float, vol: float = 0.12
) -> float:
    """Estimate true probability using Brownian motion model.

    vol defaults to 0.12 (calibrated static fallback). When the bot has
    enough recent window data, it passes a realized rolling vol instead,
    which adapts to the current regime (higher in volatile sessions,
    lower in trending sessions).
    """
    time_factor = max(seconds_remaining, 1) / 300
    effective_vol = vol * math.sqrt(time_factor)

    if effective_vol == 0:
        return 1.0 if btc_delta_pct > 0 else 0.0

    z_score = abs(btc_delta_pct) / effective_vol
    prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))

    return min(max(prob, 0.01), 0.99)


def markov_risk_modifier(
    markov_persistence: float,
    markov_stats: Optional[dict],
    market_price: float,
    config: "StrategyConfig",
) -> MarkovRiskModifier:
    """Return Markov diagnostics only; do not gate, thicken edge, or haircut size.

    Live evidence says the real loss mode is payoff/required-WR structure. The
    Markov buffer stays as a calibration feature so we can later prove/disprove
    persistence buckets, but it no longer changes entry eligibility or sizing.
    """
    stats = markov_stats or {}
    total = int(stats.get("total") or 0)
    same = int(stats.get("same") or 0)
    raw_persistence = same / total if total > 0 else float(markov_persistence or 0.0)

    if total >= 8 and raw_persistence >= config.markov_persistence_threshold:
        regime = "markov_strong"
    elif total >= 8 and raw_persistence >= config.markov_medium_threshold:
        regime = "markov_medium"
    elif total >= config.markov_weak_min_transitions and raw_persistence >= config.markov_medium_threshold:
        regime = "markov_weak_sample_positive"
    elif total < config.markov_weak_min_transitions:
        regime = "markov_insufficient_sample"
    else:
        regime = "markov_low_persistence"

    return MarkovRiskModifier(regime=regime)


def get_skip_reason(
    btc_price: float,
    opening_price: float,
    up_market_price: float,
    down_market_price: float,
    seconds_remaining: float,
    config: "StrategyConfig" = None,
    realized_vol: float = None,
    markov_persistence: float = 1.0,
    markov_stats: Optional[dict] = None,
    fee_rate_bps: float = 0.0,
    probability_price: Optional[float] = None,
    momentum_15s_pct: Optional[float] = None,
) -> str:
    """Return why evaluate() returned None, for signal logging.

    Returns one of "" (no skip reason — should have traded),
    "before_entry_window", "after_entry_window", "model_source_side_disagrees",
    "momentum_not_aligned", "price_out_of_range", "prob_below_min",
    "edge_below_min", "required_wr_too_high", "payoff_ratio_too_low",
    "kelly_below_min", or "edge_below_min_after_fees".
    """
    if config is None:
        config = StrategyConfig()
    if opening_price <= 0:
        return ""
    if seconds_remaining > config.entry_window_start:
        return "before_entry_window"
    if seconds_remaining < config.entry_window_end:
        return "after_entry_window"
    btc_delta_pct = ((btc_price - opening_price) / opening_price) * 100
    model_price = probability_price if probability_price is not None else btc_price
    model_delta_pct = ((model_price - opening_price) / opening_price) * 100
    signal_side = "UP" if btc_delta_pct > 0 else "DOWN"
    model_side = "UP" if model_delta_pct > 0 else "DOWN"
    if model_side != signal_side:
        return "model_source_side_disagrees"
    if config.require_momentum_align and momentum_15s_pct is not None:
        mom_side = (
            "UP" if momentum_15s_pct > 0
            else "DOWN" if momentum_15s_pct < 0
            else None
        )
        if mom_side != signal_side:
            return "momentum_not_aligned"
    market_price = up_market_price if signal_side == "UP" else down_market_price
    if market_price > config.max_price or market_price < config.min_price:
        return "price_out_of_range"
    vol = realized_vol if realized_vol is not None else 0.12
    true_prob = estimate_true_probability(model_delta_pct, seconds_remaining, vol=vol)
    if true_prob < config.min_prob:
        return "prob_below_min"
    edge = true_prob - market_price
    if edge < config.min_edge:
        return "edge_below_min"
    net_edge = fee_adjusted_edge(true_prob, market_price, fee_rate_bps)
    req_wr = required_win_rate(market_price, fee_rate_bps)
    pr = payoff_ratio(market_price, fee_rate_bps)
    if req_wr > config.max_required_win_rate:
        return "required_wr_too_high"
    if pr < config.min_payoff_ratio:
        return "payoff_ratio_too_low"
    bet_size = kelly_bet_size(
        true_prob=true_prob,
        market_price=market_price,
        bankroll=1.0,
        fraction=config.kelly_fraction,
        max_bet=config.max_bet,
        fee_rate_bps=fee_rate_bps,
    )
    if bet_size <= 0:
        return "kelly_below_min"
    if net_edge < config.min_edge:
        return "edge_below_min_after_fees"
    return ""


def evaluate(
    btc_price: float,
    opening_price: float,
    up_market_price: float,
    down_market_price: float,
    seconds_remaining: float,
    bankroll: float = 100.0,
    config: StrategyConfig = None,
    realized_vol: float = None,
    markov_persistence: float = 1.0,
    markov_stats: Optional[dict] = None,
    fee_rate_bps: float = 0.0,
    probability_price: Optional[float] = None,
    momentum_15s_pct: Optional[float] = None,
) -> Optional[TradeSignal]:
    """Evaluate whether to enter a trade.

    Two-layer filter:
      1. Model probability must exceed min_prob (default 80%)
      2. Edge (prob - market_price) must exceed min_edge (default 5%)

    realized_vol: rolling std dev of recent window closing deltas.
    When None, falls back to the hardcoded 0.12 default.
    """
    if config is None:
        config = StrategyConfig()

    if seconds_remaining > config.entry_window_start:
        return None
    if seconds_remaining < config.entry_window_end:
        return None
    if opening_price <= 0:
        return None

    btc_delta_pct = ((btc_price - opening_price) / opening_price) * 100
    model_price = probability_price if probability_price is not None else btc_price
    model_delta_pct = ((model_price - opening_price) / opening_price) * 100

    side = "UP" if btc_delta_pct > 0 else "DOWN"
    model_side = "UP" if model_delta_pct > 0 else "DOWN"
    if model_side != side:
        return None

    # Momentum-confirmation gate (anti-mean-reversion). Skip entries where the
    # last-15s move is fading (momentum not pushing the signal side) or flat.
    if config.require_momentum_align and momentum_15s_pct is not None:
        mom_side = (
            "UP" if momentum_15s_pct > 0
            else "DOWN" if momentum_15s_pct < 0
            else None
        )
        if mom_side != side:
            return None

    market_price = up_market_price if side == "UP" else down_market_price

    if market_price > config.max_price or market_price < config.min_price:
        return None

    vol = realized_vol if realized_vol is not None else 0.12
    true_prob = estimate_true_probability(model_delta_pct, seconds_remaining, vol=vol)

    # Filter 1: Model must be confident enough
    if true_prob < config.min_prob:
        return None

    # Entry formula: Δ⁽ʷ⁾ = p̂⁽ʷ⁾ − q⁽ʷ⁾ ≥ ε → ENTER
    # q is the raw market price. Fees are accounted for separately in EV/Kelly.
    gap = true_prob - market_price
    if gap < config.min_edge:
        return None

    risk = markov_risk_modifier(markov_persistence, markov_stats, market_price, config)

    net_edge = fee_adjusted_edge(true_prob, market_price, fee_rate_bps)
    req_wr = required_win_rate(market_price, fee_rate_bps)
    pr = payoff_ratio(market_price, fee_rate_bps)
    if req_wr > config.max_required_win_rate:
        return None
    if pr < config.min_payoff_ratio:
        return None
    if net_edge < config.min_edge:
        return None

    bet_size = kelly_bet_size(
        true_prob=true_prob,
        market_price=market_price,
        bankroll=bankroll,
        fraction=config.kelly_fraction,
        max_bet=config.max_bet,
        fee_rate_bps=fee_rate_bps,
    )

    if bet_size <= 0:
        return None

    # Raw fractional Kelly. Live execution later converts this dollar sanity
    # budget into a Polymarket minimum-share-lot plan; do not floor it with a
    # fake dollar MIN_BET because the real CLOB minimum is shares.
    # Markov regime is still recorded for diagnostics, but all Markov size
    # multipliers/haircuts were removed. Live sizing stays raw fractional Kelly
    # before executor-level 5-share lot planning.
    bet_size = round(bet_size, 2)

    confidence = min(gap / 0.10, 1.0)

    return TradeSignal(
        side=side,
        confidence=confidence,
        btc_delta_pct=btc_delta_pct,
        market_price=market_price,
        edge=gap,
        true_prob=true_prob,
        seconds_remaining=seconds_remaining,
        kelly_size=bet_size,
        gap=gap,
        fee_adjusted_edge=net_edge,
        fee_rate_bps=fee_rate_bps,
        markov_persistence=markov_persistence,
        markov_regime=risk.regime,
        edge_required=config.min_edge,
        payoff_ratio=pr,
        required_win_rate=req_wr,
        model_delta_pct=model_delta_pct,
    )
