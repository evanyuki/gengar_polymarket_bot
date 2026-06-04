import time

import executor
from source_consensus import RtdsPrice, SourceConsensusConfig, SourceConsensusGate
from strategy import kelly_bet_size


def test_source_consensus_does_not_veto_when_binance_side_disagrees_but_chainlink_is_valid():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            min_chainlink_delta_pct=0.07,
        )
    )
    now = time.time()
    chainlink = RtdsPrice(price=100.08, source="chainlink", timestamp=now, age_seconds=0.1)
    rtds_binance = RtdsPrice(price=99.90, source="rtds_binance", timestamp=now, age_seconds=0.1)

    decision = gate.assess(
        binance_price=99.90,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_binance,
        binance_opening_price=100.0,
    )

    assert not decision.should_skip
    assert decision.reason == "chainlink_settlement_source_confirmed"
    assert decision.signal_side == "UP"


def test_kelly_bet_size_returns_raw_fractional_kelly_without_min_bet_floor():
    raw = kelly_bet_size(
        true_prob=0.90,
        market_price=0.80,
        bankroll=20.0,
        fraction=0.25,
        max_bet=5.0,
        fee_rate_bps=1000.0,
    )

    assert 0.0 < raw < 5.0
    assert raw != 1.0


def test_minimum_lot_plan_treats_polymarket_minimum_as_shares_not_fixed_dollars():
    plan = executor.plan_minimum_lot_order(
        price=0.80,
        raw_kelly_usd=2.70,
        minimum_order_shares=executor.POLY_MIN_ORDER_SHARES,
        max_bet_usd=5.0,
        max_floor_to_kelly_ratio=2.0,
    )

    assert plan.executable
    assert plan.shares == 5
    assert plan.amount_usd == 4.00
    assert plan.minimum_cost_usd == 4.00
    assert plan.reason == "raised_to_minimum_share_lot"


def test_minimum_lot_plan_skips_when_share_floor_breaks_kelly_sanity_check():
    plan = executor.plan_minimum_lot_order(
        price=0.88,
        raw_kelly_usd=1.40,
        minimum_order_shares=5,
        max_bet_usd=5.0,
        max_floor_to_kelly_ratio=2.0,
    )

    assert not plan.executable
    assert plan.reason == "minimum_share_lot_exceeds_kelly_ratio"
