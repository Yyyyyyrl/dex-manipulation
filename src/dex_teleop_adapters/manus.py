"""Timestamped Manus ROS 2 source with no retargeter or actuator access."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

import numpy as np

from dex_contracts import SourceHealth, TimestampedSample

from .manus_math import keypoints_valid, msg_to_keypoints, validate_layout


@dataclass(frozen=True)
class ManusKeypoints:
    source_id: str
    glove_id: str
    hand_side: str
    points_m: tuple[tuple[float, float, float], ...]
    layout_id: str = "manus-raw-25-v1"


@dataclass(frozen=True)
class ManusSourceStatus:
    source_id: str
    health: SourceHealth
    sequence: int
    last_receive_time_ns: int | None
    reason: str


class ManusHandSource:
    """Owns ROS subscription, layout/side validation, timing, and health only."""

    def __init__(
        self,
        *,
        source_id: str,
        hand_side: str,
        topic: str,
        stale_after_ns: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if hand_side not in ("left", "right"):
            raise ValueError("Manus hand_side must be left or right")
        if not source_id or not topic or stale_after_ns <= 0:
            raise ValueError("source ID, topic, and positive staleness limit are required")
        self.source_id = source_id
        self.hand_side = hand_side
        self.topic = topic
        self.stale_after_ns = stale_after_ns
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._sequence = 0
        self._layout_checked = False
        self._last_receive_ns: int | None = None
        self._last_error = "not-started"
        self._callback: Callable[[TimestampedSample], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @staticmethod
    def _source_time_ns(msg: Any) -> int | None:
        stamp = getattr(getattr(msg, "header", None), "stamp", None)
        if stamp is None:
            return None
        sec = getattr(stamp, "sec", None)
        nanosec = getattr(stamp, "nanosec", None)
        if isinstance(sec, int) and isinstance(nanosec, int) and sec >= 0 and nanosec >= 0:
            return sec * 1_000_000_000 + nanosec
        return None

    def ingest_message(self, msg: Any) -> TimestampedSample | None:
        """Validate and normalize one ROS-like message; exposed for deterministic tests."""

        received = self._clock_ns()
        side = str(getattr(msg, "side", "")).lower()
        if side != self.hand_side:
            with self._lock:
                self._last_error = f"side-mismatch:{side or 'missing'}"
            return None
        try:
            if not self._layout_checked:
                validate_layout(msg)
            keypoints = msg_to_keypoints(msg)
            if not keypoints_valid(keypoints):
                raise ValueError("Manus keypoint frame contains missing or non-finite mapped nodes")
        except Exception as exc:
            with self._lock:
                self._last_error = f"invalid-frame:{type(exc).__name__}:{exc}"
            return None

        points = tuple(tuple(float(axis) for axis in row) for row in keypoints)
        valid = tuple(bool(value) for value in np.isfinite(keypoints).all(axis=1))
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
            self._layout_checked = True
            self._last_receive_ns = received
            self._last_error = ""
        payload = ManusKeypoints(
            source_id=self.source_id,
            glove_id=str(getattr(msg, "glove_id", "unknown")),
            hand_side=self.hand_side,
            points_m=points,
        )
        sample = TimestampedSample(
            payload=payload,
            generated_time_ns=self._source_time_ns(msg),
            received_time_ns=received,
            sequence=sequence,
            source_health=SourceHealth.HEALTHY,
            validity_mask=valid,
            coordinate_frame_id="manus-wrist-local-native",
            units="meter",
            diagnostics=(("glove_id", payload.glove_id), ("hand_side", payload.hand_side)),
        )
        callback = self._callback
        if callback is not None:
            callback(sample)
        return sample

    def status(self, now_ns: int | None = None) -> ManusSourceStatus:
        now = self._clock_ns() if now_ns is None else now_ns
        with self._lock:
            if self._last_receive_ns is None:
                health = SourceHealth.DISCONNECTED
                reason = self._last_error
            elif now - self._last_receive_ns > self.stale_after_ns:
                health = SourceHealth.STALE
                reason = "source-stale"
            elif self._last_error:
                health = SourceHealth.DEGRADED
                reason = self._last_error
            else:
                health = SourceHealth.HEALTHY
                reason = ""
            return ManusSourceStatus(
                self.source_id,
                health,
                self._sequence,
                self._last_receive_ns,
                reason,
            )

    def start(self, callback: Callable[[TimestampedSample], None]) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Manus source is already running")
            self._callback = callback
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._ros_worker,
                name=f"{self.source_id}-ros2",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError("Manus source thread did not stop")
        with self._lock:
            self._thread = None
            self._callback = None

    def _ros_worker(self) -> None:
        try:
            import rclpy
            from manus_ros2_msgs.msg import ManusGlove
        except ImportError as exc:
            with self._lock:
                self._last_error = f"ros-capability-missing:{exc}"
            return
        node = None
        try:
            rclpy.init(args=None)
            node = rclpy.create_node(f"{self.source_id}_source")
            node.create_subscription(ManusGlove, self.topic, self.ingest_message, 1)
            while not self._stop.is_set() and rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.1)
        except BaseException as exc:
            with self._lock:
                self._last_error = f"ros-worker-fault:{type(exc).__name__}:{exc}"
        finally:
            if node is not None:
                node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
