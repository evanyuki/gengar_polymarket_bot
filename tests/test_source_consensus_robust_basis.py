import time

from source_consensus import ReferencePrice, SourceConsensusConfig, SourceConsensusGate


def test_source_consensus_uses_rolling_median_not_last_twenty_four_ticks():
    gate = SourceConsensusGate(
        SourceConsensusConfig(
            require_live_reference=True,
            default_basis_bps=-14.6,
            basis_window=5,
            min_basis_samples=3,
        )
    )

    # Four normal samples around -14.6bps plus one outlier should keep median stable.
    for basis in [-14.7, -14.6, -14.5, -14.4, -27.2]:
        binance = 100.0
        ref = binance * (1.0 + basis / 10000.0)
        gate.record_basis(ref, binance, sample_timestamp=time.time() + basis)

    assert gate.basis_sample_count == 5
    assert round(gate.basis_mean_bps, 1) == -14.6

    decision = gate.assess(
        binance_price=100.8,
        opening_price=100.0,
        intended_side="UP",
        reference=ReferencePrice(
            price=100.8 * (1.0 - 27.2 / 10000.0),
            source="test",
            timestamp=time.time(),
            age_seconds=0.2,
        ),
    )

    assert decision.should_skip
    assert decision.reason == "basis_deviation_too_large"
    assert decision.basis_deviation_bps > 8.0


def test_source_consensus_deduplicates_reference_timestamp_samples():
    gate = SourceConsensusGate(SourceConsensusConfig(default_basis_bps=-14.6, basis_window=24, min_basis_samples=2))

    ts = 123456.0
    assert gate.record_basis(99.854, 100.0, sample_timestamp=ts) is not None
    assert gate.record_basis(99.700, 100.0, sample_timestamp=ts) is None

    assert gate.basis_sample_count == 1
    assert gate.basis_mean_bps == -14.6


def test_source_consensus_background_snapshot_assesses_without_recording_new_basis():
    gate = SourceConsensusGate(SourceConsensusConfig(default_basis_bps=-14.6, min_basis_samples=1))
    now = time.time()
    ref = ReferencePrice(price=100.854, source="test", timestamp=now, age_seconds=0.2)

    snapshot = gate.update_snapshot(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        reference=ref,
    )

    before = gate.basis_sample_count
    decision = gate.assess_snapshot("UP")

    assert decision.reason == snapshot.reason
    assert gate.basis_sample_count == before
