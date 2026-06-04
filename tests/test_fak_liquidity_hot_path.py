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


def test_fak_cap_never_pads_above_executable_ask():
    # No slippage-chase parameter exists: the cap is structurally the executable
    # price rounded to tick, never padded above the live ask.
    assert bot.choose_fak_price_cap(executable_price=0.63, tick_size=0.01) == 0.63
    assert bot.choose_fak_price_cap(executable_price=0.64, tick_size=0.01) == 0.64


def test_entry_orderbook_snapshot_reads_buy_sell_depth_from_one_book_copy():
    cache = ClobOrderBookCache(max_book_age_seconds=1.0)
    cache.apply_book(
        token_id="tok-up",
        bids=[{"price": "0.61", "size": "4"}, {"price": "0.60", "size": "10"}],
        asks=[{"price": "0.63", "size": "2"}, {"price": "0.64", "size": "4"}],
        timestamp=time.time(),
    )

    snap = cache.get_entry_snapshot(
        "tok-up",
        buy_usd_amount=3.20,
        sell_shares_amount=5.0,
        required_buy_shares=5.0,
        cap_price=0.64,
    )

    assert snap.executable_buy_price == 0.64
    assert snap.executable_sell_price == 0.60
    assert snap.depth.enough is True
    assert snap.depth.best_ask == 0.63
    assert snap.depth.best_bid == 0.61
    assert 0 <= snap.snapshot_age_ms < 1000


def test_executor_warm_order_metadata_primes_sdk_caches_without_posting():
    class WarmClient:
        def __init__(self):
            self.calls = []

        def get_tick_size(self, token_id):
            self.calls.append(("tick", token_id))
            return "0.01"

        def get_neg_risk(self, token_id):
            self.calls.append(("neg", token_id))
            return False

        def _ClobClient__resolve_version(self):
            self.calls.append(("version", ""))
            return 2

    executor = Executor(private_key="", safe_address="", dry_run=False)
    executor.client = cast(object, WarmClient())  # type: ignore[assignment]
    executor._initialized = True

    executor.warm_order_metadata(["up", "down"])

    assert ("version", "") in executor.client.calls
    assert ("tick", "up") in executor.client.calls
    assert ("neg", "up") in executor.client.calls
    assert ("tick", "down") in executor.client.calls
    assert ("neg", "down") in executor.client.calls


def test_order_latency_log_includes_fine_grained_timestamps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    executor = Executor(private_key="", safe_address="", dry_run=False)

    executor._log_order_latency(
        "token", 0.80, 5, "no_fill_liquidity_gone",
        bal_ms=0.0, sign_ms=5.0, post_ms=250.0,
        timing={
            "signal_ready_ts": 1000.0,
            "final_book_snapshot_ts": 1000.1,
            "executor_buy_start_ts": 1000.2,
            "sign_start_ts": 1000.3,
            "sign_end_ts": 1000.305,
            "post_start_ts": 1000.306,
            "post_end_ts": 1000.556,
            "book_age_ms": 120.0,
            "book_hash": "abc",
            "sdk_warmed": True,
        },
    )

    text = (tmp_path / "order_latency.csv").read_text()
    assert "signal_ready_ts" in text
    assert "final_book_snapshot_ts" in text
    assert "post_start_ts" in text
    assert "book_hash" in text
    assert "sdk_warmed" in text
