import time
from typing import cast

from clob_orderbook_cache import ClobOrderBookCache
from executor import Executor, REJECTED
import bot


class NoMatchClient:
    def get_balance_allowance(self, params):
        return {"balance": "10000000"}

    def create_order(self, order_args):
        return {"signed": True}

    def post_order(self, signed_order, order_type, post_only=False):
        raise Exception("PolyApiException[status_code=400, error_message={'error': 'no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.', 'orderID': '0xabc'}]")


def test_buy_depth_snapshot_reports_required_depth_and_book_age():
    cache = ClobOrderBookCache(max_book_age_seconds=1.0)
    now = time.time()
    cache.apply_book(
        token_id="tok-up",
        bids=[{"price": "0.61", "size": "4"}],
        asks=[
            {"price": "0.63", "size": "2"},
            {"price": "0.64", "size": "4"},
            {"price": "0.65", "size": "10"},
        ],
        timestamp=now,
    )

    snap = cache.get_buy_depth_snapshot("tok-up", required_shares=5, cap_price=0.64)

    assert snap.enough is True
    assert snap.best_ask == 0.63
    assert snap.worst_price == 0.64
    assert snap.cumulative_shares == 6.0
    assert snap.required_shares == 5.0
    assert 0 <= snap.book_age_ms < 1000


def test_buy_depth_snapshot_rejects_insufficient_depth_at_cap():
    cache = ClobOrderBookCache(max_book_age_seconds=1.0)
    cache.apply_book(
        token_id="tok-up",
        bids=[{"price": "0.61", "size": "4"}],
        asks=[
            {"price": "0.63", "size": "2"},
            {"price": "0.65", "size": "10"},
        ],
        timestamp=time.time(),
    )

    snap = cache.get_buy_depth_snapshot("tok-up", required_shares=5, cap_price=0.64)

    assert snap.enough is False
    assert snap.cumulative_shares == 2.0
    assert snap.worst_price == 0.63


def test_fak_no_match_is_classified_as_liquidity_gone_not_api_failure(monkeypatch):
    executor = Executor(private_key="", safe_address="", dry_run=False)
    executor.client = cast(object, NoMatchClient())  # type: ignore[assignment]
    executor._initialized = True
    executor.min_order_size = 5.0
    executor.tick_size = 0.01
    monkeypatch.setattr("executor.time.sleep", lambda *_args, **_kwargs: None)

    result = executor.buy("token", amount_usd=3.15, price=0.63)

    assert result.success is False
    assert result.status == REJECTED
    assert result.error == "fak_no_fill_liquidity_gone"


def test_one_tick_fak_cap_is_allowed_only_when_fee_adjusted_edge_survives():
    assert bot.choose_fak_price_cap(
        true_prob=0.95,
        executable_price=0.63,
        fee_rate_bps=700,
        required_fee_edge=0.15,
        tick_size=0.01,
        slippage_ticks=1,
    ) == 0.64

    assert bot.choose_fak_price_cap(
        true_prob=0.80,
        executable_price=0.64,
        fee_rate_bps=700,
        required_fee_edge=0.15,
        tick_size=0.01,
        slippage_ticks=1,
    ) == 0.64
