"""Multi-source price consensus for BTC Up/Down markets.

Combines:
- Binance/CEX low-latency signal (primary fast input)
- Polymarket/Chainlink reference prices (official settlement anchor proxy)
- Rolling Binance-vs-Chainlink basis checks

This module is deliberately conservative: if the official/reference source is
stale, missing, or disagrees with Binance after basis adjustment, live sizing is
reduced or skipped instead of pretending a single CEX tick is ground truth.
"""

from __future__ import annotations

import json
import statistics
import time
import threading
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


POLYMARKET_WEB_API = "https://polymarket.com/api"
POLYMARKET_RTDS_WS = "wss://ws-live-data.polymarket.com"
BINANCE_KLINES_API = "https://api.binance.com/api/v3/klines"


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ReferencePrice:
    price: float
    source: str
    timestamp: float
    age_seconds: float


@dataclass
class SourceConsensusConfig:
    enabled: bool = True
    require_live_reference: bool = True
    default_basis_bps: float = -17.3
    max_basis_deviation_bps: float = 8.0
    downsize_basis_deviation_bps: float = 5.0
    stale_downsize_seconds: float = 10.0
    stale_skip_seconds: float = 30.0
    downsize_factor: float = 0.50
    min_basis_samples: int = 6
    basis_window: int = 24


@dataclass
class SourceConsensusDecision:
    action: str  # "normal", "downsize", "skip"
    reason: str
    size_multiplier: float
    adjusted_price: float
    adjusted_side: str
    basis_bps: Optional[float]
    basis_mean_bps: float
    basis_deviation_bps: Optional[float]
    reference_price: Optional[float]
    reference_age_seconds: Optional[float]

    @property
    def should_skip(self) -> bool:
        return self.action == "skip"


class PolymarketReferenceFeed:
    """Best-effort Polymarket/Chainlink reference feed.

    Primary path is Polymarket RTDS `crypto_prices_chainlink` over WebSocket.
    Fallback path is Polymarket web REST `crypto/price-history` for the current
    window. The fallback is coarser, but still better than pretending Binance is
    the official settlement source.
    """

    def __init__(self, symbol: str = "BTC"):
        self.symbol = symbol.upper()
        self._lock = threading.Lock()
        self._latest: Optional[ReferencePrice] = None
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

    def get_latest(self, max_age: Optional[float] = None) -> Optional[ReferencePrice]:
        with self._lock:
            latest = self._latest
        if not latest:
            return None
        age = time.time() - latest.timestamp
        if max_age is not None and age > max_age:
            return None
        return ReferencePrice(
            price=latest.price,
            source=latest.source,
            timestamp=latest.timestamp,
            age_seconds=age,
        )

    def update_from_rest(self, window_ts: int, window_end_ts: int) -> Optional[ReferencePrice]:
        """Fetch the latest Polymarket web price-history point for this window."""
        try:
            params = urllib.parse.urlencode(
                {
                    "symbol": self.symbol,
                    "eventStartTime": iso_utc(window_ts),
                    "variant": "fiveminute",
                    "endDate": iso_utc(window_end_ts),
                }
            )
            url = f"{POLYMARKET_WEB_API}/crypto/price-history?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "PolyBot/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, list) or not data:
                return None
            last = data[-1]
            price = float(last.get("value", 0.0))
            ts_ms = int(last.get("timestamp", 0))
            if price <= 0 or ts_ms <= 0:
                return None
            ref = ReferencePrice(
                price=price,
                source="polymarket_price_history",
                timestamp=ts_ms / 1000.0,
                age_seconds=max(0.0, time.time() - (ts_ms / 1000.0)),
            )
            with self._lock:
                if self._latest is None or ref.timestamp >= self._latest.timestamp:
                    self._latest = ref
            return ref
        except Exception:
            return None

    def _set_latest(self, price: float, timestamp_ms: int, source: str) -> None:
        if price <= 0 or timestamp_ms <= 0:
            return
        ref = ReferencePrice(
            price=float(price),
            source=source,
            timestamp=float(timestamp_ms) / 1000.0,
            age_seconds=max(0.0, time.time() - (float(timestamp_ms) / 1000.0)),
        )
        with self._lock:
            if self._latest is None or ref.timestamp >= self._latest.timestamp:
                self._latest = ref

    def _ws_loop(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception:
            return

        def on_open(ws):
            msg = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        # Polymarket RTDS is sensitive to this being compact JSON.
                        # Default json.dumps emits spaces and was observed to return
                        # only the initial crypto_prices snapshot, not live Chainlink updates.
                        "filters": json.dumps(
                            {"symbol": f"{self.symbol.lower()}/usd"},
                            separators=(",", ":"),
                        ),
                    }
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
            if not message or not message.strip():
                return
            try:
                obj = json.loads(message)
            except Exception:
                return
            if not isinstance(obj, dict) or obj.get("topic") != "crypto_prices_chainlink":
                return
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                return
            data = payload.get("data")
            if isinstance(data, list):
                for point in data:
                    if isinstance(point, dict):
                        self._set_latest(
                            float(point.get("value", 0.0)),
                            int(point.get("timestamp", 0)),
                            "polymarket_rtds_chainlink",
                        )
            else:
                price = payload.get("value", payload.get("price", 0.0))
                ts = payload.get("timestamp", obj.get("timestamp", 0))
                self._set_latest(float(price or 0.0), int(ts or 0), "polymarket_rtds_chainlink")

        def on_error(ws, error):
            return

        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    POLYMARKET_RTDS_WS,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                )
                ws.run_forever(ping_interval=20, ping_timeout=5, origin="https://polymarket.com")
            except Exception:
                pass
            if self._running:
                time.sleep(5)


