"""Canonical JSONL event and bounded-rate control-trace writers."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dex_contracts import canonical_json, to_primitive


class JsonlWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._closed = False

    def write(self, value: Mapping[str, Any]) -> None:
        primitive = to_primitive(dict(value))
        line = canonical_json(primitive)
        with self._lock:
            if self._closed:
                raise RuntimeError("JSONL writer is closed")
            self._stream.write(line + "\n")
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.close()
                self._closed = True

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


@dataclass(frozen=True)
class RuntimeEvent:
    monotonic_time_ns: int
    wall_time_utc: str
    control_session_id: str
    event_type: str
    state: str
    requested_transition: str | None
    hand_owner: str
    arm_owner: str
    control_epoch: int
    policy_package_id: str | None
    readiness: Mapping[str, Any] | None
    reason_code: str | None
    deadline_ns: int | None
    gateway_acknowledgement: Mapping[str, Any] | None
    safe_response: str | None
    operator_action: str | None


class EventLogger:
    def __init__(self, path: str | Path) -> None:
        self._writer = JsonlWriter(path)

    def emit(self, event: RuntimeEvent) -> None:
        self._writer.write({"record_type": "event", **to_primitive(event)})

    def close(self) -> None:
        self._writer.close()


class ControlTraceRecorder:
    def __init__(self, path: str | Path, *, minimum_period_ns: int) -> None:
        if minimum_period_ns <= 0:
            raise ValueError("trace minimum period must be positive")
        self._writer = JsonlWriter(path)
        self.minimum_period_ns = minimum_period_ns
        self._last_recorded_ns: int | None = None
        self.recorded_count = 0
        self.rate_limited_count = 0

    def record(
        self,
        *,
        monotonic_time_ns: int,
        control_session_id: str,
        state: str,
        hand_owner: str,
        arm_owner: str,
        control_epoch: int,
        policy_package_id: str | None,
        payload: Mapping[str, Any],
    ) -> bool:
        if monotonic_time_ns < 0:
            raise ValueError("trace time must be monotonic and non-negative")
        if (
            self._last_recorded_ns is not None
            and monotonic_time_ns - self._last_recorded_ns < self.minimum_period_ns
        ):
            self.rate_limited_count += 1
            return False
        self._writer.write(
            {
                "record_type": "control-trace",
                "monotonic_time_ns": monotonic_time_ns,
                "control_session_id": control_session_id,
                "state": state,
                "hand_owner": hand_owner,
                "arm_owner": arm_owner,
                "control_epoch": control_epoch,
                "policy_package_id": policy_package_id,
                "payload": dict(payload),
            }
        )
        self._last_recorded_ns = monotonic_time_ns
        self.recorded_count += 1
        return True

    def close(self) -> None:
        self._writer.close()
