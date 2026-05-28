from executor import Executor, OrderResult, FILLED


class FakeOrderClient:
    def get_balance_allowance(self, params):
        return {"balance": "10000000"}

    def create_order(self, order_args):
        self.order_args = order_args
        return {"signed": True}

    def post_order(self, signed_order, order_type):
        return {"orderID": "order-1"}


def test_limit_buy_allows_order_when_share_size_meets_market_minimum_even_if_notional_below_five(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    executor.client = FakeOrderClient()
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