class SourceConsensusGate:
    def __init__(self, config: SourceConsensusConfig):
        self.config = config
        self._basis_samples: deque[tuple[float, float]] = deque(maxlen=max(1, config.basis_window))
        self._seen_sample_timestamps: deque[float] = deque(maxlen=max(1, config.basis_window * 4))
        self._latest_snapshot: Optional[SourceConsensusDecision] = None
        self._lock = threading.Lock()

    @property
    def basis_mean_bps(self) -> float:
        # Backward-compatible name. This is deliberately a rolling median now,
        # not a mean of the last 24 hot-loop ticks. The prior implementation
        # sampled every assess() call, so "24" meant ~2-3 seconds, not 24 windows.
        with self._lock:
            values = [basis for _, basis in self._basis_samples]
        if len(values) >= self.config.min_basis_samples:
            return float(statistics.median(values))
        return self.config.default_basis_bps

    @property
    def basis_sample_count(self) -> int:
        with self._lock:
            return len(self._basis_samples)

    def record_basis(
        self,
        reference_price: float,
        binance_price: float,
        sample_timestamp: Optional[float] = None,
    ) -> Optional[float]:
        if reference_price <= 0 or binance_price <= 0:
            return None
        ts = time.time() if sample_timestamp is None else float(sample_timestamp)
        basis = (reference_price - binance_price) / binance_price * 10000.0
        with self._lock:
            if any(abs(ts - seen) < 1e-6 for seen in self._seen_sample_timestamps):
                return None
            self._basis_samples.append((ts, basis))
            self._seen_sample_timestamps.append(ts)
        return basis

    def update_snapshot(
        self,
        *,
        binance_price: float,
        opening_price: float,
        intended_side: str,
        reference: Optional[ReferencePrice],
    ) -> SourceConsensusDecision:
        decision = self.assess(
            binance_price=binance_price,
            opening_price=opening_price,
            intended_side=intended_side,
            reference=reference,
            record_sample=True,
        )
        with self._lock:
            self._latest_snapshot = decision
        return decision

    def assess_snapshot(self, intended_side: str) -> SourceConsensusDecision:
        with self._lock:
            snapshot = self._latest_snapshot
        if snapshot is None:
            return self._decision(
                "skip",
                "source_snapshot_missing",
                0.0,
                0.0,
                intended_side,
                None,
                self.basis_mean_bps,
                None,
                None,
            )
        if snapshot.adjusted_side != intended_side:
            return self._decision(
                "skip",
                "source_snapshot_side_disagrees",
                0.0,
                snapshot.adjusted_price,
                snapshot.adjusted_side,
                snapshot.basis_bps,
                snapshot.basis_mean_bps,
                snapshot.basis_deviation_bps,
                ReferencePrice(
                    price=snapshot.reference_price or 0.0,
                    source="snapshot",
                    timestamp=time.time() - float(snapshot.reference_age_seconds or 0.0),
                    age_seconds=float(snapshot.reference_age_seconds or 0.0),
                ) if snapshot.reference_price else None,
            )
        return snapshot

    def assess(
        self,
        *,
        binance_price: float,
        opening_price: float,
        intended_side: str,
        reference: Optional[ReferencePrice],
        record_sample: bool = True,
    ) -> SourceConsensusDecision:
        mean_basis = self.basis_mean_bps
        adjusted_price = binance_price * (1.0 + mean_basis / 10000.0) if binance_price > 0 else 0.0
        adjusted_side = "UP" if adjusted_price >= opening_price else "DOWN"

        if not self.config.enabled:
            return SourceConsensusDecision(
                action="normal",
                reason="source_consensus_disabled",
                size_multiplier=1.0,
                adjusted_price=adjusted_price,
                adjusted_side=adjusted_side,
                basis_bps=None,
                basis_mean_bps=mean_basis,
                basis_deviation_bps=None,
                reference_price=None,
                reference_age_seconds=None,
            )

        if opening_price <= 0 or binance_price <= 0:
            return self._decision("skip", "missing_price_for_source_consensus", 0.0, adjusted_price, adjusted_side, None, mean_basis, None, reference)

        if adjusted_side != intended_side:
            return self._decision("skip", "basis_adjusted_binance_side_disagrees", 0.0, adjusted_price, adjusted_side, None, mean_basis, None, reference)

        if reference is None:
            if self.config.require_live_reference:
                return self._decision("skip", "missing_polymarket_chainlink_reference", 0.0, adjusted_price, adjusted_side, None, mean_basis, None, reference)
            return self._decision("downsize", "missing_reference_downsize", self.config.downsize_factor, adjusted_price, adjusted_side, None, mean_basis, None, reference)

        basis = self.record_basis(reference.price, binance_price, sample_timestamp=reference.timestamp) if record_sample else (reference.price - binance_price) / binance_price * 10000.0
        deviation = abs((basis or mean_basis) - mean_basis)
        reference_side = "UP" if reference.price >= opening_price else "DOWN"

        if reference.age_seconds > self.config.stale_skip_seconds:
            return self._decision("skip", "polymarket_chainlink_reference_stale", 0.0, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)
        if reference_side != intended_side:
            return self._decision("skip", "polymarket_chainlink_direction_disagrees", 0.0, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)
        if deviation > self.config.max_basis_deviation_bps:
            return self._decision("skip", "basis_deviation_too_large", 0.0, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)
        if reference.age_seconds > self.config.stale_downsize_seconds:
            return self._decision("downsize", "polymarket_chainlink_reference_mildly_stale", self.config.downsize_factor, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)
        if deviation > self.config.downsize_basis_deviation_bps:
            return self._decision("downsize", "basis_deviation_downsize", self.config.downsize_factor, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)
        return self._decision("normal", "sources_agree", 1.0, adjusted_price, adjusted_side, basis, mean_basis, deviation, reference)

    def _decision(
        self,
        action: str,
        reason: str,
        multiplier: float,
        adjusted_price: float,
        adjusted_side: str,
        basis: Optional[float],
        mean_basis: float,
        deviation: Optional[float],
        reference: Optional[ReferencePrice],
    ) -> SourceConsensusDecision:
        return SourceConsensusDecision(
            action=action,
            reason=reason,
            size_multiplier=multiplier,
            adjusted_price=adjusted_price,
            adjusted_side=adjusted_side,
            basis_bps=basis,
            basis_mean_bps=mean_basis,
            basis_deviation_bps=deviation,
            reference_price=reference.price if reference else None,
            reference_age_seconds=reference.age_seconds if reference else None,
        )


def fetch_binance_kline_open_close(window_ts: int, period_seconds: int = 300) -> Optional[tuple[float, float]]:
    try:
        params = urllib.parse.urlencode(
            {
                "symbol": "BTCUSDT",
                "interval": "5m" if period_seconds == 300 else "15m",
                "startTime": int(window_ts) * 1000,
                "endTime": int(window_ts + period_seconds) * 1000,
                "limit": 1,
            }
        )
        req = urllib.request.Request(f"{BINANCE_KLINES_API}?{params}", headers={"User-Agent": "PolyBot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if not data:
            return None
        return float(data[0][1]), float(data[0][4])
    except Exception:
        return None
