import csv
from pathlib import Path

from tracker import Tracker
from telegram_notifier import TelegramNotifier


def read_csv(path: Path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_tracker_logs_dry_run_session_with_markov_state_and_threshold_win_rate(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))

    tracker.log_dry_run_session(
        window_ts=1710000000,
        window_end_ts=1710000300,
        signals_detected=1,
        traded=True,
        side="UP",
        entry_price=0.62,
        entry_cost=10.0,
        entry_shares=16.129,
        markov_state="UP|p=0.91|threshold=0.87|PASS",
        markov_persistence=0.91,
        markov_threshold=0.87,
        simulated_profit=6.13,
        won_resolution=True,
        opening_price=70000.0,
        final_price=70100.0,
        btc_final_delta_pct=0.1429,
        threshold_trades=3,
        threshold_wins=2,
    )

    rows = read_csv(tmp_path / "dry_run_sessions.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["signals_detected"] == "1"
    assert row["entry_price"] == "0.62"
    assert row["markov_state"] == "UP|p=0.91|threshold=0.87|PASS"
    assert row["simulated_profit"] == "6.13"
    assert row["threshold_win_rate"] == "66.7"


def test_telegram_six_hour_summary_includes_dry_run_metrics(monkeypatch):
    sent = []
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")
    monkeypatch.setattr(notifier, "send", lambda message, silent=False: sent.append(message))

    notifier.six_hour_dry_run_summary(
        window={
            "hours": 6.0,
            "signals": 7,
            "trades": 5,
            "wins": 4,
            "losses": 1,
            "win_rate": 80.0,
            "pnl": 12.34,
            "threshold_trades": 5,
            "threshold_win_rate": 80.0,
        },
        overall={
            "hours": 24.0,
            "signals": 22,
            "trades": 17,
            "wins": 11,
            "losses": 6,
            "win_rate": 64.7,
            "pnl": 18.5,
            "threshold_trades": 17,
            "threshold_win_rate": 64.7,
        },
    )

    assert sent
    assert "6H DRY RUN SUMMARY" in sent[0]
    assert "Signals: 7" in sent[0]
    assert "p(j*,j*) WR: 80.0% (5 trades)" in sent[0]
    assert "Total P&L: $+18.50" in sent[0]
