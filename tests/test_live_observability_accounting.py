import csv
from pathlib import Path

import bot
from executor import FILLED, OrderResult
from source_consensus import SourceConsensusDecision
from strategy import TradeSignal, estimate_true_probability
from tracker import Tracker


def read_csv(path: Path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_dry_run_trade_lifecycle_is_separated_from_live_trades_csv(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))

    tracker.log_trade_entry(
        window_ts=1710000000,
        side="UP",
        entry_price=0.54,
        entry_shares=9.3,
        entry_cost=5.0,
        edge=0.37,
        prob=0.91,
        btc_delta=0.143,
        seconds_remaining=240.0,
        mode="DRY",
    )
    tracker.log_trade_resolve(
        btc_final_price=75509.72,
        opening_price=75351.96,
        won=True,
        profit=4.3,
        exit_revenue=0.0,
        resolution_method="dry_binance_fallback",
        claim_result="not_attempted",
    )

    assert read_csv(tmp_path / "trades.csv") == []
    dry_rows = read_csv(tmp_path / "dry_run_trades.csv")
    assert len(dry_rows) == 1
    assert dry_rows[0]["mode"] == "DRY"
    assert dry_rows[0]["resolution_method"] == "dry_binance_fallback"


def test_live_trade_lifecycle_stays_in_live_trades_csv(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))

    tracker.log_trade_entry(
        window_ts=1710000000,
        side="DOWN",
        entry_price=0.59,
        entry_shares=8.0,
        entry_cost=4.72,
        edge=0.30,
        prob=0.89,
        btc_delta=-0.10,
        seconds_remaining=100.0,
        mode="LIVE",
    )
    tracker.log_trade_resolve(
        btc_final_price=75000.0,
        opening_price=75100.0,
        won=True,
        profit=3.28,
        exit_revenue=8.0,
        resolution_method="claim_sell",
        claim_result="filled",
    )

    live_rows = read_csv(tmp_path / "trades.csv")
    assert len(live_rows) == 1
    assert live_rows[0]["mode"] == "LIVE"
    assert not (tmp_path / "dry_run_trades.csv").exists() or read_csv(tmp_path / "dry_run_trades.csv") == []


def test_resolution_bankroll_uses_gross_payout_not_payout_plus_profit():
    # After a $5 dry-run buy from a $25 bankroll, bankroll is $20.
    # Winning 9.3 shares should settle to $29.30, not $33.60.
    assert bot.compute_resolution_bankroll(
        bankroll_before_resolution=20.0,
        total_received=9.3,
    ) == 29.3


def test_losing_resolution_does_not_charge_entry_cost_twice():
    # After a $5 buy from a $25 bankroll, a complete loss leaves $20,
    # because the entry cost was already deducted when the position opened.
    assert bot.compute_resolution_bankroll(
        bankroll_before_resolution=20.0,
        total_received=0.0,
    ) == 20.0


