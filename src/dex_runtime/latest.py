"""Bounded latest-value transport with explicit overwrite accounting."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PublishResult:
    sequence: int
    replaced_unread: bool
    total_replaced_unread: int


class LatestValueBuffer(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._value: T | None = None
        self._sequence = -1
        self._read_sequence = -1
        self._replaced_unread = 0

    def publish(self, value: T) -> PublishResult:
        with self._condition:
            replaced = self._value is not None and self._read_sequence < self._sequence
            if replaced:
                self._replaced_unread += 1
            self._sequence += 1
            self._value = value
            self._condition.notify_all()
            return PublishResult(self._sequence, replaced, self._replaced_unread)

    def take_latest(self, timeout_s: float | None = None) -> tuple[int, T] | None:
        with self._condition:
            if self._value is None or self._read_sequence == self._sequence:
                self._condition.wait(timeout_s)
            if self._value is None or self._read_sequence == self._sequence:
                return None
            self._read_sequence = self._sequence
            return self._sequence, self._value

    @property
    def total_replaced_unread(self) -> int:
        with self._lock:
            return self._replaced_unread
