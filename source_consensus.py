"""RTDS source-consensus risk gate for BTC Up/Down markets.

Do not manufacture an oracle by applying a rolling Binance/Chainlink basis.
The live trade signal is anchored to Polymarket/Chainlink official openPrice
plus current RTDS Chainlink. RTDS Binance is logged as diagnostic context only;
it must not veto or downsize Chainlink-confirmed live entries.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Optional


POLYMARKET_RTDS_WS = "wss://ws-live-data.polymarket.com"


@dataclass
class RtdsPrice:
    price: float
    source: str
    timestamp: float
    age_seconds: float


@dataclass
class SourceConsensusConfig:
    enabled: bool = True
    require_chainlink: bool = True
    stale_skip_seconds: float = 30.0
    # Settlement-source confirmation gate. The 5m BTC Up/Down rules resolve
    # from Chainlink BTC/USD, so a signal is tradable only when Chainlink is on
    # the intended side and far enough from open to avoid the near-zero coin-flip zone.
    min_chainlink_delta_pct: float = 0.07



@dataclass
class SourceConsensusDecision:
    action: str  # "normal", "skip"
    reason: str
    signal_price: float
    signal_side: str
    chainlink_price: Optional[float]
    chainlink_age_seconds: Optional[float]
    rtds_binance_price: Optional[float] = None
    rtds_binance_age_seconds: Optional[float] = None
    source_gap_bps: Optional[float] = None
    direct_vs_rtds_binance_gap_bps: Optional[float] = None
    chainlink_delta_pct: Optional[float] = None
    rtds_binance_delta_pct: Optional[float] = None

    @property
    def should_skip(self) -> bool:
        return self.action == "skip"


class PolymarketRtdsFeed:
    """Polymarket RTDS crypto feed for Binance and Chainlink BTC prices."""

    def __init__(self, symbol: str = "BTC"):
        self.symbol = symbol.upper()
        self._lock = threading.Lock()
        self._latest_chainlink: Optional[RtdsPrice] = None
        self._latest_rtds_binance: Optional[RtdsPrice] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def get_latest(self, max_age: Optional[float] = None) -> Optional[RtdsPrice]:
        return self._copy_latest("chainlink", max_age=max_age)

    def get_binance_latest(self, max_age: Optional[float] = None) -> Optional[RtdsPrice]:
        return self._copy_latest("binance", max_age=max_age)

    def _copy_latest(self, which: str, max_age: Optional[float] = None) -> Optional[RtdsPrice]:
        with self._lock:
            latest = self._latest_chainlink if which == "chainlink" else self._latest_rtds_binance
        if not latest:
            return None
        age = time.time() - latest.timestamp
        if max_age is not None and age > max_age:
            return None
        return RtdsPrice(latest.price, latest.source, latest.timestamp, age)

    def _set_latest(self, price: float, timestamp_ms: int, source: str) -> None:
        if price <= 0 or timestamp_ms <= 0:
            return
        ref = RtdsPrice(
            price=float(price),
            source=source,
            timestamp=float(timestamp_ms) / 1000.0,
            age_seconds=max(0.0, time.time() - (float(timestamp_ms) / 1000.0)),
        )
        with self._lock:
            if source == "polymarket_rtds_binance":
                if self._latest_rtds_binance is None or ref.timestamp >= self._latest_rtds_binance.timestamp:
                    self._latest_rtds_binance = ref
            elif self._latest_chainlink is None or ref.timestamp >= self._latest_chainlink.timestamp:
                self._latest_chainlink = ref

    def _ws_loop(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception:
            return

        binance_symbol = f"{self.symbol.lower()}usdt"
        chainlink_symbol = f"{self.symbol.lower()}/usd"

        def on_open(ws):
            msg = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices",
                        "type": "update",
                        "filters": json.dumps({"symbol": binance_symbol}, separators=(",", ":")),
                    },
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": json.dumps({"symbol": chainlink_symbol}, separators=(",", ":")),
                    },
                ],
            }
            ws.send(json.dumps(msg, separators=(",", ":")))

            def app_ping_loop() -> None:
                while self._running and getattr(ws, "keep_running", False):
                    time.sleep(5)
                    if not self._running or not getattr(ws, "keep_running", False):
                        break
                    try:
                        ws.send("PING")
                    except Exception:
                        break

            threading.Thread(target=app_ping_loop, daemon=True).start()

        def on_message(ws, message: str):
            if not message or not message.strip() or message == "PONG":
                return
            try:
                obj = json.loads(message)
            except Exception:
                return
            if not isinstance(obj, dict):
                return
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                return
            symbol = str(payload.get("symbol", "")).lower()
            price = float(payload.get("value", payload.get("price", 0.0)) or 0.0)
            ts = int(payload.get("timestamp", obj.get("timestamp", 0)) or 0)
            topic = obj.get("topic")
            if topic == "crypto_prices" and symbol == binance_symbol:
                self._set_latest(price, ts, "polymarket_rtds_binance")
            elif topic == "crypto_prices_chainlink" and symbol == chainlink_symbol:
                self._set_latest(price, ts, "polymarket_rtds_chainlink")

        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    POLYMARKET_RTDS_WS,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=lambda ws, error: None,
                )
                ws.run_forever(ping_interval=20, ping_timeout=5, origin="https://polymarket.com")
            except Exception:
                pass
            if self._running:
                time.sleep(5)


class SourceConsensusGate:
    def __init__(self, config: SourceConsensusConfig):
        self.config = config
        self._latest_snapshot: Optional[SourceConsensusDecision] = None
        self._lock = threading.Lock()

    def update_snapshot(
        self,
        *,
        binance_price: float,
        opening_price: float,
        intended_side: str,
        chainlink: Optional[RtdsPrice],
        rtds_binance: Optional[RtdsPrice] = None,
        binance_opening_price: Optional[float] = None,
    ) -> SourceConsensusDecision:
        decision = self.assess(
            binance_price=binance_price,
            opening_price=opening_price,
            intended_side=intended_side,
            chainlink=chainlink,
            rtds_binance=rtds_binance,
            binance_opening_price=binance_opening_price,
        )
        with self._lock:
            self._latest_snapshot = decision
        return decision

    def assess_snapshot(self, intended_side: str) -> SourceConsensusDecision:
        with self._lock:
            snapshot = self._latest_snapshot
        if snapshot is None:
            return self._decision("skip", "source_snapshot_missing", 0.0, intended_side, None, None, None, None)
        if snapshot.signal_side != intended_side.upper():
            chainlink = (
                RtdsPrice(snapshot.chainlink_price, "snapshot", time.time(), snapshot.chainlink_age_seconds or 0.0)
                if snapshot.chainlink_price else None
            )
            rtds_binance = (
                RtdsPrice(snapshot.rtds_binance_price, "snapshot", time.time(), snapshot.rtds_binance_age_seconds or 0.0)
                if snapshot.rtds_binance_price else None
            )
            return self._decision(
                "skip",
                "source_snapshot_side_disagrees",
                snapshot.signal_price,
                snapshot.signal_side,
                chainlink,
                rtds_binance,
                snapshot.source_gap_bps,
                snapshot.direct_vs_rtds_binance_gap_bps,
            )
        return snapshot

    def assess(
        self,
        *,
        binance_price: float,
        opening_price: float,
        intended_side: str,
        chainlink: Optional[RtdsPrice],
        rtds_binance: Optional[RtdsPrice] = None,
        binance_opening_price: Optional[float] = None,
    ) -> SourceConsensusDecision:
        intended_side = intended_side.upper()
        signal_price = float(chainlink.price if chainlink else (binance_price or 0.0))
        signal_side = intended_side

        if not self.config.enabled:
            return self._decision("normal", "source_consensus_disabled", signal_price, signal_side, chainlink, rtds_binance)
        if opening_price <= 0:
            return self._decision("skip", "missing_price_for_source_consensus", signal_price, signal_side, chainlink, rtds_binance)

        direct_gap = None
        rtds_delta_pct = None
        if rtds_binance is not None:
            direct_ref = float(binance_price or 0.0)
            if direct_ref > 0 and rtds_binance.price > 0:
                direct_gap = abs((direct_ref - rtds_binance.price) / rtds_binance.price * 10000.0)
            binance_open = (
                float(binance_opening_price)
                if binance_opening_price and binance_opening_price > 0
                else float(opening_price or 0.0)
            )
            if binance_open > 0:
                rtds_delta_pct = (rtds_binance.price - binance_open) / binance_open * 100.0

        if chainlink is None:
            if self.config.require_chainlink:
                return self._decision("skip", "missing_polymarket_chainlink", signal_price, signal_side, chainlink, rtds_binance, direct_gap=direct_gap, rtds_delta_pct=rtds_delta_pct)
            return self._decision("normal", "chainlink_not_required", signal_price, signal_side, chainlink, rtds_binance, direct_gap=direct_gap, rtds_delta_pct=rtds_delta_pct)

        chainlink_delta_pct = (chainlink.price - opening_price) / opening_price * 100.0
        chainlink_side = "UP" if chainlink_delta_pct >= 0.0 else "DOWN"
        signal_price = chainlink.price
        signal_side = chainlink_side
        source_gap = ((rtds_binance.price - chainlink.price) / chainlink.price * 10000.0) if rtds_binance is not None else None

        if chainlink.age_seconds > self.config.stale_skip_seconds:
            return self._decision("skip", "polymarket_chainlink_stale", signal_price, signal_side, chainlink, rtds_binance, source_gap, direct_gap, chainlink_delta_pct, rtds_delta_pct)
        if chainlink_side != intended_side:
            return self._decision("skip", "chainlink_side_disagrees", signal_price, signal_side, chainlink, rtds_binance, source_gap, direct_gap, chainlink_delta_pct, rtds_delta_pct)
        if abs(chainlink_delta_pct) < self.config.min_chainlink_delta_pct:
            return self._decision("skip", "chainlink_delta_too_close_to_open", signal_price, signal_side, chainlink, rtds_binance, source_gap, direct_gap, chainlink_delta_pct, rtds_delta_pct)

        # Binance/RTDS is diagnostic only here. It must not veto or downsize a
        # trade whose settlement-source Chainlink side and distance are valid.
        return self._decision("normal", "chainlink_settlement_source_confirmed", signal_price, signal_side, chainlink, rtds_binance, source_gap, direct_gap, chainlink_delta_pct, rtds_delta_pct)

    def _decision(
        self,
        action: str,
        reason: str,
        signal_price: float,
        signal_side: str,
        chainlink: Optional[RtdsPrice],
        rtds_binance: Optional[RtdsPrice],
        source_gap: Optional[float] = None,
        direct_gap: Optional[float] = None,
        chainlink_delta_pct: Optional[float] = None,
        rtds_delta_pct: Optional[float] = None,
    ) -> SourceConsensusDecision:
        return SourceConsensusDecision(
            action=action,
            reason=reason,
            signal_price=signal_price,
            signal_side=signal_side,
            chainlink_price=chainlink.price if chainlink else None,
            chainlink_age_seconds=chainlink.age_seconds if chainlink else None,
            rtds_binance_price=rtds_binance.price if rtds_binance else None,
            rtds_binance_age_seconds=rtds_binance.age_seconds if rtds_binance else None,
            source_gap_bps=source_gap,
            direct_vs_rtds_binance_gap_bps=direct_gap,
            chainlink_delta_pct=chainlink_delta_pct,
            rtds_binance_delta_pct=rtds_delta_pct,
        )
