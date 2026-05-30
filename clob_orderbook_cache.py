"""Local Polymarket CLOB orderbook cache.

The hot trade path must not block on REST price rechecks. This cache maintains
best bid/ask and depth from the CLOB market websocket and exposes the same
`get_market_price(token_id, side, amount_usd)` shape used by Executor.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional


CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class CachedBook:
    bids: list[BookLevel] = field(default_factory=list)  # high -> low
    asks: list[BookLevel] = field(default_factory=list)  # low -> high
    timestamp: float = 0.0


@dataclass
class BuyDepthSnapshot:
    best_ask: float = 0.0
    best_bid: float = 0.0
    worst_price: float = 0.0
    cap_price: float = 0.0
    cumulative_shares: float = 0.0
    cumulative_usd: float = 0.0
    required_shares: float = 0.0
    book_age_ms: float = 0.0
    enough: bool = False


class ClobOrderBookCache:

    def __init__(self, max_book_age_seconds: float = 1.0):
        self.max_book_age_seconds = float(max_book_age_seconds)
        self._books: dict[str, CachedBook] = {}
        self._lock = threading.Lock()
        self._token_ids: set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ws_app = None

    def subscribe(self, token_ids: Iterable[str]) -> None:
        new_token_ids = {str(t) for t in token_ids if t}
        ws_app = None
        changed = False
        with self._lock:
            changed = new_token_ids != self._token_ids
            self._token_ids = new_token_ids
            ws_app = self._ws_app
        # Polymarket's market websocket subscription is sent on open. When the
        # 5-minute market rolls, token IDs change; reconnect so on_open sends
        # the new assets_ids.
        if changed and self._running and ws_app is not None:
            try:
                ws_app.close()
            except Exception:
                pass

    def start(self, token_ids: Optional[Iterable[str]] = None) -> None:
        if token_ids is not None:
            self.subscribe(token_ids)
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            ws_app = self._ws_app
        if ws_app is not None:
            try:
                ws_app.close()
            except Exception:
                pass

    def apply_book(self, token_id: str, bids: Iterable[dict], asks: Iterable[dict], timestamp: Optional[float] = None) -> None:
        ts = time.time() if timestamp is None else float(timestamp)
        book = CachedBook(
            bids=sorted(self._parse_levels(bids), key=lambda x: x.price, reverse=True),
            asks=sorted(self._parse_levels(asks), key=lambda x: x.price),
            timestamp=ts,
        )
        with self._lock:
            self._books[str(token_id)] = book

    def apply_price_change(self, token_id: str, changes: Iterable[dict], timestamp: Optional[float] = None) -> None:
        ts = time.time() if timestamp is None else float(timestamp)
        token_id = str(token_id)
        with self._lock:
            book = self._books.setdefault(token_id, CachedBook(timestamp=ts))
            for change in changes:
                side = str(change.get("side") or change.get("asset_side") or "").upper()
                price = self._to_float(change.get("price"))
                size = self._to_float(change.get("size"))
                if price <= 0:
                    continue
                if side in {"BUY", "BID"}:
                    book.bids = self._upsert_level(book.bids, price, size, reverse=True)
                elif side in {"SELL", "ASK"}:
                    book.asks = self._upsert_level(book.asks, price, size, reverse=False)
            book.timestamp = ts

    def is_fresh(self, token_id: str) -> bool:
        with self._lock:
            book = self._books.get(str(token_id))
            if not book:
                return False
            return time.time() - book.timestamp <= self.max_book_age_seconds

    def get_best_bid(self, token_id: str) -> float:
        book = self._fresh_book(token_id)
        return book.bids[0].price if book and book.bids else 0.0

    def get_best_ask(self, token_id: str) -> float:
        book = self._fresh_book(token_id)
        return book.asks[0].price if book and book.asks else 0.0

    def get_market_price(self, token_id: str, side: str, amount_usd: float) -> float:
        book = self._fresh_book(token_id)
        if not book:
            return 0.0
        side = side.upper()
        if side == "BUY":
            return self._walk_buy(book.asks, float(amount_usd))
        if side == "SELL":
            return self._walk_sell(book.bids, float(amount_usd))
        return 0.0

    def get_buy_depth_snapshot(self, token_id: str, required_shares: float, cap_price: float) -> BuyDepthSnapshot:
        book = self._fresh_book(token_id)
        if not book or required_shares <= 0 or cap_price <= 0:
            return BuyDepthSnapshot(required_shares=float(required_shares), cap_price=float(cap_price))

        remaining = float(required_shares)
        cumulative_shares = 0.0
        cumulative_usd = 0.0
        worst_price = 0.0
        for level in book.asks:
            if level.price > cap_price:
                break
            if level.size <= 0:
                continue
            cumulative_shares += level.size
            cumulative_usd += level.size * level.price
            worst_price = level.price
            remaining -= level.size

        return BuyDepthSnapshot(
            best_ask=book.asks[0].price if book.asks else 0.0,
            best_bid=book.bids[0].price if book.bids else 0.0,
            worst_price=round(worst_price, 6),
            cap_price=round(float(cap_price), 6),
            cumulative_shares=round(cumulative_shares, 6),
            cumulative_usd=round(cumulative_usd, 6),
            required_shares=float(required_shares),
            book_age_ms=round((time.time() - book.timestamp) * 1000.0, 1),
            enough=cumulative_shares + 1e-9 >= float(required_shares),
        )

    def _fresh_book(self, token_id: str) -> Optional[CachedBook]:
        with self._lock:
            book = self._books.get(str(token_id))
            if not book:
                return None
            if time.time() - book.timestamp > self.max_book_age_seconds:
                return None
            return CachedBook(bids=list(book.bids), asks=list(book.asks), timestamp=book.timestamp)

    def _walk_buy(self, asks: list[BookLevel], usd_amount: float) -> float:
        if usd_amount <= 0:
            return 0.0
        remaining = usd_amount
        worst = 0.0
        for level in asks:
            capacity = level.price * level.size
            worst = level.price
            if capacity >= remaining:
                return round(worst, 6)
            remaining -= capacity
        return 0.0

    def _walk_sell(self, bids: list[BookLevel], shares_amount: float) -> float:
        if shares_amount <= 0:
            return 0.0
        remaining = shares_amount
        worst = 0.0
        for level in bids:
            worst = level.price
            if level.size >= remaining:
                return round(worst, 6)
            remaining -= level.size
        return 0.0

    def _ws_loop(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception:
            return

        def on_open(ws):
            with self._lock:
                assets_ids = list(self._token_ids)
            if not assets_ids:
                return
            # Polymarket docs use assets_ids for the CLOB market channel.
            ws.send(json.dumps({"assets_ids": assets_ids, "type": "market"}, separators=(",", ":")))

        def on_message(ws, message: str):
            self._handle_ws_message(message)

        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    CLOB_MARKET_WS,
                    on_open=on_open,
                    on_message=on_message,
                )
                with self._lock:
                    self._ws_app = ws
                ws.run_forever(ping_interval=20, ping_timeout=5, origin="https://polymarket.com")
                with self._lock:
                    if self._ws_app is ws:
                        self._ws_app = None
            except Exception:
                pass
            if self._running:
                time.sleep(1)

    def _handle_ws_message(self, message: str) -> None:
        try:
            obj = json.loads(message)
        except Exception:
            return
        events = obj if isinstance(obj, list) else [obj]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            token_id = str(event.get("asset_id") or event.get("token_id") or "")
            ts = self._timestamp_from_event(event)
            if event_type == "book" or ("bids" in event and "asks" in event):
                token_id = token_id or str(event.get("asset_id") or "")
                if token_id:
                    self.apply_book(token_id, event.get("bids") or [], event.get("asks") or [], ts)
            elif event_type == "price_change":
                changes = event.get("changes") or []
                by_token: dict[str, list[dict]] = {}
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    cid = str(change.get("asset_id") or change.get("token_id") or token_id or "")
                    if cid:
                        by_token.setdefault(cid, []).append(change)
                for cid, token_changes in by_token.items():
                    self.apply_price_change(cid, token_changes, ts)

    def _timestamp_from_event(self, event: dict) -> float:
        raw = event.get("timestamp") or event.get("ts")
        try:
            val = float(raw)
            if val > 10_000_000_000:
                return val / 1000.0
            return val
        except Exception:
            return time.time()

    def _parse_levels(self, levels: Iterable[dict]) -> list[BookLevel]:
        parsed = []
        for level in levels or []:
            if not isinstance(level, dict):
                continue
            price = self._to_float(level.get("price"))
            size = self._to_float(level.get("size"))
            if price > 0 and size > 0:
                parsed.append(BookLevel(price=price, size=size))
        return parsed

    def _upsert_level(self, levels: list[BookLevel], price: float, size: float, reverse: bool) -> list[BookLevel]:
        out = [lvl for lvl in levels if abs(lvl.price - price) > 1e-9]
        if size > 0:
            out.append(BookLevel(price=price, size=size))
        return sorted(out, key=lambda x: x.price, reverse=reverse)

    def _to_float(self, value) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0
