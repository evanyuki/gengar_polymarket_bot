"""Markov persistence filter for short-horizon BTC direction.

Tracks directional self-transition probability p(j*, j*) where j* is the
candidate trade direction (UP or DOWN). The bot may enter only when the
candidate direction persists strongly enough.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


Direction = str


@dataclass
class MarkovPersistenceFilter:
    lookback_seconds: float = 60.0
    tick_threshold_pct: float = 0.005
    min_transitions: int = 8
    _samples: Deque[tuple[float, float]] = field(default_factory=deque)
    _directions: Deque[tuple[float, Direction]] = field(default_factory=deque)

    def reset(self) -> None:
        self._samples.clear()
        self._directions.clear()

    def update(self, price: float, now: Optional[float] = None) -> Direction:
        if price <= 0:
            return "FLAT"
        ts = time.time() if now is None else now

        direction = "FLAT"
        if self._samples:
            prev_price = self._samples[-1][1]
            if prev_price > 0:
                change_pct = (price - prev_price) / prev_price * 100.0
                if change_pct > self.tick_threshold_pct:
                    direction = "UP"
                elif change_pct < -self.tick_threshold_pct:
                    direction = "DOWN"

        self._samples.append((ts, price))
        if len(self._samples) > 1:
            self._directions.append((ts, direction))
        self._trim(ts)
        return direction

    def transition_stats(self, direction: Direction) -> dict:
        """Return self-transition diagnostics for a candidate direction.

        `total` is the number of observed transitions whose previous state was
        the candidate direction. `same` is how many of those stayed in the same
        direction. Persistence is deliberately reported as 0 until total meets
        min_transitions, matching the trading gate.
        """
        direction = direction.upper()
        raw_dirs = [d for _, d in self._directions]
        dirs = [d for d in raw_dirs if d in {"UP", "DOWN"}]
        total = 0
        same = 0
        if direction in {"UP", "DOWN"} and len(dirs) >= 2:
            for prev, cur in zip(dirs, dirs[1:]):
                if prev != direction:
                    continue
                total += 1
                if cur == direction:
                    same += 1
        persistence = same / total if total >= self.min_transitions and total else 0.0
        return {
            "direction": direction,
            "persistence": persistence,
            "same": same,
            "total": total,
            "directional_samples": len(dirs),
            "flat_samples": sum(1 for d in raw_dirs if d == "FLAT"),
            "raw_samples": len(raw_dirs),
            "min_transitions": self.min_transitions,
        }

    def persistence(self, direction: Direction) -> float:
        return float(self.transition_stats(direction)["persistence"])

    def passes(self, direction: Direction, threshold: float = 0.87) -> bool:
        return self.persistence(direction) >= threshold

    def _trim(self, now: float) -> None:
        cutoff = now - self.lookback_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        while self._directions and self._directions[0][0] < cutoff:
            self._directions.popleft()
