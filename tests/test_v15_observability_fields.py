import csv
from pathlib import Path

import bot
from executor import FILLED, OrderResult
from source_consensus import SourceConsensusDecision
from strategy import TradeSignal
from tracker import Tracker, GATE_TICK_FIELDS, TRADE_FIELDS


def _last_csv_row(path: Path) -> dict[str, str]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows
    return rows[-1]


def test_gate_tick_logs_chainlink_rtds_binance_and_signal_replay_fields(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))
    decision = SourceConsensusDecision(
        action="normal",
        reason="rtds_chainlink_lag_aligned",
        signal_price=100.12,
        signal_side="UP",
        chainlink_price=100.12,
        chainlink_age_seconds=0.25,
        rtds_binance_price=100.30,
        rtds_binance_age_seconds=0.15,
        source_gap_bps=17.98,
        direct_vs_rtds_binance_gap_bps=1.0,
        chainlink_delta_pct=0.12,
        rtds_binance_delta_pct=0.20,
    )

    tracker.log_gate_tick(
        window_ts=1710000000,
        btc_price=100.29,
        signal_btc_price=100.12,
        opening_price=100.0,
        up_price=0.72,
        down_price=0.28,
        seconds_remaining=150.0,
        candidate_side="UP",
        candidate_market_price=0.72,
        opposite_market_price=0.28,
        true_prob=0.91,
        raw_edge=0.19,
        fee_adjusted_edge=0.18,
        kelly_size=5.0,
        markov_persistence=0.0,
        source_decision=decision,
        signal_source="polymarket_rtds_chainlink",
        signal_price=100.12,
        signal_opening_price=100.0,
        signal_delta_pct=0.12,
        signal_side="UP",
        chainlink_price=100.12,
        chainlink_open_price=100.0,
        chainlink_delta_pct=0.12,
        rtds_binance_price=100.30,
        rtds_binance_open_price=100.10,
        rtds_binance_delta_pct=0.20,
    )

    row = _last_csv_row(tmp_path / "gate_ticks.csv")
    for field in [
        "chainlink_price",
        "chainlink_open_price",
        "chainlink_delta_pct",
        "rtds_binance_price",
        "rtds_binance_open_price",
        "rtds_binance_delta_pct",
        "signal_source",
        "signal_price",
        "signal_opening_price",
        "signal_delta_pct",
        "signal_side",
    ]:
        assert field in GATE_TICK_FIELDS
        assert row[field] not in ("", "0", "0.0")
    assert row["signal_source"] == "polymarket_rtds_chainlink"
    assert row["signal_side"] == "UP"
    assert float(row["chainlink_delta_pct"]) == 0.12
    assert float(row["rtds_binance_delta_pct"]) == 0.20


def test_trade_entry_logs_entry_source_prices_and_deltas(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))

    tracker.log_trade_entry(
        window_ts=1710000000,
        side="UP",
        entry_price=0.72,
        entry_shares=5.0,
        entry_cost=3.60,
        edge=0.19,
        prob=0.91,
        btc_delta=0.12,
        seconds_remaining=150.0,
        entry_delta_pct=0.12,
        entry_seconds_remaining=150.0,
        entry_signal_source="polymarket_rtds_chainlink",
        entry_signal_price=100.12,
        entry_signal_open_price=100.0,
        entry_signal_delta_pct=0.12,
        entry_chainlink_price=100.12,
        entry_chainlink_delta_pct=0.12,
        entry_rtds_binance_price=100.30,
        entry_rtds_binance_delta_pct=0.20,
    )
    tracker.log_trade_resolve(
        btc_final_price=100.20,
        opening_price=100.0,
        won=True,
        profit=1.40,
        final_price_source="polymarket_crypto_price",
        official_open_price=100.0,
        official_close_price=100.20,
        official_completed=True,
    )

    row = _last_csv_row(tmp_path / "trades.csv")
    for field in [
        "entry_signal_source",
        "entry_signal_price",
        "entry_signal_open_price",
        "entry_signal_delta_pct",
        "entry_chainlink_price",
        "entry_chainlink_delta_pct",
        "entry_rtds_binance_price",
        "entry_rtds_binance_delta_pct",
    ]:
        assert field in TRADE_FIELDS
        assert row[field] not in ("", "0", "0.0")
    assert row["entry_signal_source"] == "polymarket_rtds_chainlink"
    assert float(row["entry_signal_delta_pct"]) == 0.12


def test_execute_trade_passes_source_observability_to_trade_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    monkeypatch.setenv("ENTRY_MAX_SPREAD", "0.20")
    monkeypatch.setenv("ENTRY_MIN_EXIT_PRICE", "0.01")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000300
    polybot._opening_price = 100.10
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01
    polybot._last_real_balance = 20.0

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class FakeExecutor:
        _initialized = True

        def get_market_price(self, token_id, side, amount):
            return 0.70

        def buy(self, token_id, amount_usd, price=0.0, balance_hint=0.0):
            return OrderResult(True, "ok", FILLED, "BUY", price, amount_usd, 5.0, token_id, False)

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision(
                "normal",
                "rtds_chainlink_lag_aligned",
                100.12,
                side,
                100.12,
                0.25,
                rtds_binance_price=100.30,
                rtds_binance_age_seconds=0.15,
                chainlink_delta_pct=0.12,
                rtds_binance_delta_pct=0.20,
            )

    class FakeTracker:
        def __init__(self):
            self.signals = []
            self.trade_entries = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

        def log_trade_entry(self, **kwargs):
            self.trade_entries.append(kwargs)

    class FakeTelegram:
        def trade_alert(self, *args, **kwargs):
            pass

    monkeypatch.setattr(bot, "get_current_market", lambda period, include_open_price=False: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()
    polybot.telegram = FakeTelegram()

    sig = TradeSignal("UP", 0.92, 0.12, 0.70, 0.22, 0.92, 150.0, 5.0, edge_required=0.06)
    sig.entry_signal_source = "polymarket_rtds_chainlink"
    sig.entry_signal_price = 100.12
    sig.entry_signal_open_price = 100.0
    sig.entry_signal_delta_pct = 0.12

    polybot._execute_trade(sig, seconds_remaining=150.0)

    entry = polybot.tracker.trade_entries[-1]
    assert entry["entry_signal_source"] == "polymarket_rtds_chainlink"
    assert entry["entry_signal_price"] == 100.12
    assert entry["entry_signal_open_price"] == 100.0
    assert entry["entry_signal_delta_pct"] == 0.12
    assert entry["entry_chainlink_price"] == 100.12
    assert entry["entry_chainlink_delta_pct"] == 0.12
    assert entry["entry_rtds_binance_price"] == 100.30
    assert entry["entry_rtds_binance_delta_pct"] == 0.20
