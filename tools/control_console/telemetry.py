"""Adapters that publish demo/runtime snapshots into the read-only hub."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from dex_runtime.telemetry import TelemetryEnvelope, TelemetryHealth, TelemetryHub


def _payload_health(payload: dict[str, object]) -> TelemetryHealth:
    source_health = str(payload.get("source_health", "")).lower()
    if source_health == "healthy":
        return TelemetryHealth.HEALTHY
    if source_health == "degraded":
        return TelemetryHealth.DEGRADED
    if source_health in ("fault", "faulted"):
        return TelemetryHealth.FAULT
    if source_health in ("stale", "disconnected"):
        return TelemetryHealth.STALE
    return TelemetryHealth.HEALTHY if payload.get("connected") else TelemetryHealth.STALE


class SyntheticArmTelemetry:
    """Truthfully labelled hardware-free TCP tracking source for UI rehearsal."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._clock_ns = clock_ns
        self._started_ns = clock_ns()
        self._sequence = 0

    def snapshot(self) -> dict[str, object]:
        now_ns = self._clock_ns()
        elapsed_s = (now_ns - self._started_ns) / 1_000_000_000
        phase = elapsed_s * 0.65
        target = (
            430.0 + 72.0 * math.cos(phase),
            -20.0 + 92.0 * math.sin(phase),
            285.0 + 42.0 * math.sin(phase * 0.72),
            178.0,
            4.0 + 5.0 * math.sin(phase * 0.4),
            -12.0 + 8.0 * math.cos(phase * 0.45),
        )
        lag = 0.18
        actual = (
            430.0 + 72.0 * math.cos(phase - lag),
            -20.0 + 92.0 * math.sin(phase - lag),
            285.0 + 42.0 * math.sin((phase - lag) * 0.72),
            178.0,
            4.0 + 5.0 * math.sin((phase - lag) * 0.4),
            -12.0 + 8.0 * math.cos((phase - lag) * 0.45),
        )
        trail_target = []
        trail_actual = []
        for index in range(60):
            trail_phase = phase - (59 - index) * 0.035
            trail_target.append(
                [
                    430.0 + 72.0 * math.cos(trail_phase),
                    -20.0 + 92.0 * math.sin(trail_phase),
                    285.0 + 42.0 * math.sin(trail_phase * 0.72),
                ]
            )
            actual_phase = trail_phase - lag
            trail_actual.append(
                [
                    430.0 + 72.0 * math.cos(actual_phase),
                    -20.0 + 92.0 * math.sin(actual_phase),
                    285.0 + 42.0 * math.sin(actual_phase * 0.72),
                ]
            )
        snapshot = {
            "connected": True,
            "mode": "synthetic",
            "source_sequence": self._sequence,
            "sample_monotonic_ns": now_ns,
            "received_monotonic_ns": now_ns,
            "tracker_pose": [
                target[0] / 1000.0,
                target[1] / 1000.0,
                target[2] / 1000.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ],
            "transformed_delta": [round(target[index] - actual[index], 4) for index in range(6)],
            "tcp_actual": [round(value, 4) for value in actual],
            "tcp_target": [round(value, 4) for value in target],
            "trail_actual": [[round(value, 4) for value in point] for point in trail_actual],
            "trail_target": [[round(value, 4) for value in point] for point in trail_target],
            "ik_target": [
                round(4.0 + 6.0 * math.sin(phase + index * 0.35), 4) for index in range(6)
            ],
            "ik_ok": True,
            "servo_ok": True,
            "servo_interval_ms": 20.0,
            "tcp_query_ms": 3.4,
            "ik_latency_ms": 2.1,
            "total_latency_ms": 7.8,
            "consecutive_failures": 0,
            "failure_reason": None,
            "units": {"position": "mm", "orientation": "deg"},
            "rate_hz": 20.0,
            "dropped_since_last": 0,
        }
        self._sequence += 1
        return snapshot


