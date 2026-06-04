import csv

from source_consensus import SourceConsensusDecision
from tracker import Tracker


def test_tracker_logs_gate_level_tick_diagnostics(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))
    decision = SourceConsensusDecision(
        action="pass",
        reason="source_consensus_ok",
        signal_price=101.0,
        signal_side="UP",
        chainlink_price=100.9,
        chainlink_age_seconds=1.2,
        source_gap_bps=1.0,
        direct_vs_rtds_binance_gap_bps=0.5,
    )

    tracker.log_gate_tick(
        window_ts=1710000000,
        btc_price=101.0,
        signal_btc_price=101.0,
        opening_price=100.0,
        up_price=0.99,
        down_price=0.01,
        seconds_remaining=120.0,
        candidate_side="UP",
        candidate_market_price=0.99,
        opposite_market_price=0.01,
        true_prob=0.99,
        raw_edge=0.0,
        fee_adjusted_edge=-0.001,
        kelly_size=0.0,
        markov_persistence=0.0,
        markov_stats={"same": 0, "total": 0, "directional_samples": 1, "flat_samples": 500},
        markov_threshold=0.87,
        realized_vol=0.12,
        fee_rate_bps=0.0,
        source_decision=decision,
        gate_reason="markov_persistence_below_threshold",
        signal_ready=False,
        extreme_book=True,
        book_state="target_extreme_high_no_margin",
    )

    with (tmp_path / "gate_ticks.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["signal_btc_price"] == "101.0"
    assert row["gate_reason"] == "markov_persistence_below_threshold"
    assert row["book_state"] == "target_extreme_high_no_margin"
    assert row["extreme_book"] == "1"
    assert row["markov_flat_samples"] == "500"
    assert row["chainlink_age_seconds"] == "1.2"
