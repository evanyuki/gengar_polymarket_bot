from executor import Executor


def test_dry_run_buy_uses_supplied_market_price_for_entry_price():
    executor = Executor(private_key="", safe_address="", dry_run=True)

    result = executor.buy(token_id="DRY-UP-1", amount_usd=10.0, price=0.62)

    assert result.success is True
    assert result.price == 0.62
    assert round(result.shares, 4) == round(10.0 / 0.62, 4)
