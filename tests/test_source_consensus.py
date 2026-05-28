import time

from source_consensus import ReferencePrice, SourceConsensusConfig, SourceConsensusGate


def test_source_consensus_skips_when_chainlink_direction_disagrees():
    gate = SourceConsensusGate(SourceConsensusConfig(require_live_reference=True, default_basis_bps=0.0))
    ref = ReferencePrice(price=99.0, source="test", timestamp=time.time(), age_seconds=0.2)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        reference=ref,
    )

    assert decision.should_skip
    assert decision.reason == "polymarket_chainlink_direction_disagrees"


def test_source_consensus_downsizes_on_mild_stale_reference():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_live_reference=True,
            default_basis_bps=0.0,
            stale_downsize_seconds=10.0,
            stale_skip_seconds=30.0,
            downsize_factor=0.5,
        )
    )
    ref = ReferencePrice(price=101.0, source="test", timestamp=time.time() - 15.0, age_seconds=15.0)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        reference=ref,
    )

    assert decision.action == "downsize"
    assert decision.size_multiplier == 0.5
    assert decision.reason == "polymarket_chainlink_reference_mildly_stale"


def test_source_consensus_skips_on_large_basis_deviation():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_live_reference=True,
            default_basis_bps=-17.3,
            max_basis_deviation_bps=8.0,
        )
    )
    # Around -40bps vs expected -17.3bps; direction still UP, but basis is abnormal.
    ref = ReferencePrice(price=100.60, source="test", timestamp=time.time(), age_seconds=0.1)

    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        reference=ref,
    )

    assert decision.should_skip
    assert decision.reason == "basis_deviation_too_large"
    assert decision.basis_deviation_bps is not None
    assert decision.basis_deviation_bps > 8.0


def test_source_consensus_uses_adjusted_binance_side():
    gate = SourceConsensusGate(SourceConsensusConfig(require_live_reference=False, default_basis_bps=-30.0))

    decision = gate.assess(
        binance_price=100.20,
        opening_price=100.0,
        intended_side="UP",
        reference=None,
    )

    assert decision.should_skip
    assert decision.reason == "basis_adjusted_binance_side_disagrees"
    assert decision.adjusted_price < 100.0
