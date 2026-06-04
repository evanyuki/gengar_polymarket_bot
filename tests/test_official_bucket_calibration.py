import csv
from pathlib import Path

from scripts.calibrate_official_buckets import calibrate_rows, load_trade_rows


def test_calibrate_rows_uses_official_settlement_and_payoff_math(tmp_path):
    path = tmp_path / "trades.csv"
    fields = [
        "mode", "entry_price", "prob_at_entry", "entry_delta_pct",
        "entry_seconds_remaining", "fee_rate_bps", "official_winning_side",
        "side", "won_resolution", "profit",
    ]
    rows = [
        {"mode": "LIVE", "entry_price": "0.80", "prob_at_entry": "0.91", "entry_delta_pct": "0.12", "entry_seconds_remaining": "100", "fee_rate_bps": "0", "official_winning_side": "UP", "side": "UP", "won_resolution": "True", "profit": "1.0"},
        {"mode": "LIVE", "entry_price": "0.80", "prob_at_entry": "0.91", "entry_delta_pct": "0.13", "entry_seconds_remaining": "90", "fee_rate_bps": "0", "official_winning_side": "DOWN", "side": "UP", "won_resolution": "False", "profit": "-4.0"},
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    loaded = load_trade_rows(path)
    summary = calibrate_rows(loaded, min_n=1)

    assert summary["overall"]["n"] == 2
    assert summary["overall"]["wins"] == 1
    assert summary["overall"]["win_rate"] == 0.5
    assert summary["overall"]["avg_required_wr"] == 0.8
    assert summary["overall"]["avg_payoff_ratio"] == 0.25
    assert summary["overall"]["ev_vs_required_wr"] == -0.3
    assert summary["buckets"]
