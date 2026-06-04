import time

from source_consensus import RtdsPrice, SourceConsensusConfig, SourceConsensusGate


def test_source_consensus_skips_when_chainlink_direction_disagrees_even_if_binance_lag_is_aligned():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=True))
    chainlink = RtdsPrice(price=99.0, source="test_chainlink", timestamp=time.time(), age_seconds=0.2)
    rtds_bn = RtdsPrice(price=101.0, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.2)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert decision.should_skip
    assert decision.reason == "chainlink_side_disagrees"
    assert decision.chainlink_delta_pct is not None
    assert decision.chainlink_delta_pct < 0.0


def test_source_consensus_skips_when_chainlink_delta_is_too_close_to_open():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            min_chainlink_delta_pct=0.02,
        )
    )
    chainlink = RtdsPrice(price=100.009, source="test_chainlink", timestamp=time.time(), age_seconds=0.2)
    rtds_bn = RtdsPrice(price=100.15, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.2)

    decision = gate.assess(
        binance_price=100.15,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert decision.should_skip
    assert decision.reason == "chainlink_delta_too_close_to_open"
    assert decision.chainlink_delta_pct is not None
    assert 0.0 < decision.chainlink_delta_pct < 0.02


def test_source_consensus_default_near_zero_gate_is_0_07pct_after_phase2_backtest():
    # Phase 2 full Chainlink-observable history showed the old 0.02% gate was
    # redundant with the strategy's 0.06% min delta. Raising the default to 0.07%
    # removes the weakest near-open entries unless explicitly overridden.
    assert SourceConsensusConfig().min_chainlink_delta_pct == 0.07


def test_source_consensus_allows_confirmed_chainlink_direction_above_min_delta():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            min_chainlink_delta_pct=0.02,
        )
    )
    chainlink = RtdsPrice(price=100.021, source="test_chainlink", timestamp=time.time(), age_seconds=0.2)
    rtds_bn = RtdsPrice(price=100.15, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.2)

    decision = gate.assess(
        binance_price=100.15,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert not decision.should_skip
    assert decision.reason == "chainlink_settlement_source_confirmed"
    assert decision.chainlink_delta_pct is not None
    assert decision.chainlink_delta_pct >= 0.02


def test_source_consensus_does_not_downsize_on_mild_stale_chainlink_below_hard_skip():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            stale_skip_seconds=30.0,
        )
    )
    chainlink = RtdsPrice(price=101.0, source="test_chainlink", timestamp=time.time() - 15.0, age_seconds=15.0)
    rtds_bn = RtdsPrice(price=101.02, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.2)

    decision = gate.assess(
        binance_price=101.02,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert decision.action == "normal"
    assert decision.reason == "chainlink_settlement_source_confirmed"


def test_source_consensus_records_but_does_not_veto_on_instant_rtds_source_gap():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=True))
    chainlink = RtdsPrice(price=100.60, source="test_chainlink", timestamp=time.time(), age_seconds=0.1)
    rtds_bn = RtdsPrice(price=101.00, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.1)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert not decision.should_skip
    assert decision.reason == "chainlink_settlement_source_confirmed"
    assert decision.source_gap_bps is not None
    assert decision.source_gap_bps > 12.0


def test_source_consensus_uses_chainlink_when_present_and_binance_only_when_chainlink_not_required():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=False))

    decision = gate.assess(
        binance_price=100.20,
        opening_price=100.0,
        intended_side="UP",
        chainlink=None,
    )

    assert decision.action == "normal"
    assert decision.reason == "chainlink_not_required"
    assert decision.signal_price == 100.20
    assert decision.signal_side == "UP"


def test_source_consensus_does_not_require_rtds_binance():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=False))

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=None,
        rtds_binance=None,
    )

    assert not decision.should_skip
    assert decision.reason == "chainlink_not_required"
