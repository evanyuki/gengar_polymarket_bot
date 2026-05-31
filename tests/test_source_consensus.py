import time

from source_consensus import RtdsPrice, SourceConsensusConfig, SourceConsensusGate


def test_source_consensus_skips_when_chainlink_direction_disagrees_even_if_binance_lag_is_aligned():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=True, max_rtds_source_gap_bps=250.0))
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
            max_rtds_source_gap_bps=250.0,
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


def test_source_consensus_allows_confirmed_chainlink_direction_above_min_delta():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            max_rtds_source_gap_bps=250.0,
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
    assert decision.reason == "rtds_chainlink_lag_aligned"
    assert decision.chainlink_delta_pct is not None
    assert decision.chainlink_delta_pct >= 0.02


def test_source_consensus_downsizes_on_mild_stale_chainlink():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_chainlink=True,
            stale_downsize_seconds=10.0,
            stale_skip_seconds=30.0,
            downsize_factor=0.5,
            max_rtds_source_gap_bps=50.0,
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

    assert decision.action == "downsize"
    assert decision.size_multiplier == 0.5
    assert decision.reason == "polymarket_chainlink_mildly_stale"


def test_source_consensus_skips_on_instant_rtds_source_gap():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=True, max_rtds_source_gap_bps=12.0))
    chainlink = RtdsPrice(price=100.60, source="test_chainlink", timestamp=time.time(), age_seconds=0.1)
    rtds_bn = RtdsPrice(price=101.00, source="test_rtds_binance", timestamp=time.time(), age_seconds=0.1)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=chainlink,
        rtds_binance=rtds_bn,
    )

    assert decision.should_skip
    assert decision.reason == "rtds_binance_chainlink_gap_too_large"
    assert decision.source_gap_bps is not None
    assert decision.source_gap_bps > 12.0


def test_source_consensus_uses_raw_signal_price_without_synthetic_adjustment():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=False, require_rtds_binance=False))

    decision = gate.assess(
        binance_price=100.20,
        opening_price=100.0,
        intended_side="UP",
        chainlink=None,
    )

    assert decision.action == "downsize"
    assert decision.reason == "missing_chainlink_downsize"
    assert decision.signal_price == 100.20
    assert decision.signal_side == "UP"


def test_source_consensus_requires_rtds_binance_when_configured():
    gate = SourceConsensusGate(SourceConsensusConfig(require_chainlink=False, require_rtds_binance=True))

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=None,
        rtds_binance=None,
    )

    assert decision.should_skip
    assert decision.reason == "missing_polymarket_rtds_binance"
