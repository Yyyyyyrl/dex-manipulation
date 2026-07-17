"""System and deterministic monotonic clocks."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class MonotonicClock(Protocol):
    def now_ns(self) -> int: ...


class SystemClock:
    def now_ns(self) -> int:
        return time.monotonic_ns()


class FakeClock:
    def __init__(self, initial_ns: int = 0) -> None:
        if initial_ns < 0:
            raise ValueError("initial fake time must be non-negative")
        self._value = initial_ns
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            return self._value

    def advance_ns(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("monotonic clocks cannot move backwards")
        with self._lock:
            self._value += delta_ns
            return self._value

    def set_ns(self, value_ns: int) -> None:
        with self._lock:
            if value_ns < self._value:
                raise ValueError("monotonic clocks cannot move backwards")
            self._value = value_ns
