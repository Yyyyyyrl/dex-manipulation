"""Read-only, bounded telemetry primitives for live operator surfaces.

Telemetry is deliberately separate from command transport.  Publishers replace
one immutable value per source and never wait for a browser or network client.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
import threading
import time
from typing import Callable, Mapping


class TelemetryHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    FAULT = "fault"


@dataclass(frozen=True)
class ControlLoopTelemetry:
    """Exact immutable references retained from one completed control tick."""

    tick: int
    actual_time_ns: int
    scheduled_time_ns: int
    lateness_ns: int
    control_period_ns: int
    state: str
    hand_owner: str
    arm_owner: str
    control_epoch: int
    manus_sample: object
    manus_source_status: object
    teleop_candidate: object
    policy_candidate: object | None
    requested_candidate: object | None
    hand_state: object
    authorized_command: object | None
    gateway_acknowledgement: object | None
    effective_target: object
    readiness: object
    mapping_preview: object
    blend_alpha: float | None
    rejection_reason: str | None

    def __post_init__(self) -> None:
        if self.tick < 0:
            raise ValueError("control telemetry tick must be non-negative")
        if min(
            self.actual_time_ns,
            self.scheduled_time_ns,
            self.lateness_ns,
            self.control_period_ns,
            self.control_epoch,
        ) < 0:
            raise ValueError("control telemetry time and epoch values must be non-negative")
        if self.control_period_ns == 0:
            raise ValueError("control telemetry period must be positive")
        if not self.state or not self.hand_owner or not self.arm_owner:
            raise ValueError("control telemetry state and owners are required")


JsonScalar = str | int | float | bool | None
FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def _freeze_json(value: object, *, path: str = "payload", depth: int = 0) -> FrozenJson:
    if depth > 24:
        raise ValueError(f"{path} exceeds the maximum telemetry nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJson] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}", depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise ValueError(f"{path} contains unsupported type {type(value).__name__}")


def _thaw_json(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class TelemetryEnvelope:
    source: str
    sequence: int
    sample_monotonic_ns: int
    received_monotonic_ns: int
    rate_hz: float
    dropped_since_last: int
    health: TelemetryHealth
    payload: Mapping[str, object]
    stale_after_ns: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported telemetry schema version")
        if not self.source or len(self.source) > 64:
            raise ValueError("telemetry source must be 1..64 characters")
        if self.sequence < 0:
            raise ValueError("telemetry sequence must be non-negative")
        if self.sample_monotonic_ns < 0 or self.received_monotonic_ns < 0:
            raise ValueError("telemetry timestamps must be non-negative")
        if not math.isfinite(self.rate_hz) or self.rate_hz < 0:
            raise ValueError("telemetry rate must be finite and non-negative")
        if self.dropped_since_last < 0:
            raise ValueError("telemetry drop count must be non-negative")
        if self.stale_after_ns is not None and self.stale_after_ns <= 0:
            raise ValueError("telemetry stale threshold must be positive")
        object.__setattr__(self, "health", TelemetryHealth(self.health))
        frozen = _freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise ValueError("telemetry payload must be a mapping")
        object.__setattr__(self, "payload", frozen)

    def as_dict(self, now_ns: int) -> dict[str, object]:
        age_ns = max(0, now_ns - self.received_monotonic_ns)
        health = self.health
        if (
            self.stale_after_ns is not None
            and age_ns > self.stale_after_ns
            and health in (TelemetryHealth.HEALTHY, TelemetryHealth.DEGRADED)
        ):
            health = TelemetryHealth.STALE
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "sequence": self.sequence,
            "sample_monotonic_ns": self.sample_monotonic_ns,
            "received_monotonic_ns": self.received_monotonic_ns,
            "age_ms": round(age_ns / 1_000_000, 3),
            "rate_hz": round(self.rate_hz, 3),
            "dropped_since_last": self.dropped_since_last,
            "health": health.value,
            "payload": _thaw_json(self.payload),
        }


class TelemetryHub:
    """One latest immutable envelope per source, safe for many readers."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        self._condition = threading.Condition()
        self._latest: dict[str, TelemetryEnvelope] = {}
        self._revision = 0

    def publish(self, envelope: TelemetryEnvelope) -> int:
        with self._condition:
            previous = self._latest.get(envelope.source)
            if previous is not None and envelope.sequence <= previous.sequence:
                raise ValueError(
                    f"telemetry sequence for {envelope.source!r} must increase "
                    f"({envelope.sequence} <= {previous.sequence})"
                )
            self._latest[envelope.source] = envelope
            self._revision += 1
            self._condition.notify_all()
            return self._revision

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    def wait_for_revision(self, after_revision: int, timeout_s: float) -> int:
        if timeout_s < 0:
            raise ValueError("timeout must be non-negative")
        with self._condition:
            if self._revision <= after_revision:
                self._condition.wait(timeout_s)
            return self._revision

    def snapshot(self, *, now_ns: int | None = None) -> dict[str, object]:
        if now_ns is None:
            now_ns = self._clock_ns()
        with self._condition:
            revision = self._revision
            latest = dict(self._latest)
        return {
            "schema_version": 1,
            "revision": revision,
            "server_monotonic_ns": now_ns,
            "sources": {
                source: envelope.as_dict(now_ns)
                for source, envelope in sorted(latest.items())
            },
        }
