import time

from clob_orderbook_cache import ClobOrderBookCache


def test_orderbook_cache_computes_executable_buy_and_sell_prices_from_local_book():
    cache = ClobOrderBookCache(max_book_age_seconds=1.0)
    cache.apply_book(
        token_id="tok-up",
        bids=[{"price": "0.58", "size": "3"}, {"price": "0.57", "size": "10"}],
        asks=[{"price": "0.60", "size": "2"}, {"price": "0.61", "size": "10"}],
        timestamp=time.time(),
    )

    assert cache.get_market_price("tok-up", "BUY", 3.0) == 0.61
    assert cache.get_market_price("tok-up", "SELL", 5.0) == 0.57
    assert cache.get_best_ask("tok-up") == 0.60
    assert cache.get_best_bid("tok-up") == 0.58


def test_orderbook_cache_rejects_stale_or_insufficient_liquidity():
    cache = ClobOrderBookCache(max_book_age_seconds=0.1)
    cache.apply_book(
        token_id="tok-down",
        bids=[{"price": "0.40", "size": "1"}],
        asks=[{"price": "0.42", "size": "1"}],
        timestamp=time.time() - 10.0,
    )

    assert cache.get_market_price("tok-down", "BUY", 1.0) == 0.0

    cache.apply_book(
        token_id="tok-down",
        bids=[{"price": "0.40", "size": "1"}],
        asks=[{"price": "0.42", "size": "1"}],
        timestamp=time.time(),
    )
    assert cache.get_market_price("tok-down", "BUY", 5.0) == 0.0
