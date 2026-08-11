"""Validated latest-value OpenXR hand source for control and visualization."""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from dex_contracts import SourceHealth, TimestampedSample
from dex_teleop_adapters import (
    OPENXR_JOINT_NAMES,
    OPENXR_LAYOUT_ID,
    OPENXR_PARENT_IDS,
    OpenXRKeypoints,
    OpenXRSourceStatus,
    needed_openxr_joints_valid,
)

SCHEMA_VERSION = 1


class UdpOpenXRSource:
    """Strict loopback receiver for Quest/WiVRn ``XR_EXT_hand_tracking`` frames."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        source_id: str,
        hand_side: str,
        stale_after_ns: int,
        warmup_samples: int = 1,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the OpenXR bridge must bind to a loopback address")
        if not 0 <= port <= 65535:
            raise ValueError("the OpenXR bridge UDP port must be within 0..65535")
        if hand_side not in ("left", "right"):
            raise ValueError("OpenXR hand_side must be left or right")
        if not source_id or stale_after_ns <= 0 or warmup_samples <= 0:
            raise ValueError("source_id and a positive stale threshold are required")
        self.host = host
        self.port = port
        self.source_id = source_id
        self.hand_side = hand_side
        self.stale_after_ns = stale_after_ns
        self.warmup_samples = warmup_samples
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
        self._latest_metadata: dict[str, object] = {}
        self._last_receive_ns: int | None = None
        self._last_source_sequence: int | None = None
        self._accepted_frames = 0
        self._consecutive_valid = 0
        self._rejected_frames = 0
        self._dropped_frames = 0
        self._last_error = "not-started"
        self._intervals_ns: deque[int] = deque(maxlen=90)

    def start(self, callback: Callable[[TimestampedSample], None]) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("UDP OpenXR source is already running")
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
                raise TimeoutError("UDP OpenXR source thread did not stop")
        with self._lock:
            self._thread = None
            self._callback = None

    def _reject(self, reason: str) -> None:
        with self._lock:
            self._rejected_frames += 1
            self._last_error = reason

    @staticmethod
    def _number(value: object, label: str) -> float:
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
            if payload.get("layout") != OPENXR_LAYOUT_ID:
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
            if payload.get("session_running") is not True:
                raise ValueError("session-not-running")
            if payload.get("session_focused") is not True:
                raise ValueError("session-not-focused")
            joints = payload.get("joints")
            if not isinstance(joints, list) or len(joints) != len(OPENXR_JOINT_NAMES):
                raise ValueError("joint-count-mismatch")
            valid_mask = payload.get("valid_mask")
            if (
                not isinstance(valid_mask, list)
                or len(valid_mask) != len(OPENXR_JOINT_NAMES)
                or any(not isinstance(value, bool) for value in valid_mask)
            ):
                raise ValueError("invalid-joint-mask")
            if not needed_openxr_joints_valid(valid_mask):
                raise ValueError("required-joint-invalid")
            positions: list[tuple[float, float, float] | None] = [None] * len(joints)
            orientations: list[tuple[float, float, float, float] | None] = [None] * len(joints)
            radii: list[float | None] = [None] * len(joints)
            for joint in joints:
                if not isinstance(joint, Mapping):
                    raise ValueError("invalid-joint-record")
                joint_id = joint.get("id")
                if isinstance(joint_id, bool) or not isinstance(joint_id, int):
                    raise ValueError("invalid-joint-id")
                if joint_id < 0 or joint_id >= len(joints) or positions[joint_id] is not None:
                    raise ValueError("invalid-joint-id")
                if joint.get("name") != OPENXR_JOINT_NAMES[joint_id]:
                    raise ValueError("joint-name-mismatch")
                if joint.get("parent") != OPENXR_PARENT_IDS[joint_id]:
                    raise ValueError("parent-layout-mismatch")
                positions[joint_id] = (
                    self._number(joint.get("x"), f"joint-{joint_id}-x"),
                    self._number(joint.get("y"), f"joint-{joint_id}-y"),
                    self._number(joint.get("z"), f"joint-{joint_id}-z"),
                )
                orientations[joint_id] = (
                    self._number(joint.get("qx"), f"joint-{joint_id}-qx"),
                    self._number(joint.get("qy"), f"joint-{joint_id}-qy"),
                    self._number(joint.get("qz"), f"joint-{joint_id}-qz"),
                    self._number(joint.get("qw"), f"joint-{joint_id}-qw"),
                )
                radii[joint_id] = self._number(
                    joint.get("radius_m"), f"joint-{joint_id}-radius"
                )
            if any(value is None for value in positions + orientations + radii):
                raise ValueError("joint-id-set-mismatch")
            pinch = payload.get("pinch_m")
            pinch_m = None if pinch is None else self._number(pinch, "pinch_m")
            if pinch_m is not None and pinch_m < 0:
                raise ValueError("pinch_m must be non-negative")
            with self._lock:
                previous_sequence = self._last_source_sequence
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError("non-monotonic-source-sequence")
        except ValueError as exc:
            self._reject(str(exc))
            return None

        points = tuple(value for value in positions if value is not None)
        quaternions = tuple(value for value in orientations if value is not None)
        radius_values = tuple(value for value in radii if value is not None)
        device = str(payload.get("device") or "Quest 3S")
        runtime = str(payload.get("runtime") or "WiVRn")
        sample = TimestampedSample(
            payload=OpenXRKeypoints(
                source_id=self.source_id,
                hand_side=self.hand_side,
                points_m=points,
                orientations_xyzw=quaternions,
                radii_m=radius_values,
                device=device,
                runtime=runtime,
                pinch_m=pinch_m,
            ),
            generated_time_ns=source_time_ns,
            received_time_ns=received_ns,
            sequence=sequence,
            source_health=SourceHealth.HEALTHY,
            validity_mask=tuple(valid_mask),
            coordinate_frame_id="openxr-view-right-handed",
            units="meter",
            diagnostics=(
                ("bridge_mode", str(payload.get("mode", "unknown"))),
                ("device", device),
                ("runtime", runtime),
                ("session_focused", True),
            ),
        )
        with self._lock:
            if (
                self._last_receive_ns is None
                or received_ns - self._last_receive_ns > self.stale_after_ns
            ):
                self._consecutive_valid = 0
            if self._last_receive_ns is not None and received_ns > self._last_receive_ns:
                self._intervals_ns.append(received_ns - self._last_receive_ns)
            if self._last_source_sequence is not None:
                self._dropped_frames += max(0, sequence - self._last_source_sequence - 1)
            self._latest = sample
            self._latest_metadata = {
                "mode": str(payload.get("mode", "unknown")),
                "device": device,
                "runtime": runtime,
                "session_running": True,
                "session_focused": True,
                "pinch_m": pinch_m,
            }
            self._last_receive_ns = received_ns
            self._last_source_sequence = sequence
            self._accepted_frames += 1
            self._consecutive_valid += 1
            warmed_up = self._consecutive_valid >= self.warmup_samples
            self._last_error = "" if warmed_up else "warming-up"
            callback = self._callback if warmed_up else None
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
            if len(data) > 48_000:
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

    def status(self, now_ns: int | None = None) -> OpenXRSourceStatus:
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
        return OpenXRSourceStatus(self.source_id, health, sequence, receive_ns, reason)

    def snapshot(self) -> dict[str, object]:
        now_ns = self._clock_ns()
        with self._lock:
            latest = self._latest
            metadata = dict(self._latest_metadata)
            receive_ns = self._last_receive_ns
            intervals = tuple(self._intervals_ns)
            dropped = self._dropped_frames
            rejected = self._rejected_frames
        status = self.status(now_ns)
        if latest is None or receive_ns is None:
            return {
                "connected": False,
                "mode": metadata.get("mode", "off"),
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
                "name": OPENXR_JOINT_NAMES[index],
                "parent": OPENXR_PARENT_IDS[index],
                "x": point[0],
                "y": point[1],
                "z": point[2],
                "valid": bool(latest.validity_mask[index]),
            }
            for index, point in enumerate(keypoints.points_m)
        ]
        rate_hz = (
            1_000_000_000 / (sum(intervals) / len(intervals)) if intervals else 0.0
        )
        wrist = keypoints.points_m[1]
        wrist_orientation = keypoints.orientations_xyzw[1]
        return {
            "connected": status.health is SourceHealth.HEALTHY,
            "schema_version": SCHEMA_VERSION,
            "mode": metadata.get("mode", "real"),
            "control_correlated": False,
            "device": keypoints.device,
            "runtime": keypoints.runtime,
            "session_running": metadata.get("session_running", True),
            "session_focused": metadata.get("session_focused", True),
            "side": keypoints.hand_side,
            "layout": keypoints.layout_id,
            "joint_count": len(keypoints.points_m),
            "valid_joint_count": sum(latest.validity_mask),
            "source_sequence": latest.sequence,
            "sample_monotonic_ns": latest.received_time_ns,
            "received_monotonic_ns": receive_ns,
            "rate_hz": round(rate_hz, 3),
            "dropped_since_last": dropped,
            "stale_after_ns": self.stale_after_ns,
            "rejected_frames": rejected,
            "valid_mask": list(latest.validity_mask),
            "nodes": nodes,
            "wrist_position_m": list(wrist),
            "wrist_orientation_xyzw": list(wrist_orientation),
            "pinch_m": keypoints.pinch_m,
            "source_health": status.health.value,
            "source_reason": status.reason,
            "coordinate_frame_id": latest.coordinate_frame_id,
            "units": latest.units,
        }
