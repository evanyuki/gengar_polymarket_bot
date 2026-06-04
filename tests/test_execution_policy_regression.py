
import bot
from executor import Executor, FILLED, OrderResult


def test_price_stop_loss_is_active_and_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.delenv("STOP_LOSS_ENABLED", raising=False)
    monkeypatch.delenv("STOP_LOSS_PRICE_FRACTION", raising=False)

    polybot = bot.PolyBot()

    assert polybot._stop_loss_enabled is True
    assert polybot._stop_loss_price_fraction == 0.50


def test_price_stop_exits_when_sell_price_drops_50pct(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()
    polybot._opening_price = 100.0
    polybot._chainlink_open_price = 100.0
    polybot._traded = True
    polybot._trade_side = "DOWN"
    polybot._trade_price = 0.80
    polybot._trade_cost = 4.00
    polybot._trade_shares = 5.0
    polybot._trade_token_id = "tok-down"
    polybot._last_position_check = 0.0

    class NoChainlink:
        def get_latest(self):
            return None

    exits = []
    resolutions = []
    polybot.rtds_feed = NoChainlink()
    monkeypatch.setattr(polybot.tracker, "update_hold_stats", lambda prob, price: None)
    monkeypatch.setattr(
        polybot.tracker,
        "log_trade_exit",
        lambda **kwargs: exits.append(kwargs),
    )
    monkeypatch.setattr(
        polybot,
        "_record_resolution",
        lambda **kwargs: resolutions.append(kwargs),
    )

    # DOWN held, but BTC has ripped above open; held-side probability/sell price
    # collapses below 50% of 0.80 entry, so the price stop must fire.
    polybot._manage_position(btc_price=101.0, seconds_remaining=120.0, now=1000.0)

    assert exits
    assert exits[0]["exit_type"] == "price-stop"
    assert exits[0]["exit_price"] <= 0.40
    assert resolutions
    assert resolutions[0]["resolution_method"] == "price_stop_50pct"
    assert resolutions[0]["remaining_shares"] == 0.0
    assert polybot._traded is False


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


def test_huge_edge_min_gate_removed(monkeypatch, tmp_path):
    # The standalone ENTRY_HUGE_EDGE_MIN "huge-edge" gate was removed: official
    # settlement showed fee-adjusted edge is INVERSELY correlated with win rate
    # (edge<0.15 -> 91% WR, edge>=0.15 -> 61%), so a minimum-edge gate selected
    # fat-edge/low-price coin-flip losers. Entry is gated on edge_required only;
    # the live ask is re-checked against edge_required to catch slippage. The
    # env var, the _huge_edge_min attribute, and the skip path are all gone.
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("ENTRY_HUGE_EDGE_MIN", "0.15")  # must be ignored now

    polybot = bot.PolyBot()

    assert not hasattr(polybot, "_huge_edge_min")
