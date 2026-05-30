
import bot
from executor import Executor, FILLED, OrderResult
from strategy import TradeSignal


def test_stop_loss_code_is_removed_for_hold_to_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    polybot = bot.PolyBot()

    assert not hasattr(polybot, "_stop_loss_enabled")


def test_directional_probability_for_down_uses_brownian_current_direction():
    # estimate_true_probability uses abs(delta), so a negative delta means the
    # returned probability is DOWN's probability, not UP's probability.
    prob = bot.probability_for_held_side(
        btc_delta_pct=-0.12,
        seconds_remaining=120,
        held_side="DOWN",
        vol=0.12,
    )

    assert prob > 0.80


def test_directional_probability_penalizes_held_side_after_reversal():
    prob = bot.probability_for_held_side(
        btc_delta_pct=0.12,
        seconds_remaining=120,
        held_side="DOWN",
        vol=0.12,
    )

    assert prob < 0.20


class FakeMarketOrderClient:
    def __init__(self):
        self.order_args = None
        self.order_type = None
        self.post_only = None

    def get_balance_allowance(self, params):
        return {"balance": "10000000"}

    def create_order(self, order_args):
        self.order_args = order_args
        return {"signed": True}

    def post_order(self, signed_order, order_type, post_only=False):
        self.order_type = order_type
        self.post_only = post_only
        return {"orderID": "order-1"}


def test_live_buy_uses_fak_taker_market_order_not_gtd_post_only(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    fake_client = FakeMarketOrderClient()
    executor.client = fake_client  # type: ignore[assignment]
    executor._initialized = True
    executor.min_order_size = 5.0
    executor.tick_size = 0.01

    def fake_verify(order_id, price, shares, token_id, balance_before):
        return OrderResult(
            success=True,
            order_id=order_id,
            status=FILLED,
            side="BUY",
            price=price,
            amount_usd=shares * price,
            shares=shares,
            token_id=token_id,
            dry_run=False,
        )

    monkeypatch.setattr(executor, "_verify_buy_via_balance", fake_verify)

    result = executor.buy("token", amount_usd=5.0, price=0.55)

    assert result.success is True
    assert fake_client.order_type == "FAK"
    assert fake_client.post_only is False
    assert fake_client.order_args is not None
    assert not hasattr(fake_client.order_args, "expiration") or fake_client.order_args.expiration in (0, None)
    assert fake_client.order_args.size == 9.0


def test_execute_trade_skips_non_huge_edge_before_buy(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("ENTRY_HUGE_EDGE_MIN", "0.15")
    monkeypatch.setenv("CLOB_ORDERBOOK_CACHE_ENABLED", "false")
    polybot = bot.PolyBot()
    polybot._current_window = 1710000000
    polybot._opening_price = 75000.0
    polybot._cached_up = 0.70
    polybot._cached_down = 0.30

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
            return 0.70 if side == "BUY" else 0.68

        def buy(self, *args, **kwargs):
            raise AssertionError("non-huge edge should not submit any buy")

    class FakeSourceConsensus:
        basis_mean_bps = 0.0

        def assess_snapshot(self, side):
            from source_consensus import SourceConsensusDecision
            return SourceConsensusDecision(
                action="normal",
                reason="sources_agree",
                size_multiplier=1.0,
                adjusted_price=75100.0,
                adjusted_side="UP",
                basis_bps=0.0,
                basis_mean_bps=0.0,
                basis_deviation_bps=0.0,
                reference_price=75100.0,
                reference_age_seconds=0.1,
            )

    class FakePriceFeed:
        def get_price(self):
            return 75100.0, True

    class FakeTracker:
        def __init__(self):
            self.signals = []

        def log_signal(self, **kwargs):
            self.signals.append(kwargs)

    monkeypatch.setattr(bot, "get_current_market", lambda period, include_open_price=False: FakeMarket())
    polybot.executor = FakeExecutor()
    polybot.source_consensus = FakeSourceConsensus()
    polybot.price_feed = FakePriceFeed()
    polybot.tracker = FakeTracker()

    sig = TradeSignal(
        side="UP",
        confidence=0.9,
        btc_delta_pct=0.13,
        market_price=0.70,
        edge=0.10,
        true_prob=0.80,
        seconds_remaining=180,
        kelly_size=5.0,
        gap=0.10,
        fee_adjusted_edge=0.10,
        fee_rate_bps=0.0,
        markov_persistence=0.9,
        edge_required=0.05,
    )

    polybot._execute_trade(sig, seconds_remaining=180)

    assert polybot._traded is False
    assert polybot.tracker.signals[-1]["action"] == "skipped_not_huge_edge"
    assert polybot.tracker.signals[-1]["skip_reason"] == "fee_adjusted_edge_below_huge_taker_threshold"
