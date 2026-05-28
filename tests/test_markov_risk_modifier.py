import math

from strategy import StrategyConfig, evaluate, get_skip_reason


def _risk_config():
    return StrategyConfig(
        min_edge=0.05,
        min_prob=0.86,
        min_btc_delta=0.06,
        min_price=0.50,
        max_price=0.90,
        min_bet=1.0,
        max_bet=10.0,
        kelly_fraction=0.25,
        markov_persistence_threshold=0.87,
        markov_medium_threshold=0.75,
        markov_weak_min_transitions=5,
        markov_medium_edge=0.07,
        markov_weak_edge=0.08,
        markov_insufficient_edge=0.10,
        markov_medium_size_multiplier=0.50,
        markov_weak_size_multiplier=0.35,
        markov_insufficient_size_multiplier=0.25,
        high_price_edge_buffer_threshold=0.80,
        high_price_min_edge=0.08,
    )


def test_markov_insufficient_sample_no_longer_hard_blocks_thick_edge_but_downsizes():
    cfg = _risk_config()

    signal = evaluate(
        btc_price=100.20,
        opening_price=100.0,
        up_market_price=0.70,
        down_market_price=0.31,
        seconds_remaining=200,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.10,
        markov_persistence=0.0,
        markov_stats={"same": 3, "total": 3, "directional_samples": 5, "flat_samples": 500},
        fee_rate_bps=0.0,
    )

    assert signal is not None
    assert signal.markov_regime == "markov_insufficient_sample"
    assert math.isclose(signal.edge_required, 0.10)
    assert math.isclose(signal.markov_size_multiplier, 0.25)
    assert math.isclose(signal.kelly_size, 2.5)


def test_markov_medium_high_price_requires_extra_edge_to_avoid_thin_0_80_plus_entries():
    cfg = _risk_config()

    signal = evaluate(
        btc_price=100.079,
        opening_price=100.0,
        up_market_price=0.84,
        down_market_price=0.17,
        seconds_remaining=236.4,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.0707,
        markov_persistence=0.80,
        markov_stats={"same": 8, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=700.0,
    )

    assert signal is None
    reason = get_skip_reason(
        btc_price=100.079,
        opening_price=100.0,
        up_market_price=0.84,
        down_market_price=0.17,
        seconds_remaining=236.4,
        config=cfg,
        realized_vol=0.0707,
        markov_persistence=0.80,
        markov_stats={"same": 8, "total": 10, "directional_samples": 12, "flat_samples": 500},
        fee_rate_bps=700.0,
    )
    assert reason == "markov_edge_buffer_below_required"


def test_markov_medium_allows_normal_price_thick_edge_with_half_size():
    cfg = _risk_config()

    signal = evaluate(
        btc_price=99.897,
        opening_price=100.0,
        up_market_price=0.32,
        down_market_price=0.69,
        seconds_remaining=233.4,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.072,
        markov_persistence=7 / 9,
        markov_stats={"same": 7, "total": 9, "directional_samples": 14, "flat_samples": 500},
        fee_rate_bps=700.0,
    )

    assert signal is not None
    assert signal.side == "DOWN"
    assert signal.markov_regime == "markov_medium"
    assert math.isclose(signal.edge_required, 0.07)
    assert math.isclose(signal.markov_size_multiplier, 0.5)
    assert 1.0 <= signal.kelly_size <= 5.0