def test_live_low_market_price_does_not_resolve_without_claim_or_balance_truth(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()

    class FakeTelegram:
        def loss_alert(self, *args, **kwargs):
            pass

    class FakePriceFeed:
        def get_price(self):
            return 75334.55, True

    class FakeTracker:
        def __init__(self):
            self.resolve_calls = []

        def log_trade_resolve(self, **kwargs):
            self.resolve_calls.append(kwargs)

    polybot.telegram = FakeTelegram()
    polybot.price_feed = FakePriceFeed()
    polybot.tracker = FakeTracker()

    polybot.stats.bankroll = 19.31
    polybot._opening_price = 75311.84
    polybot._trade_side = "DOWN"
    polybot._trade_cost = 5.53
    polybot._trade_shares = 7.0
    polybot._exit_revenue = 0.0
    polybot._last_sell_price_seen = 0.01
    polybot._trade_token_id = "LIVE_TOKEN"

    class FakeExecutor:
        _initialized = False

    polybot.executor = FakeExecutor()

    polybot._resolve_previous_trade()

    assert polybot.stats.total_pnl == 0.0
    assert polybot.stats.bankroll == 19.31
    assert polybot.tracker.resolve_calls == []


def test_live_claim_revenue_is_logged_as_resolution_exit_revenue(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()

    class FakeTelegram:
        def win_alert(self, *args, **kwargs):
            pass

    class FakePriceFeed:
        def get_price(self):
            return 75050.0, True

    class FakeTracker:
        def __init__(self):
            self.resolve_calls = []

        def log_trade_resolve(self, **kwargs):
            self.resolve_calls.append(kwargs)

    polybot.telegram = FakeTelegram()
    polybot.price_feed = FakePriceFeed()
    polybot.tracker = FakeTracker()
    polybot.stats.bankroll = 19.0
    polybot._opening_price = 75000.0
    polybot._exit_revenue = 0.0

    polybot._record_resolution(
        won=True,
        original_cost=5.94,
        remaining_shares=9.0,
        resolution_method="claim_sell",
        claim_revenue=8.90,
        claim_result="filled",
    )

    assert polybot.tracker.resolve_calls[0]["exit_revenue"] == 8.90
    assert polybot.tracker.resolve_calls[0]["claim_result"] == "filled"


def test_reverse_orderbook_pre_entry_gate_skips_live_buy(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()
    polybot._current_window = 1710000000
    polybot._opening_price = 75000.0
    polybot._chainlink_open_price = 75000.0
    polybot._cached_up = 0.54
    polybot._cached_down = 0.47

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class FakeClient:
        def get_ok(self):
            return True

    class FakeExecutor:
        _initialized = True
        client = FakeClient()

        def get_market_price(self, token_id, side, amount):
            return 0.54 if side == "BUY" else 0.43

        def buy(self, *args, **kwargs):
            raise AssertionError("buy should be skipped on reverse/thin orderbook")

    class FakePriceFeed:
        def get_price(self):
            return 75300.0, True

    class FakeReference:
        price = 75220.0
        source = "test_chainlink"
        timestamp = 1710000000.0
        age_seconds = 0.1

    class FakeRtdsBinance:
        price = 75300.0
        source = "test_rtds_binance"
        timestamp = 1710000000.0
        age_seconds = 0.1

    class FakeReferenceFeed:
        def get_latest(self):
            return FakeReference()

        def get_binance_latest(self):
            return FakeRtdsBinance()

        def update_from_rest(self, window_ts, window_end_ts):
            return FakeReference()

    class FakeTracker:
        def __init__(self):
            self.signals = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision("normal", "ok", 75220.0, side, 75220.0, 0.1)

    monkeypatch.setattr(bot, "get_current_market", lambda period: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot._orderbook_cache_enabled = False
    polybot.price_feed = FakePriceFeed()
    polybot.rtds_feed = FakeReferenceFeed()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()

    sig = TradeSignal(
        side="UP",
        confidence=0.95,
        btc_delta_pct=0.156,
        market_price=0.54,
        edge=0.41,
        true_prob=0.95,
        seconds_remaining=190.0,
        kelly_size=5.0,
        gap=0.41,
        fee_adjusted_edge=0.41,
        fee_rate_bps=0.0,
        markov_persistence=0.9,
    )

    polybot._execute_trade(sig, seconds_remaining=190.0)

    assert polybot._traded is False
    assert polybot.tracker.signals[-1]["action"] == "skipped_reverse_orderbook"
    assert polybot.tracker.signals[-1]["skip_reason"] == "reverse_orderbook_before_entry"


def test_final_prebuy_recheck_uses_same_realized_vol_as_signal_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MIN_PROB", "0.86")
    monkeypatch.setenv("ENTRY_MAX_SPREAD", "0.10")
    monkeypatch.setenv("ENTRY_MIN_EXIT_PRICE", "0.50")
    monkeypatch.setenv("ENTRY_HUGE_EDGE_MIN", "0.05")
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000300
    polybot._opening_price = 100.0
    polybot._cached_up = 0.24
    polybot._cached_down = 0.76
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01
    polybot._compute_realized_vol = lambda: 0.0835

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class FakeClient:
        def get_ok(self):
            return True

    class FakeExecutor:
        _initialized = True
        client = FakeClient()

        def __init__(self):
            self.buy_calls = []

        def get_market_price(self, token_id, side, amount):
            return 0.76 if side == "BUY" else 0.74

        def buy(self, token_id, amount_usd, price=0.0, order_type="", balance_hint=0.0):
            self.buy_calls.append(
                {"token_id": token_id, "amount_usd": amount_usd, "price": price, "order_type": order_type}
            )
            return OrderResult(
                success=True,
                order_id="test-order",
                status=FILLED,
                side="BUY",
                price=price or 0.76,
                amount_usd=amount_usd,
                shares=5.0,
                token_id=token_id,
                dry_run=False,
            )

    class FakePriceFeed:
        def get_price(self):
            # Same side as the DOWN signal. Under vol=0.0835 this remains >= MIN_PROB;
            # under the old default-vol recheck it falsely drops below 0.86.
            return 99.9164, True

    class FakeReferenceFeed:
        def get_latest(self):
            return None

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision(
                action="normal",
                reason="source_consensus_ok",
                signal_price=99.9164,
                signal_side="DOWN",
                chainlink_price=None,
                chainlink_age_seconds=None,
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

    monkeypatch.setattr(bot, "get_current_market", lambda period: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot.price_feed = FakePriceFeed()
    polybot.rtds_feed = FakeReferenceFeed()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()
    polybot.telegram = FakeTelegram()

    latest_prob = estimate_true_probability(-0.0836, 236.0, vol=0.0835)
    assert latest_prob >= polybot.strategy_config.min_prob
    assert estimate_true_probability(-0.0836, 236.0) < polybot.strategy_config.min_prob

    sig = TradeSignal(
        side="DOWN",
        confidence=latest_prob,
        btc_delta_pct=-0.0836,
        market_price=0.76,
        edge=latest_prob - 0.76,
        true_prob=latest_prob,
        seconds_remaining=236.0,
        kelly_size=3.80,
        gap=latest_prob - 0.76,
        fee_adjusted_edge=latest_prob - 0.76,
        fee_rate_bps=0.0,
        markov_persistence=0.78,
        markov_regime="markov_medium",
        edge_required=0.07,
    )

    polybot._execute_trade(sig, seconds_remaining=236.0)

    assert len(polybot.executor.buy_calls) == 1
    assert polybot._traded is True
    assert polybot.tracker.signals[-1]["action"] == "traded"
