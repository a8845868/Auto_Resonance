"""Estimate train arrival time from occasional remaining-distance OCR samples."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Optional


_DISTANCE_RE = re.compile(
    r"(?:剩余(?:行程|距离)|距(?:离)?目的地)[：:\s]*([0-9][0-9,.]*(?:\.[0-9]+)?)\s*(km|公里|m|米)?",
    re.IGNORECASE,
)


def parse_remaining_distance(items: Iterable[dict | str]) -> Optional[float]:
    """Return the remaining distance in kilometres from OCR output."""
    texts = [
        str(item.get("text", "") if isinstance(item, dict) else item).replace("，", ",")
        for item in items
    ]
    # PaddleOCR may return the label and number as one box or as adjacent boxes.
    for text in [*texts, "".join(texts)]:
        match = _DISTANCE_RE.search(text)
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (match.group(2) or "km").lower()
        return value / 1000 if unit in {"m", "米"} else value
    return None


@dataclass
class TrainArrivalEstimator:
    """Rolling, outlier-resistant speed and ETA estimator."""

    max_samples: int = 5
    _last_distance: Optional[float] = None
    _last_time: Optional[float] = None
    _speeds: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._speeds = deque(self._speeds, maxlen=self.max_samples)

    @property
    def speed(self) -> Optional[float]:
        """Current median speed in kilometres per second."""
        return median(self._speeds) if self._speeds else None

    def observe(self, distance_km: float, now: float) -> Optional[float]:
        """Add an OCR sample and return the updated ETA in seconds."""
        if self._last_distance is not None and self._last_time is not None:
            elapsed = now - self._last_time
            travelled = self._last_distance - distance_km
            if elapsed > 0 and travelled > 0:
                candidate = travelled / elapsed
                current = self.speed
                # OCR occasionally drops/adds a digit. Reject extreme jumps once
                # a baseline exists, but let later valid samples replace it.
                if current is None or 0.2 * current <= candidate <= 5 * current:
                    self._speeds.append(candidate)
        self._last_distance = distance_km
        self._last_time = now
        return self.eta(distance_km)

    def eta(self, distance_km: float) -> Optional[float]:
        speed = self.speed
        return distance_km / speed if speed and speed > 0 else None


def polling_interval(eta_seconds: Optional[float], auto_pick: bool = False) -> float:
    """Choose a cheap far-away poll rate and a responsive near-station rate."""
    if eta_seconds is None or eta_seconds <= 30:
        interval = 0.3
    elif eta_seconds <= 180:
        interval = 2.0
    else:
        interval = 5.0
    # Auto-pick is an interactive feature and must remain reasonably responsive.
    return min(interval, 0.5) if auto_pick else interval
