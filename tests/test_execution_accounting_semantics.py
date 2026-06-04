import csv
from pathlib import Path

from executor import Executor
from clob_orderbook_cache import ClobOrderBookCache
from source_consensus import SourceConsensusDecision
from strategy import HourlyStats, TradeSignal
from telegram_notifier import TelegramNotifier
from tracker import Tracker, TRADE_FIELDS
import bot


def read_csv(path: Path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


class BalanceSequenceExecutor(Executor):
    def __init__(self, balances):
        super().__init__(private_key="", safe_address="", dry_run=False)
        self._initialized = True
        self._balances = list(balances)
        self.client = object()

    def get_collateral_balance(self):
        return self._balances.pop(0)

    def _check_order(self, order_id):
        return None


def test_balance_verified_buy_keeps_intended_shares_not_fee_inflated_spent_over_price():
    executor = BalanceSequenceExecutor([95.99194])

    result = executor._verify_buy_via_balance(
        order_id="order-1",
        price=0.79,
        shares=5.0,
        token_id="TOKEN1234567890",
        balance_before=100.0,
    )

    assert result.success is True
    assert result.shares == 5.0
    assert round(result.amount_usd, 5) == 4.00806
    assert round(result.planned_order_notional_usd, 2) == 3.95
    assert round(result.actual_cash_spent_usd, 5) == 4.00806
    assert round(result.estimated_fee_usd, 5) == 0.05806


def test_dry_run_executor_uses_integer_shares_and_planned_notional():
    executor = Executor(private_key="", safe_address="", dry_run=True)

    result = executor.buy(token_id="DRY-UP-1", amount_usd=3.95, price=0.79)

    assert result.success is True
    assert result.price == 0.79
    assert result.shares == 5.0
    assert result.amount_usd == 3.95
    assert result.planned_order_notional_usd == 3.95
    assert result.actual_cash_spent_usd == 3.95


def test_tracker_trade_entry_separates_raw_kelly_planned_notional_cash_spent_and_fee(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))
    tracker.log_trade_entry(
        window_ts=1710000000,
        side="UP",
        entry_price=0.79,
        entry_shares=5.0,
        entry_cost=4.00806,
        edge=0.10,
        prob=0.89,
        btc_delta=0.12,
        seconds_remaining=120,
        mode="LIVE",
        raw_kelly_usd=1.44,
        planned_order_notional_usd=3.95,
        actual_cash_spent_usd=4.00806,
        estimated_fee_usd=0.05806,
        sizing_reason="raised_to_minimum_share_lot",
    )
    tracker.log_trade_resolve(
        btc_final_price=101.0,
        opening_price=100.0,
        won=True,
        profit=0.99194,
    )

    row = read_csv(tmp_path / "trades.csv")[0]
    assert row["raw_kelly_usd"] == "1.44"
    assert row["planned_order_notional_usd"] == "3.95"
    assert row["actual_cash_spent_usd"] == "4.01"
    assert row["estimated_fee_usd"] == "0.06"
    assert row["sizing_reason"] == "raised_to_minimum_share_lot"


class CapturingTelegram(TelegramNotifier):
    def __init__(self):
        self.enabled = True
        self.messages = []

    def send(self, message: str, silent: bool = False):
        self.messages.append(message)


def test_telegram_trade_alert_uses_cash_spent_raw_kelly_planned_lot_and_fee_language():
    tg = CapturingTelegram()

    tg.trade_alert(
        side="UP",
        price=0.79,
        amount=4.00806,
        market_slug="btc-updown-test",
        dry_run=False,
        edge=0.10,
        raw_kelly_usd=1.44,
        planned_order_notional_usd=3.95,
        actual_cash_spent_usd=4.00806,
        estimated_fee_usd=0.05806,
        shares=5,
        sizing_reason="raised_to_minimum_share_lot",
    )

    msg = tg.messages[0]
    assert "Cash spent: $4.01 incl fee" in msg
    assert "Raw Kelly: $1.44" in msg
    assert "Order: 5 shares @ $0.7900 = $3.95" in msg
    assert "Fee est: $0.06" in msg
    assert "raised_to_minimum_share_lot" in msg
    assert "Amount:" not in msg


def test_hourly_stats_tracks_avg_entry_price_separately_from_avg_edge():
    h = HourlyStats()
    h.record_trade(edge=0.10, delta=0.1, entry_price=0.79)
    h.record_trade(edge=0.20, delta=-0.2, entry_price=0.85)

    assert round(h.avg_edge, 4) == 0.15
    assert round(h.avg_entry_price, 4) == 0.82
    assert h.to_dict()["avg_entry_price"] == h.avg_entry_price


def test_dry_run_execute_trade_uses_real_market_min_lot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_BET", "5.0")
    monkeypatch.setenv("ENTRY_MIN_SIZE_KELLY_RATIO", "3.0")
    monkeypatch.setenv("ENTRY_MAX_SPREAD", "0.08")
    monkeypatch.setenv("ENTRY_MIN_EXIT_PRICE", "0.50")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000000
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.79
    polybot._cached_down = 0.22
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"
        condition_id = "condition"

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision(
                action="normal",
                reason="source_consensus_ok",
                signal_price=100.2,
                signal_side=side,
                chainlink_price=100.2,
                chainlink_age_seconds=0.1,
            )

    class FakeTracker:
        def __init__(self):
            self.signals = []
            self.entries = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

        def log_trade_entry(self, **kwargs):
            self.entries.append(kwargs)

    class FakeTelegram:
        def __init__(self):
            self.trade_alerts = []

        def trade_alert(self, **kwargs):
            self.trade_alerts.append(kwargs)

    monkeypatch.setattr(bot, "get_current_market", lambda *args, **kwargs: FakeMarket())
    polybot._current_market = FakeMarket()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()
    polybot.telegram = FakeTelegram()
    polybot.executor = Executor(private_key="", safe_address="", dry_run=True)
    cache = ClobOrderBookCache(max_book_age_seconds=10.0)
    cache.apply_book(
        token_id="UPTOKEN",
        bids=[{"price": "0.77", "size": "20"}],
        asks=[{"price": "0.79", "size": "20"}],
    )
    polybot.orderbook_cache = cache
    polybot._orderbook_cache_enabled = True

    sig = TradeSignal(
        side="UP",
        confidence=0.90,
        btc_delta_pct=0.20,
        market_price=0.79,
        edge=0.11,
        true_prob=0.90,
        seconds_remaining=120.0,
        kelly_size=1.44,
        gap=0.11,
        fee_adjusted_edge=0.11,
        fee_rate_bps=0.0,
        markov_persistence=0.9,
    )

    polybot._execute_trade(sig, seconds_remaining=120.0)

    assert polybot._traded is True
    assert polybot._trade_token_id == "UPTOKEN"
    assert polybot._trade_shares == 5.0
    assert polybot._trade_cost == 3.95
    entry = polybot.tracker.entries[0]
    assert entry["raw_kelly_usd"] == 1.44
    assert entry["planned_order_notional_usd"] == 3.95
    assert entry["actual_cash_spent_usd"] == 3.95
    assert entry["sizing_reason"] == "raised_to_minimum_share_lot"
    assert polybot.telegram.trade_alerts[0]["shares"] == 5.0