class ConsoleTelemetryPump:
    """Samples read-only providers and publishes bounded display snapshots."""

    def __init__(
        self,
        hub: TelemetryHub,
        *,
        controller,
        vr=None,
        arm=None,
        camera=None,
        display_hz: float = 20.0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if display_hz <= 0:
            raise ValueError("display_hz must be positive")
        self.hub = hub
        self.controller = controller
        self.vr = vr
        self.arm = arm
        self.camera = camera
        self.display_hz = display_hz
        self._clock_ns = clock_ns
        self._sequence = {
            "runtime": 0,
            "openxr": 0,
            "linker": 0,
            "hitbot": 0,
            "d435": 0,
        }
        history_length = max(2, round(display_hz * 10.0))
        self._latency_history = {source: deque(maxlen=history_length) for source in self._sequence}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name="console-telemetry-pump",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("console telemetry pump did not stop")

    @staticmethod
    def _health(value: object) -> TelemetryHealth:
        try:
            return TelemetryHealth(str(value))
        except ValueError:
            return TelemetryHealth.DEGRADED

    def _publish(
        self,
        source: str,
        payload: dict[str, object],
        *,
        sample_ns: int,
        received_ns: int,
        rate_hz: float,
        health: TelemetryHealth,
        stale_after_ns: int,
        dropped: int = 0,
    ) -> None:
        observed_ns = self._clock_ns()
        latency_ms = None
        if source == "hitbot":
            for field in ("cycle_latency_ms", "total_latency_ms"):
                value = payload.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    latency_ms = float(value)
                    break
        elif received_ns > 0:
            latency_ms = max(0, observed_ns - received_ns) / 1_000_000
        if latency_ms is not None:
            self._latency_history[source].append(latency_ms)
        payload = dict(payload)
        payload["display_latency_ms"] = latency_ms
        payload["latency_history_ms"] = list(self._latency_history[source])
        sequence = self._sequence[source]
        self._sequence[source] += 1
        self.hub.publish(
            TelemetryEnvelope(
                source=source,
                sequence=sequence,
                sample_monotonic_ns=sample_ns,
                received_monotonic_ns=received_ns,
                rate_hz=rate_hz,
                dropped_since_last=max(0, dropped),
                health=health,
                payload=payload,
                stale_after_ns=stale_after_ns,
            )
        )

    def _runtime(self, now_ns: int) -> None:
        try:
            payload = self.controller.snapshot()
            if payload.get("fault"):
                health = TelemetryHealth.FAULT
            elif payload.get("stopped") or not payload.get("connected"):
                health = TelemetryHealth.DEGRADED
            else:
                health = TelemetryHealth.HEALTHY
        except BaseException as exc:  # snapshot failures are source-local
            payload = {
                "state": "FAULT",
                "message": f"Runtime snapshot failed: {type(exc).__name__}",
                "fault": str(exc),
                "switchable": False,
                "stopped": False,
            }
            health = TelemetryHealth.FAULT
        self._publish(
            "runtime",
            payload,
            sample_ns=now_ns,
            received_ns=now_ns,
            rate_hz=self.display_hz,
            health=health,
            stale_after_ns=250_000_000,
        )

    def _openxr(self, now_ns: int) -> None:
        runtime_payload = None
        runtime_snapshot = getattr(self.controller, "vr_control_snapshot", None)
        if runtime_snapshot is not None:
            try:
                candidate = runtime_snapshot()
                if candidate.get("nodes") and candidate.get("control_correlated"):
                    runtime_payload = candidate
            except BaseException:
                runtime_payload = None
        if runtime_payload is not None:
            payload = runtime_payload
            sample_ns = int(payload["sample_monotonic_ns"])
            received_ns = int(payload["received_monotonic_ns"])
            rate_hz = float(payload.get("rate_hz") or 0.0)
            dropped = int(payload.get("dropped_since_last") or 0)
            health = _payload_health(payload)
        elif self.vr is None:
            payload: dict[str, object] = {
                "connected": False,
                "mode": "off",
                "nodes": [],
                "source_sequence": None,
            }
            sample_ns = received_ns = 0
            rate_hz = 0.0
            health = TelemetryHealth.STALE
            dropped = 0
        else:
            try:
                payload = self.vr.snapshot()
                sample_ns = int(payload.get("sample_monotonic_ns") or now_ns)
                received_ns = int(payload.get("received_monotonic_ns") or 0)
                rate_hz = float(payload.get("rate_hz") or 0.0)
                dropped = int(payload.get("dropped_since_last") or 0)
                health = _payload_health(payload)
            except BaseException as exc:
                payload = {
                    "connected": False,
                    "nodes": [],
                    "fault": f"{type(exc).__name__}: {exc}",
                }
                sample_ns = received_ns = now_ns
                rate_hz = 0.0
                dropped = 0
                health = TelemetryHealth.FAULT
        self._publish(
            "openxr",
            payload,
            sample_ns=sample_ns,
            received_ns=received_ns,
            rate_hz=rate_hz,
            health=health,
            stale_after_ns=int(payload.get("stale_after_ns") or 500_000_000),
            dropped=dropped,
        )

    def _linker(self, now_ns: int) -> None:
        try:
            payload = self.controller.linker_snapshot()
            sample_ns = int(payload.get("sample_monotonic_ns") or now_ns)
            received_ns = int(payload.get("received_monotonic_ns") or sample_ns)
            rate_hz = float(payload.get("rate_hz") or 0.0)
            if payload.get("fault"):
                health = TelemetryHealth.FAULT
            elif not payload.get("connected"):
                health = TelemetryHealth.STALE
            elif payload.get("command_identity_match") is False:
                health = TelemetryHealth.DEGRADED
            else:
                health = self._health(payload.get("health", "healthy"))
        except BaseException as exc:
            payload = {
                "connected": False,
                "joints": [],
                "fault": f"{type(exc).__name__}: {exc}",
            }
            sample_ns = received_ns = now_ns
            rate_hz = 0.0
            health = TelemetryHealth.FAULT
        configured_gateway = getattr(getattr(self.controller, "gateway", None), "config", None)
        stale_ns = int(
            payload.get("stale_after_ns")
            or (
                configured_gateway.state_stale_ns if configured_gateway is not None else 250_000_000
            )
        )
        self._publish(
            "linker",
            payload,
            sample_ns=sample_ns,
            received_ns=received_ns,
            rate_hz=rate_hz,
            health=health,
            stale_after_ns=stale_ns,
        )

    def _hitbot(self, now_ns: int) -> None:
        if self.arm is None:
            payload: dict[str, object] = {
                "connected": False,
                "mode": "off",
                "source_sequence": None,
            }
            sample_ns = received_ns = 0
            rate_hz = 0.0
            health = TelemetryHealth.STALE
            dropped = 0
        else:
            try:
                payload = self.arm.snapshot()
                sample_ns = int(payload.get("sample_monotonic_ns") or now_ns)
                received_ns = int(payload.get("received_monotonic_ns") or sample_ns)
                rate_hz = float(payload.get("rate_hz") or 0.0)
                dropped = int(payload.get("dropped_since_last") or 0)
                health = _payload_health(payload)
            except BaseException as exc:
                payload = {
                    "connected": False,
                    "mode": "fault",
                    "fault": f"{type(exc).__name__}: {exc}",
                }
                sample_ns = received_ns = now_ns
                rate_hz = 0.0
                dropped = 0
                health = TelemetryHealth.FAULT
        self._publish(
            "hitbot",
            payload,
            sample_ns=sample_ns,
            received_ns=received_ns,
            rate_hz=rate_hz,
            health=health,
            stale_after_ns=int(payload.get("stale_after_ns") or 500_000_000),
            dropped=dropped,
        )

    def _d435(self, now_ns: int) -> None:
        if self.camera is None:
            payload: dict[str, object] = {
                "connected": False,
                "mode": "off",
                "source_sequence": None,
                "source_health": "stale",
                "source_reason": "camera-disabled",
            }
            sample_ns = received_ns = 0
            rate_hz = 0.0
            health = TelemetryHealth.STALE
        else:
            try:
                payload = self.camera.snapshot()
                sample_ns = int(payload.get("sample_monotonic_ns") or now_ns)
                received_ns = int(payload.get("received_monotonic_ns") or 0)
                rate_hz = float(payload.get("rate_hz") or 0.0)
                health = _payload_health(payload)
            except BaseException as exc:
                payload = {
                    "connected": False,
                    "mode": "fault",
                    "source_health": "fault",
                    "fault": f"{type(exc).__name__}: {exc}",
                }
                sample_ns = received_ns = now_ns
                rate_hz = 0.0
                health = TelemetryHealth.FAULT
        self._publish(
            "d435",
            payload,
            sample_ns=sample_ns,
            received_ns=received_ns,
            rate_hz=rate_hz,
            health=health,
            stale_after_ns=int(payload.get("stale_after_ns") or 1_000_000_000),
        )

    def publish_once(self) -> None:
        now_ns = self._clock_ns()
        self._runtime(now_ns)
        self._openxr(now_ns)
        self._linker(now_ns)
        self._hitbot(now_ns)
        self._d435(now_ns)

    def _loop(self) -> None:
        period_s = 1.0 / self.display_hz
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self.publish_once()
            next_tick += period_s
            wait_s = next_tick - time.monotonic()
            if wait_s <= 0:
                next_tick = time.monotonic()
                continue
            self._stop.wait(wait_s)
