from typing import cast, Any

from clob_orderbook_cache import ClobOrderBookCache


class DummyWs:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_subscribe_closes_active_ws_when_token_ids_change():
    cache = ClobOrderBookCache(max_book_age_seconds=10)
    ws = DummyWs()
    cache._running = True
    cache._ws_app = cast(Any, ws)
    cache.subscribe(["old-up", "old-down"])

    assert ws.closed


def test_subscribe_does_not_close_ws_when_token_ids_unchanged():
    cache = ClobOrderBookCache(max_book_age_seconds=10)
    cache._token_ids = {"up", "down"}
    cache._running = True
    ws = DummyWs()
    cache._ws_app = cast(Any, ws)

    cache.subscribe(["down", "up"])

    assert not ws.closed
