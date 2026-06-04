import math

from strategy import (
    StrategyConfig,
    effective_market_price,
    evaluate,
    fee_adjusted_edge,
    get_skip_reason,
)


def test_polymarket_fee_formula_adds_fee_rate_times_price_times_one_minus_price():
    price = 0.89
    fee_rate_bps = 1000  # 10% feeRate from CLOB metadata, expressed as bps.

    expected = price + (fee_rate_bps / 10_000.0) * price * (1.0 - price)

    assert math.isclose(effective_market_price(price, fee_rate_bps), expected, rel_tol=1e-12)
    assert math.isclose(fee_adjusted_edge(0.96, price, fee_rate_bps), 0.96 - expected, rel_tol=1e-12)


def test_fee_adjusted_kelly_does_not_treat_fee_rate_as_simple_price_markup():
    price = 0.89
    fee_rate_bps = 1000

    docs_formula_price = effective_market_price(price, fee_rate_bps)
    simple_markup_price = price * (1.0 + fee_rate_bps / 10_000.0)

    assert docs_formula_price < simple_markup_price
    assert docs_formula_price < 0.90


def test_evaluate_rejects_signal_when_fee_adjusted_kelly_is_zero():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=5.0,
        markov_persistence_threshold=0.0,
    )

    signal = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.89,
        down_market_price=0.11,
        seconds_remaining=120,
        bankroll=25.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=1.0,
        fee_rate_bps=10000,  # Deliberately punitive: raw edge passes, fee-adjusted Kelly should not.
    )

    assert signal is None


def test_skip_reason_reports_fee_adjusted_kelly_below_min_before_buy_attempt():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=5.0,
        markov_persistence_threshold=0.0,
    )

    reason = get_skip_reason(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.89,
        down_market_price=0.11,
        seconds_remaining=120,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=1.0,
        fee_rate_bps=10000,
    )

    assert reason == "kelly_below_min"
