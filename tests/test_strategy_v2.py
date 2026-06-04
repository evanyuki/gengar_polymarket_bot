import math

from strategy import (
    StrategyConfig,
    evaluate,
    estimate_true_probability,
    get_skip_reason,
    kelly_fraction,
)


def _momentum_config():
    return StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
        require_momentum_align=True,
    )


_MOMENTUM_MARKOV = {"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500}


def _evaluate_up(cfg, momentum_15s_pct):
    # UP signal: btc above open. Momentum sign decides whether the move is still
    # pushing the signal side (aligned) or fading (against).
    return evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.62,
        down_market_price=0.38,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
        markov_stats=_MOMENTUM_MARKOV,
        fee_rate_bps=0,
        momentum_15s_pct=momentum_15s_pct,
    )


def test_momentum_gate_allows_entry_when_move_still_pushing_signal_side():
    cfg = _momentum_config()
    signal = _evaluate_up(cfg, momentum_15s_pct=0.03)  # UP move still pushing up
    assert signal is not None
    assert signal.side == "UP"


def test_momentum_gate_blocks_entry_when_move_is_fading():
    cfg = _momentum_config()
    signal = _evaluate_up(cfg, momentum_15s_pct=-0.03)  # move reversing against UP
    assert signal is None
    reason = get_skip_reason(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.62,
        down_market_price=0.38,
        seconds_remaining=120,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
        markov_stats=_MOMENTUM_MARKOV,
        fee_rate_bps=0,
        momentum_15s_pct=-0.03,
    )
    assert reason == "momentum_not_aligned"


def test_momentum_gate_blocks_entry_when_momentum_flat():
    cfg = _momentum_config()
    # Exactly flat counts as not-confirming (no push in the signal direction).
    assert _evaluate_up(cfg, momentum_15s_pct=0.0) is None


def test_momentum_gate_off_ignores_momentum():
    cfg = _momentum_config()
    cfg.require_momentum_align = False
    # Fading momentum must NOT block when the gate is disabled (backward compat).
    assert _evaluate_up(cfg, momentum_15s_pct=-0.03) is not None
    # And a None momentum value is always ignored regardless of the flag.
    cfg.require_momentum_align = True
    assert _evaluate_up(cfg, momentum_15s_pct=None) is not None


def test_entry_uses_model_probability_minus_market_price_gap():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
    )

    signal = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.62,
        down_market_price=0.38,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
        markov_stats={"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=0,
    )

    assert signal is not None
    expected_gap = signal.true_prob - signal.market_price
    assert signal.edge == expected_gap
    assert signal.gap == expected_gap
    assert signal.gap >= cfg.min_edge


def test_entry_probability_uses_chainlink_price_when_probability_price_is_supplied():
    cfg = StrategyConfig(
        min_edge=0.0,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
    )

    signal = evaluate(
        btc_price=100.154,
        probability_price=100.021,
        opening_price=100.0,
        up_market_price=0.50,
        down_market_price=0.50,
        seconds_remaining=173.1,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.12,
        markov_persistence=0.90,
        markov_stats={"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=0,
    )

    assert signal is not None
    assert signal.side == "UP"
    assert math.isclose(signal.btc_delta_pct, 0.154, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(signal.model_delta_pct, 0.021, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(signal.true_prob, estimate_true_probability(0.021, 173.1, vol=0.12), rel_tol=1e-12)


def test_entry_rejects_when_chainlink_probability_side_disagrees_with_binance_signal_side():
    cfg = StrategyConfig(
        min_edge=0.0,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
    )

    signal = evaluate(
        btc_price=100.154,
        probability_price=99.99,
        opening_price=100.0,
        up_market_price=0.50,
        down_market_price=0.50,
        seconds_remaining=173.1,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.12,
        markov_persistence=0.90,
        markov_stats={"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=0,
    )

    assert signal is None


def test_entry_rejects_when_gap_below_epsilon_even_if_probability_high():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
    )

    signal = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.93,
        down_market_price=0.18,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
    )

    assert signal is None


def test_markov_low_persistence_is_logged_not_used_as_entry_gate():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        markov_persistence_threshold=0.87,
    )

    signal = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.55,
        down_market_price=0.45,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.625,
        markov_stats={"same": 5, "total": 8, "directional_samples": 10, "flat_samples": 500},
    )

    assert signal is not None
    assert signal.markov_regime == "markov_low_persistence"
    assert signal.edge_required == cfg.min_edge


def test_kelly_fraction_uses_explicit_formula_p_minus_one_minus_p_over_b():
    p = 0.80
    price = 0.60
    b = (1.0 - price) / price
    expected = p - (1.0 - p) / b

    assert math.isclose(kelly_fraction(p, price), expected, rel_tol=1e-12)


def test_fee_aware_kelly_reduces_position_size():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
    )

    no_fee = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.55,
        down_market_price=0.45,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
        markov_stats={"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=0,
    )
    with_fee = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.55,
        down_market_price=0.45,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.90,
        markov_stats={"same": 9, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=1000,
    )

    assert no_fee is not None
    assert with_fee is not None
    assert with_fee.fee_adjusted_edge < no_fee.fee_adjusted_edge
    assert with_fee.kelly_size < no_fee.kelly_size
