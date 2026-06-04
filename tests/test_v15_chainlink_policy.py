import bot
from source_consensus import SourceConsensusDecision
from strategy import TradeSignal
from executor import OrderResult, FILLED


class _Ref:
    price = 100.10
    age_seconds = 0.1
    timestamp = 1.0
    source = "test_chainlink"


def test_entry_reference_uses_chainlink_open_not_binance_when_sides_disagree(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()
    polybot._chainlink_open_price = 100.0
    polybot._opening_price = 100.0

    ref = polybot._entry_reference_price(binance_price=99.90, chainlink=_Ref())

    assert ref["price"] == 100.10
    assert ref["opening_price"] == 100.0
    assert ref["side"] == "UP"
    assert ref["source"] == "polymarket_rtds_chainlink"


def test_final_prebuy_check_does_not_call_binance_price_feed(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    monkeypatch.setenv("ENTRY_MAX_SPREAD", "0.20")
    monkeypatch.setenv("ENTRY_MIN_EXIT_PRICE", "0.01")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000300
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01
    polybot._last_real_balance = 20.0

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
            return 0.70 if side == "BUY" else 0.65

        def buy(self, token_id, amount_usd, price=0.0, balance_hint=0.0):
            self.buy_calls.append({"token_id": token_id, "amount_usd": amount_usd, "price": price})
            return OrderResult(
                success=True,
                order_id="test-order",
                status=FILLED,
                side="BUY",
                price=price,
                amount_usd=amount_usd,
                shares=5.0,
                token_id=token_id,
                dry_run=False,
            )

    class ExplodingPriceFeed:
        def get_price(self):
            raise AssertionError("final pre-buy check must not use Binance")

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision(
                action="normal",
                reason="rtds_chainlink_lag_aligned",
                signal_price=100.10,
                signal_side=side,
                chainlink_price=100.10,
                chainlink_age_seconds=0.1,
                chainlink_delta_pct=0.10,
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
    polybot.price_feed = ExplodingPriceFeed()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()
    polybot.telegram = FakeTelegram()

    sig = TradeSignal(
        side="UP",
        confidence=0.92,
        btc_delta_pct=0.10,
        market_price=0.70,
        edge=0.22,
        true_prob=0.92,
        seconds_remaining=150.0,
        kelly_size=5.0,
        gap=0.22,
        fee_adjusted_edge=0.22,
        fee_rate_bps=0.0,
        markov_persistence=0.0,
        edge_required=0.06,
    )

    polybot._execute_trade(sig, seconds_remaining=150.0)

    assert len(polybot.executor.buy_calls) == 1
    assert polybot.tracker.signals[-1]["action"] == "traded"



def test_prebuy_does_not_ping_clob_health_on_hot_path(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    monkeypatch.setenv("ENTRY_MAX_SPREAD", "0.20")
    monkeypatch.setenv("ENTRY_MIN_EXIT_PRICE", "0.01")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000600
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01
    polybot._last_real_balance = 20.0

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class ExplodingClient:
        def get_ok(self):
            raise AssertionError("health GET must not be in the buy hot path")

    class FakeExecutor:
        _initialized = True
        client = ExplodingClient()

        def __init__(self):
            self.buy_calls = []

        def get_market_price(self, token_id, side, amount):
            return 0.70 if side == "BUY" else 0.65

        def buy(self, token_id, amount_usd, price=0.0, balance_hint=0.0):
            self.buy_calls.append(token_id)
            return OrderResult(True, "ok", FILLED, "BUY", price, amount_usd, 5.0, token_id, False)

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision("normal", "ok", 1.0, 100.1, side, 100.1, 0.1)

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

    sig = TradeSignal("UP", 0.92, 0.10, 0.70, 0.22, 0.92, 150.0, 5.0, edge_required=0.06)

    polybot._execute_trade(sig, seconds_remaining=150.0)

    assert polybot.executor.buy_calls == ["UPTOKEN"]


def test_cache_enabled_price_unavailable_skips_instead_of_rest_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "true")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000900
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class EmptyOrderbookCache:
        def get_market_price(self, *args, **kwargs):
            return 0.0

    class FakeExecutor:
        _initialized = True

        def get_market_price(self, *args, **kwargs):
            raise AssertionError("REST quote fallback must not run when cache is enabled")

        def buy(self, *args, **kwargs):
            raise AssertionError("buy should be skipped when cached executable ask is unavailable")

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision("normal", "ok", 1.0, 100.1, side, 100.1, 0.1)

    class FakeTracker:
        def __init__(self):
            self.signals = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

    monkeypatch.setattr(bot, "get_current_market", lambda period, include_open_price=False: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot.orderbook_cache = EmptyOrderbookCache()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.tracker = FakeTracker()

    sig = TradeSignal("UP", 0.92, 0.10, 0.70, 0.22, 0.92, 150.0, 5.0, edge_required=0.06)

    polybot._execute_trade(sig, seconds_remaining=150.0)

    assert polybot.tracker.signals[-1]["action"] == "skipped_orderbook_stale"


def test_live_missing_source_snapshot_skips_without_sync_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    polybot = bot.PolyBot()
    polybot._current_window = 1710001200
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30
    polybot._market_min_order_size = 5.0
    polybot._market_tick_size = 0.01

    class FakeMarket:
        token_id_up = "UPTOKEN"
        token_id_down = "DOWNTOKEN"

    class FakeSourceConsensus:
        def assess_snapshot(self, side):
            return SourceConsensusDecision("skip", "source_snapshot_missing", 0.0, side, None, None)

        def update_snapshot(self, *args, **kwargs):
            raise AssertionError("LIVE hot path must not synchronously refresh source snapshots")

    class ExplodingPriceFeed:
        def get_price(self):
            raise AssertionError("LIVE hot path must not call Binance fallback on source_snapshot_missing")

    class FakeExecutor:
        _initialized = True

        def get_market_price(self, *args, **kwargs):
            raise AssertionError("missing source should skip before CLOB price lookup")

        def buy(self, *args, **kwargs):
            raise AssertionError("missing source should not buy")

    class FakeTracker:
        def __init__(self):
            self.signals = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

    monkeypatch.setattr(bot, "get_current_market", lambda period, include_open_price=False: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.price_feed = ExplodingPriceFeed()
    polybot.tracker = FakeTracker()

    sig = TradeSignal("UP", 0.92, 0.10, 0.70, 0.22, 0.92, 150.0, 5.0, edge_required=0.06)

    polybot._execute_trade(sig, seconds_remaining=150.0)

    assert polybot.tracker.signals[-1]["action"] == "skipped_source_disagreement"
    assert polybot.tracker.signals[-1]["skip_reason"] == "source_snapshot_missing"
