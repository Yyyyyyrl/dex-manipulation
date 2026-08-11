"""Strict latest-value listener for dex_teleop Hitbot cycle telemetry."""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

MAX_HEALTHY_CYCLE_LATENCY_MS = 200.0
MAX_HEALTHY_SERVO_INTERVAL_MS = 150.0
HEARTBEAT_SOURCE = "dex-teleop-hitbot-controller-heartbeat"


class ArmTelemetryListener:
    """Receive read-only localhost datagrams; never imports a robot SDK."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8780,
        *,
        stale_after_ns: int = 500_000_000,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("arm telemetry must bind to a loopback address")
        if not 0 <= port <= 65535 or stale_after_ns <= 0:
            raise ValueError("valid UDP port and positive stale threshold are required")
        self.stale_after_ns = stale_after_ns
        self._clock_ns = clock_ns
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self.port = int(self._sock.getsockname()[1])
        self._sock.settimeout(0.25)
        self._lock = threading.Lock()
        self._latest: dict[str, object] | None = None
        self._receive_ns: int | None = None
        self._heartbeat_receive_ns: int | None = None
        self._last_sequence: int | None = None
        self._intervals_ns: deque[int] = deque(maxlen=60)
        self._trail_actual: deque[list[float]] = deque(maxlen=200)
        self._trail_target: deque[list[float]] = deque(maxlen=200)
        self._dropped = 0
        self._rejected = 0
        self._last_error = "waiting-for-cycle"
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._receive_loop,
            name="hitbot-telemetry-listener",
            daemon=True,
        )

    def start(self) -> None:
        if self._thread.is_alive():
            raise RuntimeError("arm telemetry listener is already running")
        self._thread.start()

    def _reject(self, reason: str) -> None:
        with self._lock:
            self._rejected += 1
            self._last_error = reason

    @staticmethod
    def _vector(
        payload: dict[str, object],
        name: str,
        length: int,
        *,
        optional: bool = False,
    ) -> None:
        value = payload.get(name)
        if value is None and optional:
            return
        if (
            not isinstance(value, list)
            or len(value) != length
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in value
            )
        ):
            raise ValueError(f"invalid-{name}")

    def ingest_payload(
        self,
        payload: dict[str, object],
        *,
        received_time_ns: int | None = None,
    ) -> bool:
        received_ns = self._clock_ns() if received_time_ns is None else received_time_ns
        try:
            if payload.get("schema_version") != 1:
                raise ValueError("schema-version-mismatch")
            if payload.get("source") != "dex-teleop-hitbot-control-cycle":
                raise ValueError("source-mismatch")
            if payload.get("mode") != "live":
                raise ValueError("mode-mismatch")
            sequence = payload.get("source_sequence")
            sample_ns = payload.get("sample_monotonic_ns")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("invalid-source-sequence")
            if isinstance(sample_ns, bool) or not isinstance(sample_ns, int) or sample_ns < 0:
                raise ValueError("invalid-sample-time")
            self._vector(payload, "tracker_pose", 7, optional=True)
            self._vector(payload, "transformed_delta", 7)
            self._vector(payload, "tcp_actual", 6, optional=True)
            self._vector(payload, "tcp_target", 6, optional=True)
            self._vector(payload, "ik_target", 6, optional=True)
            self._vector(payload, "ik_result", 6, optional=True)
            for field in (
                "tcp_query_ms",
                "ik_ms",
                "servo_call_ms",
                "cycle_latency_ms",
            ):
                value = payload.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value < 0
                ):
                    raise ValueError(f"invalid-{field}")
            interval = payload.get("servo_interval_ms")
            if interval is not None and (
                isinstance(interval, bool)
                or not isinstance(interval, (int, float))
                or not math.isfinite(float(interval))
                or interval < 0
            ):
                raise ValueError("invalid-servo_interval_ms")
            if not isinstance(payload.get("cycle_success"), bool):
                raise ValueError("invalid-cycle-success")
            control_mode = payload.get("control_mode", "teleop")
            if control_mode not in ("teleop", "hold"):
                raise ValueError("invalid-control-mode")
            hold_state = payload.get("hold_state", "TELEOP")
            if not isinstance(hold_state, str) or not hold_state:
                raise ValueError("invalid-hold-state")
            hold_verified = payload.get("hold_verified", False)
            if not isinstance(hold_verified, bool):
                raise ValueError("invalid-hold-verified")
            for field in ("hold_position_error_mm", "hold_orientation_error_deg"):
                value = payload.get(field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value < 0
                ):
                    raise ValueError(f"invalid-{field}")
            with self._lock:
                previous_sequence = self._last_sequence
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError("non-monotonic-source-sequence")
        except ValueError as exc:
            self._reject(str(exc))
            return False

        normalized = dict(payload)
        normalized["received_monotonic_ns"] = received_ns
        normalized["stale_after_ns"] = self.stale_after_ns
        with self._lock:
            if self._receive_ns is not None and received_ns > self._receive_ns:
                self._intervals_ns.append(received_ns - self._receive_ns)
            if self._last_sequence is not None:
                self._dropped += max(0, sequence - self._last_sequence - 1)
            self._latest = normalized
            actual = normalized.get("tcp_actual")
            target = normalized.get("tcp_target")
            if isinstance(actual, list) and isinstance(target, list):
                self._trail_actual.append(list(actual))
                self._trail_target.append(list(target))
            self._receive_ns = received_ns
            self._last_sequence = sequence
            self._last_error = ""
        return True

    def ingest_heartbeat(
        self,
        payload: dict[str, object],
        *,
        received_time_ns: int | None = None,
    ) -> bool:
        """Record controller liveness without fabricating a motion sample."""

        received_ns = self._clock_ns() if received_time_ns is None else received_time_ns
        try:
            if payload.get("schema_version") != 1:
                raise ValueError("heartbeat-schema-version-mismatch")
            if payload.get("source") != HEARTBEAT_SOURCE:
                raise ValueError("heartbeat-source-mismatch")
            if payload.get("mode") != "live":
                raise ValueError("heartbeat-mode-mismatch")
            for field in ("controller_monotonic_ns", "sent_monotonic_ns"):
                value = payload.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"invalid-{field}")
        except ValueError as exc:
            self._reject(str(exc))
            return False
        with self._lock:
            self._heartbeat_receive_ns = received_ns
        return True

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(32768)
            except TimeoutError:
                continue
            except OSError:
                break
            if address[0] != "127.0.0.1":
                self._reject("non-loopback-sender")
                continue
            if len(data) > 16384:
                self._reject("datagram-too-large")
                continue
            try:
                decoded = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._reject("malformed-json")
                continue
            if not isinstance(decoded, dict):
                self._reject("payload-not-object")
                continue
            if decoded.get("source") == HEARTBEAT_SOURCE:
                self.ingest_heartbeat(decoded)
            else:
                self.ingest_payload(decoded)

    def snapshot(self) -> dict[str, object]:
        now_ns = self._clock_ns()
        with self._lock:
            latest = None if self._latest is None else dict(self._latest)
            receive_ns = self._receive_ns
            heartbeat_receive_ns = self._heartbeat_receive_ns
            intervals = tuple(self._intervals_ns)
            dropped = self._dropped
            rejected = self._rejected
            error = self._last_error
            trail_actual = list(self._trail_actual)
            trail_target = list(self._trail_target)
        liveness_receive_ns = max(
            value for value in (receive_ns, heartbeat_receive_ns, 0) if value is not None
        )
        if latest is None or receive_ns is None:
            heartbeat_fresh = (
                heartbeat_receive_ns is not None
                and now_ns - heartbeat_receive_ns <= self.stale_after_ns
            )
            return {
                "connected": heartbeat_fresh,
                "mode": "live",
                "source_sequence": None,
                "sample_monotonic_ns": 0,
                "received_monotonic_ns": liveness_receive_ns,
                "rate_hz": 0.0,
                "dropped_since_last": dropped,
                "rejected_frames": rejected,
                "source_health": "healthy" if heartbeat_fresh else "stale",
                "source_reason": "waiting-for-cycle" if heartbeat_fresh else error,
                "stale_after_ns": self.stale_after_ns,
                "trail_actual": trail_actual,
                "trail_target": trail_target,
            }
        age_ns = max(0, now_ns - liveness_receive_ns)
        cycle_age_ns = max(0, now_ns - receive_ns)
        cycle_fresh = cycle_age_ns <= self.stale_after_ns
        cycle_success = bool(latest.get("cycle_success"))
        connected = bool(latest.get("connected"))
        cycle_latency_ms = float(latest.get("cycle_latency_ms", 0.0))
        servo_interval = latest.get("servo_interval_ms")
        hold_state = str(latest.get("hold_state") or "TELEOP")
        if hold_state == "FAULT_HOLD":
            health = "fault"
            reason = str(latest.get("failure_reason") or "arm-hold-fault")
        elif age_ns > self.stale_after_ns:
            health = "stale"
            reason = "source-stale"
        elif cycle_latency_ms > MAX_HEALTHY_CYCLE_LATENCY_MS:
            health = "degraded"
            reason = "cycle-latency-high"
        elif (
            isinstance(servo_interval, (int, float))
            and not isinstance(servo_interval, bool)
            and float(servo_interval) > MAX_HEALTHY_SERVO_INTERVAL_MS
        ):
            health = "degraded"
            reason = "servo-interval-high"
        elif cycle_success:
            health = "healthy"
            reason = ""
        elif connected:
            health = "degraded"
            reason = str(latest.get("failure_reason") or "cycle-failed")
        else:
            health = "fault"
            reason = str(latest.get("failure_reason") or "robot-unavailable")
        measured_cycle_rate_hz = (
            1_000_000_000 / (sum(intervals) / len(intervals)) if intervals else 0.0
        )
        latest.update(
            {
                "received_monotonic_ns": liveness_receive_ns,
                "cycle_received_monotonic_ns": receive_ns,
                "cycle_age_ms": cycle_age_ns / 1_000_000,
                "motion_sample_fresh": cycle_fresh,
                "controller_alive": age_ns <= self.stale_after_ns,
                "controller_heartbeat_age_ms": (
                    None
                    if heartbeat_receive_ns is None
                    else max(0, now_ns - heartbeat_receive_ns) / 1_000_000
                ),
                "rate_hz": round(measured_cycle_rate_hz, 3) if cycle_fresh else 0.0,
                "dropped_since_last": dropped,
                "rejected_frames": rejected,
                "source_health": health,
                "source_reason": reason,
                "stale_after_ns": self.stale_after_ns,
                "trail_actual": trail_actual,
                "trail_target": trail_target,
            }
        )
        return latest

    def stop(self, timeout_s: float = 1.0) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise TimeoutError("arm telemetry listener did not stop")
