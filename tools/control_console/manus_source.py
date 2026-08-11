"""Validated localhost Manus source shared by control and visualization.

The ROS 2 bridge sends native 25-node Manus frames over a bounded UDP
datagram.  This source validates a frame once, constructs the exact
``TimestampedSample`` consumed by the retargeter, and retains only its latest
immutable value for display diagnostics.  It never serializes UI data in the
runtime callback.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from dex_contracts import SourceHealth, TimestampedSample
from dex_teleop_adapters import ManusKeypoints, ManusSourceStatus

SCHEMA_VERSION = 1
LAYOUT_ID = "manus-raw-25-v1"
PARENT_IDS = (
    -1,
    0, 1, 2, 3,
    0, 5, 6, 7, 8,
    0, 10, 11, 12, 13,
    0, 15, 16, 17, 18,
    0, 20, 21, 22, 23,
)


class UdpManusSource:
    """A strict latest-value Manus source for a localhost ROS 2 bridge."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        source_id: str,
        hand_side: str,
        stale_after_ns: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the Manus bridge must bind to a loopback address")
        if not 0 <= port <= 65535:
            raise ValueError("the Manus bridge UDP port must be within 0..65535")
        if hand_side not in ("left", "right"):
            raise ValueError("Manus hand_side must be left or right")
        if not source_id or stale_after_ns <= 0:
            raise ValueError("source_id and a positive stale threshold are required")
        self.host = host
        self.port = port
        self.source_id = source_id
        self.hand_side = hand_side
        self.stale_after_ns = stale_after_ns
        self._clock_ns = clock_ns
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self.port = int(self._sock.getsockname()[1])
        self._sock.settimeout(0.25)
        self._lock = threading.RLock()
        self._callback: Callable[[TimestampedSample], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest: TimestampedSample | None = None
        self._latest_mode = "off"
        self._last_receive_ns: int | None = None
        self._last_source_sequence: int | None = None
        self._accepted_frames = 0
        self._rejected_frames = 0
        self._dropped_frames = 0
        self._last_error = "not-started"
        self._intervals_ns: deque[int] = deque(maxlen=60)

    def start(self, callback: Callable[[TimestampedSample], None]) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("UDP Manus source is already running")
            self._callback = callback
            self._stop.clear()
            self._last_error = "waiting-for-frame"
            self._thread = threading.Thread(
                target=self._receive_loop,
                name=f"{self.source_id}-udp",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError("UDP Manus source thread did not stop")
        with self._lock:
            self._thread = None
            self._callback = None

    def _reject(self, reason: str) -> None:
        with self._lock:
            self._rejected_frames += 1
            self._last_error = reason

    @staticmethod
    def _finite_coordinate(value: object, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be numeric")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{label} must be finite")
        return result

    def ingest_payload(
        self,
        payload: Mapping[str, object],
        *,
        received_time_ns: int | None = None,
    ) -> TimestampedSample | None:
        """Validate one decoded bridge payload; exposed for deterministic tests."""

        received_ns = self._clock_ns() if received_time_ns is None else received_time_ns
        try:
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("schema-version-mismatch")
            if str(payload.get("side", "")).lower() != self.hand_side:
                raise ValueError("side-mismatch")
            if payload.get("layout") != LAYOUT_ID:
                raise ValueError("layout-mismatch")
            sequence = payload.get("source_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("invalid-source-sequence")
            source_time_ns = payload.get("sample_monotonic_ns")
            if (
                isinstance(source_time_ns, bool)
                or not isinstance(source_time_ns, int)
                or source_time_ns < 0
            ):
                raise ValueError("invalid-source-time")
            nodes = payload.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(PARENT_IDS):
                raise ValueError("node-count-mismatch")
            by_id: dict[int, tuple[float, float, float]] = {}
            for node in nodes:
                if not isinstance(node, Mapping):
                    raise ValueError("invalid-node-record")
                node_id = node.get("id")
                parent = node.get("parent")
                if isinstance(node_id, bool) or not isinstance(node_id, int):
                    raise ValueError("invalid-node-id")
                if node_id < 0 or node_id >= len(PARENT_IDS) or node_id in by_id:
                    raise ValueError("invalid-node-id")
                if parent != PARENT_IDS[node_id]:
                    raise ValueError("parent-layout-mismatch")
                by_id[node_id] = (
                    self._finite_coordinate(node.get("x"), f"node-{node_id}-x"),
                    self._finite_coordinate(node.get("y"), f"node-{node_id}-y"),
                    self._finite_coordinate(node.get("z"), f"node-{node_id}-z"),
                )
            if len(by_id) != len(PARENT_IDS):
                raise ValueError("node-id-set-mismatch")
            valid_mask = payload.get("valid_mask")
            if (
                not isinstance(valid_mask, list)
                or len(valid_mask) != len(PARENT_IDS)
                or any(not isinstance(value, bool) for value in valid_mask)
                or not all(valid_mask)
            ):
                raise ValueError("invalid-node-mask")
            with self._lock:
                previous_sequence = self._last_source_sequence
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError("non-monotonic-source-sequence")
        except ValueError as exc:
            self._reject(str(exc))
            return None

        points = tuple(by_id[index] for index in range(len(PARENT_IDS)))
        sample = TimestampedSample(
            payload=ManusKeypoints(
                source_id=self.source_id,
                glove_id=str(payload.get("glove_id", "unknown")),
                hand_side=self.hand_side,
                points_m=points,
                layout_id=LAYOUT_ID,
            ),
            generated_time_ns=source_time_ns,
            received_time_ns=received_ns,
            sequence=sequence,
            source_health=SourceHealth.HEALTHY,
            validity_mask=tuple(valid_mask),
            coordinate_frame_id="manus-wrist-local-native",
            units="meter",
            diagnostics=(
                ("bridge_mode", str(payload.get("mode", "unknown"))),
                ("bridge_source", str(payload.get("source", "unknown"))),
            ),
        )
        with self._lock:
            if self._last_receive_ns is not None and received_ns > self._last_receive_ns:
                self._intervals_ns.append(received_ns - self._last_receive_ns)
            if self._last_source_sequence is not None:
                self._dropped_frames += max(0, sequence - self._last_source_sequence - 1)
            self._latest = sample
            self._latest_mode = str(payload.get("mode", "unknown"))
            self._last_receive_ns = received_ns
            self._last_source_sequence = sequence
            self._accepted_frames += 1
            self._last_error = ""
            callback = self._callback
        if callback is not None:
            try:
                callback(sample)
            except BaseException as exc:
                self._reject(f"callback-fault:{type(exc).__name__}:{exc}")
                return None
        return sample

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            if address[0] not in ("127.0.0.1", "::1"):
                self._reject("non-loopback-sender")
                continue
            if len(data) > 32768:
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
            self.ingest_payload(decoded)

    def status(self, now_ns: int | None = None) -> ManusSourceStatus:
        now = self._clock_ns() if now_ns is None else now_ns
        with self._lock:
            receive_ns = self._last_receive_ns
            error = self._last_error
            sequence = self._accepted_frames
        if receive_ns is None:
            health = SourceHealth.DISCONNECTED
            reason = error
        elif now - receive_ns > self.stale_after_ns:
            health = SourceHealth.STALE
            reason = "source-stale"
        elif error:
            health = SourceHealth.DEGRADED
            reason = error
        else:
            health = SourceHealth.HEALTHY
            reason = ""
        return ManusSourceStatus(self.source_id, health, sequence, receive_ns, reason)

    def snapshot(self) -> dict[str, object]:
        now_ns = self._clock_ns()
        with self._lock:
            latest = self._latest
            mode = self._latest_mode
            receive_ns = self._last_receive_ns
            intervals = tuple(self._intervals_ns)
            dropped = self._dropped_frames
            rejected = self._rejected_frames
        status = self.status(now_ns)
        if latest is None or receive_ns is None:
            return {
                "connected": False,
                "mode": mode,
                "control_correlated": False,
                "nodes": [],
                "source_sequence": None,
                "sample_monotonic_ns": 0,
                "received_monotonic_ns": 0,
                "rate_hz": 0.0,
                "dropped_since_last": dropped,
                "stale_after_ns": self.stale_after_ns,
                "rejected_frames": rejected,
                "source_health": status.health.value,
                "source_reason": status.reason,
            }
        keypoints = latest.payload
        nodes = [
            {
                "id": index,
                "parent": PARENT_IDS[index],
                "x": -point[0],
                "y": point[1],
                "z": point[2],
            }
            for index, point in enumerate(keypoints.points_m)
        ]
        rate_hz = (
            1_000_000_000 / (sum(intervals) / len(intervals)) if intervals else 0.0
        )
        return {
            "connected": status.health is SourceHealth.HEALTHY,
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "control_correlated": False,
            "glove_id": keypoints.glove_id,
            "side": keypoints.hand_side,
            "layout": keypoints.layout_id,
            "source_sequence": latest.sequence,
            "sample_monotonic_ns": latest.received_time_ns,
            "received_monotonic_ns": receive_ns,
            "rate_hz": round(rate_hz, 3),
            "dropped_since_last": dropped,
            "stale_after_ns": self.stale_after_ns,
            "rejected_frames": rejected,
            "valid_mask": list(latest.validity_mask),
            "nodes": nodes,
            "source_health": status.health.value,
            "source_reason": status.reason,
            "coordinate_frame_id": latest.coordinate_frame_id,
            "units": latest.units,
        }
