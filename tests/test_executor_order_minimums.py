from typing import cast

from executor import Executor, OrderResult, FILLED, calculate_order_size


class FakeOrderClient:
    def get_balance_allowance(self, params):
        return {"balance": "10000000"}

    def create_order(self, order_args):
        self.order_args = order_args
        return {"signed": True}

    def post_order(self, signed_order, order_type, post_only=False):
        self.order_type = order_type
        self.post_only = post_only
        return {"orderID": "order-1"}


def test_limit_buy_allows_order_when_share_size_meets_market_minimum_even_if_notional_below_five(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    fake_client = FakeOrderClient()
    executor.client = cast(object, fake_client)  # type: ignore[assignment]
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
    assert result.shares == 9.0
    assert result.amount_usd == 4.95
    assert fake_client.order_type == "FAK"
    assert fake_client.post_only is False
    assert fake_client.order_args.size == 9.0


def test_fak_buy_does_not_set_gtd_expiration(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    fake_client = FakeOrderClient()
    executor.client = cast(object, fake_client)  # type: ignore[assignment]
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
    monkeypatch.setattr("executor.time.time", lambda: 1_780_000_000.4)
    result = executor.buy("token", amount_usd=5.0, price=0.55)

    assert result.success is True
    assert fake_client.order_type == "FAK"
    assert not hasattr(fake_client.order_args, "expiration") or fake_client.order_args.expiration in (0, None)


def test_calculate_order_size_does_not_lose_a_share_from_float_cent_truncation():
    shares, spend = calculate_order_size(price=0.82, max_usd=4.10)

    assert shares == 5.0
    assert spend == 4.10


def test_fak_buy_passes_integer_share_size_not_dollar_amount_to_avoid_market_helper_division(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    fake_client = FakeOrderClient()
    executor.client = cast(object, fake_client)  # type: ignore[assignment]
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

    result = executor.buy("token", amount_usd=25.0, price=0.58)

    assert result.success is True
    assert result.shares == 43.0
    assert round(result.amount_usd, 2) == 24.94
    assert fake_client.order_args.size == 43.0
    assert fake_client.order_args.price == 0.58


def test_calculate_order_size_handles_integer_share_float_artifact_case():
    shares, spend = calculate_order_size(price=0.59, max_usd=12.40)

    assert shares == 21.0
    assert spend == 12.39
