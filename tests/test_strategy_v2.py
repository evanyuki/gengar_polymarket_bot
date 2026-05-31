import math

from strategy import StrategyConfig, evaluate, estimate_true_probability, kelly_fraction


def test_entry_uses_model_probability_minus_market_price_gap():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_btc_delta=0.0,
        min_price=0.01,
        max_price=0.99,
        min_bet=1.0,
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
        min_btc_delta=0.0,
        min_price=0.01,
        max_price=0.99,
        min_bet=0.0,
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
        min_btc_delta=0.0,
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
        min_btc_delta=0.0,
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


def test_markov_low_persistence_blocks_only_when_sample_is_sufficient():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_btc_delta=0.0,
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

    assert signal is None


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
        min_btc_delta=0.0,
        min_price=0.01,
        max_price=0.99,
        min_bet=0.0,
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
