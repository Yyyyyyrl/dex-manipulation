#!/usr/bin/env python3
"""Single Hitbot SDK owner driven by the shared OpenXR left-wrist stream."""

from __future__ import annotations

from collections import OrderedDict
import argparse
import json
import math
from pathlib import Path
import select
import socket
import sys
import threading
import time

import numpy as np


OPENXR_LAYOUT = "openxr-hand-26-v1"
TRACKER_STALE_NS = 250_000_000
WRIST_POLL_S = 0.01
SERVO_FALLBACK_S = 0.05
# GetActualTCPPose costs ~0.63 s on this controller (measured; network RTT to
# the arm is ~1 ms), so querying it every cycle caps teleop near 1.5 Hz against
# a 185 Hz wrist stream. Integrate the commanded pose between re-syncs instead.
TCP_RESYNC_S = 0.5
# Re-sync compares the commanded pose against the measured one. That difference
# is normal servo tracking lag, so the default only trips on gross divergence;
# tcp_drift_mm is published every re-sync so the real envelope can be observed
# before tightening this.
TCP_DRIFT_LIMIT_MM = 50.0
HOLD_REQUEST_FIELDS = {
    "schema_version", "kind", "command_id", "control_session_id",
    "control_epoch", "action", "sent_monotonic_ns",
    "deadline_monotonic_ns", "hold_lease_ns",
}
HOLD_STATES = {"PREPARED", "HOLDING", "VERIFIED", "REANCHOR_ACKED", "FAULT_HOLD"}


def _finite_vector(value, length: int, label: str) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.shape != (length,) or not np.isfinite(result).all():
        raise ValueError(f"{label} must contain {length} finite values")
    return result


class ArmTelemetryPublisher:
    """Latest-wins loopback publisher; JSON/network never block the SDK owner."""

    def __init__(self, host: str, port: int) -> None:
        self.destination = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._lock = threading.Lock()
        self._latest: dict[str, object] | None = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._replaced = 0
        self._send_errors = 0
        self._thread = threading.Thread(
            target=self._worker,
            name="vr-hitbot-telemetry",
            daemon=True,
        )
        self._thread.start()

    def publish(self, payload: dict[str, object]) -> None:
        with self._lock:
            if self._latest is not None:
                self._replaced += 1
            self._latest = payload
        self._event.set()

    def _take(self) -> dict[str, object] | None:
        with self._lock:
            payload = self._latest
            self._latest = None
            self._event.clear()
            return payload

    def _send(self, payload: dict[str, object]) -> None:
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > 16_384:
                raise ValueError("arm telemetry datagram too large")
            self._sock.sendto(encoded, self.destination)
        except (BlockingIOError, OSError, TypeError, ValueError):
            self._send_errors += 1

    def _worker(self) -> None:
        last_heartbeat_ns = 0
        while not self._stop.is_set():
            self._event.wait(0.2)
            payload = self._take()
            now_ns = time.monotonic_ns()
            if payload is not None:
                payload = dict(payload)
                payload["sent_monotonic_ns"] = now_ns
                payload["publisher_replaced"] = self._replaced
                payload["publisher_send_errors"] = self._send_errors
                self._send(payload)
            if now_ns - last_heartbeat_ns >= 200_000_000:
                self._send(
                    {
                        "schema_version": 1,
                        "source": "dex-teleop-hitbot-controller-heartbeat",
                        "mode": "live",
                        "controller_monotonic_ns": now_ns,
                        "sent_monotonic_ns": time.monotonic_ns(),
                    }
                )
                last_heartbeat_ns = now_ns

    def close(self) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(1.0)
        self._sock.close()


class HitbotOwner:
    """The only object allowed to call the Hitbot SDK in this process."""

    def __init__(
        self,
        arm_controller,
        telemetry: ArmTelemetryPublisher,
        *,
        tcp_resync_s: float = TCP_RESYNC_S,
        tcp_drift_limit_mm: float = TCP_DRIFT_LIMIT_MM,
    ) -> None:
        self.arm_controller = arm_controller
        self.hitbot = arm_controller.hitbot_interface
        self.telemetry = telemetry
        self.sequence = 0
        self.last_servo_t: float | None = None
        self.consecutive_failures = 0
        self.tcp_resync_s = tcp_resync_s
        self.tcp_drift_limit_mm = tcp_drift_limit_mm
        # commanded_tcp is the integrated pose teleop builds deltas on; it is
        # dropped whenever the arm may have moved outside teleop's command
        # stream (hold, fault) so the next cycle re-syncs from the robot.
        self.commanded_tcp: np.ndarray | None = None
        self.measured_tcp: np.ndarray | None = None
        self.measured_tcp_t: float | None = None

        from dex_teleop.utils.transformations import (
            euler_from_quaternion,
            quaternion_from_euler,
            quaternion_multiply,
        )
        self.euler_from_quaternion = euler_from_quaternion
        self.quaternion_from_euler = quaternion_from_euler
        self.quaternion_multiply = quaternion_multiply

    @staticmethod
    def _orientation_error_deg(actual: np.ndarray, target: np.ndarray) -> float:
        wrapped = (actual[3:] - target[3:] + 180.0) % 360.0 - 180.0
        return float(np.max(np.abs(wrapped)))

    def _emit(
        self,
        *,
        tracker_pose: np.ndarray | None,
        delta_pos: np.ndarray,
        delta_ori: np.ndarray,
        actual: np.ndarray | None,
        target: np.ndarray | None,
        ik_result: np.ndarray | None,
        servo_result,
        tcp_query_ns: int,
        ik_ns: int,
        servo_call_ns: int,
        cycle_start_ns: int,
        servo_interval_ns: int | None,
        success: bool,
        failure_reason: str | None,
        control_mode: str = "teleop",
        hold_state: str = "TELEOP",
        hold_position_error_mm: float | None = None,
        hold_orientation_error_deg: float | None = None,
        tcp_pose_measured: bool = True,
        tcp_drift_mm: float | None = None,
    ) -> None:
        payload = {
            "schema_version": 1,
            "source": "dex-teleop-hitbot-control-cycle",
            "input_source": "openxr-left-wrist",
            "mode": "live",
            "connected": actual is not None,
            "cycle_success": success,
            "source_sequence": self.sequence,
            "sample_monotonic_ns": time.monotonic_ns(),
            "tracker_pose": None if tracker_pose is None else tracker_pose.tolist(),
            "transformed_delta": delta_pos.tolist() + delta_ori.tolist(),
            "tcp_actual": None if actual is None else actual.tolist(),
            "tcp_target": None if target is None else target.tolist(),
            "ik_target": None if target is None else target.tolist(),
            "ik_result": None if ik_result is None else ik_result.tolist(),
            "ik_ok": ik_result is not None,
            "servo_result": (
                servo_result
                if servo_result is None or isinstance(servo_result, (str, int, float, bool))
                else repr(servo_result)
            ),
            "servo_ok": success,
            "tcp_query_ms": tcp_query_ns / 1_000_000,
            "ik_ms": ik_ns / 1_000_000,
            "servo_call_ms": servo_call_ns / 1_000_000,
            "cycle_latency_ms": max(0, time.perf_counter_ns() - cycle_start_ns) / 1_000_000,
            "servo_interval_ms": None if servo_interval_ns is None else servo_interval_ns / 1_000_000,
            "failure_reason": failure_reason,
            "consecutive_failures": self.consecutive_failures,
            "position_units": "mm",
            "orientation_units": "deg",
            "tracker_position_units": "m",
            "tracker_orientation_order": "xyzw",
            "control_mode": control_mode,
            "hold_state": hold_state,
            "hold_verified": hold_state in ("VERIFIED", "REANCHOR_ACKED"),
            "hold_position_error_mm": hold_position_error_mm,
            "hold_orientation_error_deg": hold_orientation_error_deg,
            # tcp_actual is the newest *measured* pose, which teleop only
            # refreshes every tcp_resync_s; publish its age so consumers never
            # mistake an integrated cycle for a fresh reading.
            "tcp_pose_measured": tcp_pose_measured,
            "tcp_pose_age_ms": (
                0.0
                if tcp_pose_measured or self.measured_tcp_t is None
                else max(0.0, time.perf_counter() - self.measured_tcp_t) * 1000.0
            ),
            "tcp_drift_mm": tcp_drift_mm,
        }
        self.sequence += 1
        self.telemetry.publish(payload)

    def move_delta(
        self,
        robot_delta_pos: np.ndarray,
        robot_delta_ori: np.ndarray,
        tracker_pose: np.ndarray,
    ) -> None:
        cycle_start_ns = time.perf_counter_ns()
        target = joints = None
        servo_result = None
        # Bound before the first SDK call so the failure path can always report
        # the newest measurement, even when this cycle's query is what raised.
        actual = self.measured_tcp
        tcp_ns = ik_ns = servo_ns = 0
        servo_interval_ns = None
        measured = False
        drift_mm = None
        try:
            now = time.perf_counter()
            if (
                self.commanded_tcp is None
                or self.measured_tcp_t is None
                or now - self.measured_tcp_t >= self.tcp_resync_s
            ):
                started = time.perf_counter_ns()
                self.measured_tcp = _finite_vector(self.hitbot.get_tcp_pose(), 6, "tcp pose")
                tcp_ns = time.perf_counter_ns() - started
                self.measured_tcp_t = time.perf_counter()
                measured = True
                if self.commanded_tcp is not None:
                    drift_mm = float(
                        np.linalg.norm(self.measured_tcp[:3] - self.commanded_tcp[:3])
                    )
                    if drift_mm > self.tcp_drift_limit_mm:
                        raise RuntimeError(
                            f"tcp-tracking-drift:{drift_mm:.1f}mm"
                            f">{self.tcp_drift_limit_mm:.1f}mm"
                        )
                # Re-anchor on the measurement so integration error can never
                # accumulate across more than one re-sync interval.
                base = actual = self.measured_tcp
            else:
                base = self.commanded_tcp
            current_rpy = np.deg2rad(base[3:])
            current_quaternion = self.quaternion_from_euler(*current_rpy)
            target_position = base[:3] + robot_delta_pos * 1000.0 * 0.7
            target_quaternion = self.quaternion_multiply(robot_delta_ori, current_quaternion)
            target_rpy = np.rad2deg(self.euler_from_quaternion(target_quaternion))
            target = np.concatenate((target_position, target_rpy))
            started = time.perf_counter_ns()
            raw_joints = self.hitbot.get_ik(target)
            ik_ns = time.perf_counter_ns() - started
            if isinstance(raw_joints, str):
                raise RuntimeError(f"ik-failed:{raw_joints}")
            joints = _finite_vector(raw_joints, 6, "IK result")
            now = time.perf_counter()
            if self.last_servo_t is None or now - self.last_servo_t > 1.0:
                servo_dt = SERVO_FALLBACK_S
            else:
                servo_dt = now - self.last_servo_t
            self.last_servo_t = now
            servo_interval_ns = int(servo_dt * 1_000_000_000)
            started = time.perf_counter_ns()
            servo_result = self.hitbot.servo_j(joints, t=servo_dt)
            servo_ns = time.perf_counter_ns() - started
            self.consecutive_failures = 0
            self.commanded_tcp = target
            self._emit(
                tracker_pose=tracker_pose,
                delta_pos=robot_delta_pos,
                delta_ori=robot_delta_ori,
                actual=actual,
                target=target,
                ik_result=joints,
                servo_result=servo_result,
                tcp_query_ns=tcp_ns,
                ik_ns=ik_ns,
                servo_call_ns=servo_ns,
                cycle_start_ns=cycle_start_ns,
                servo_interval_ns=servo_interval_ns,
                success=True,
                failure_reason=None,
                tcp_pose_measured=measured,
                tcp_drift_mm=drift_mm,
            )
        except BaseException as exc:
            self.consecutive_failures += 1
            # The commanded pose is only a valid integration base while cycles
            # succeed; force a re-sync rather than build on an unknown state.
            self.commanded_tcp = None
            self._emit(
                tracker_pose=tracker_pose,
                delta_pos=robot_delta_pos,
                delta_ori=robot_delta_ori,
                actual=actual,
                target=target,
                ik_result=joints,
                servo_result=servo_result,
                tcp_query_ns=tcp_ns,
                ik_ns=ik_ns,
                servo_call_ns=servo_ns,
                cycle_start_ns=cycle_start_ns,
                servo_interval_ns=servo_interval_ns,
                success=False,
                failure_reason=f"{type(exc).__name__}:{exc}",
                tcp_pose_measured=measured,
                tcp_drift_mm=drift_mm,
            )
            raise

    def hold_position(
        self,
        anchor_tcp: np.ndarray,
        anchor_joints: np.ndarray,
        *,
        tracker_pose: np.ndarray | None,
        hold_state: str,
    ) -> tuple[np.ndarray, float, float]:
        cycle_start_ns = time.perf_counter_ns()
        actual = None
        servo_result = None
        tcp_ns = servo_ns = 0
        try:
            started = time.perf_counter_ns()
            servo_result = self.hitbot.servo_j(anchor_joints, t=SERVO_FALLBACK_S)
            servo_ns = time.perf_counter_ns() - started
            started = time.perf_counter_ns()
            actual = _finite_vector(self.hitbot.get_tcp_pose(), 6, "hold TCP")
            tcp_ns = time.perf_counter_ns() - started
            # Hold verification must stay closed-loop on a real reading; it also
            # refreshes teleop's measurement and drops the integration base so
            # resuming teleop re-syncs instead of building on a pre-hold pose.
            self.measured_tcp = actual
            self.measured_tcp_t = time.perf_counter()
            self.commanded_tcp = None
            position_error = float(np.linalg.norm(actual[:3] - anchor_tcp[:3]))
            orientation_error = self._orientation_error_deg(actual, anchor_tcp)
            self.consecutive_failures = 0
            self._emit(
                tracker_pose=tracker_pose,
                delta_pos=np.zeros(3),
                delta_ori=np.array([0.0, 0.0, 0.0, 1.0]),
                actual=actual,
                target=anchor_tcp,
                ik_result=anchor_joints,
                servo_result=servo_result,
                tcp_query_ns=tcp_ns,
                ik_ns=0,
                servo_call_ns=servo_ns,
                cycle_start_ns=cycle_start_ns,
                servo_interval_ns=int(SERVO_FALLBACK_S * 1_000_000_000),
                success=True,
                failure_reason=None,
                control_mode="hold",
                hold_state=hold_state,
                hold_position_error_mm=position_error,
                hold_orientation_error_deg=orientation_error,
            )
            return actual, position_error, orientation_error
        except BaseException as exc:
            self.consecutive_failures += 1
            self._emit(
                tracker_pose=tracker_pose,
                delta_pos=np.zeros(3),
                delta_ori=np.array([0.0, 0.0, 0.0, 1.0]),
                actual=actual,
                target=anchor_tcp,
                ik_result=anchor_joints,
                servo_result=servo_result,
                tcp_query_ns=tcp_ns,
                ik_ns=0,
                servo_call_ns=servo_ns,
                cycle_start_ns=cycle_start_ns,
                servo_interval_ns=None,
                success=False,
                failure_reason=f"hold:{type(exc).__name__}:{exc}",
                control_mode="hold",
                hold_state="FAULT_HOLD",
            )
            raise


class ArmHoldController:
    def __init__(self, owner: HitbotOwner) -> None:
        self.owner = owner
        self.state = "TELEOP"
        self.control_session_id: str | None = None
        self.control_epoch = 0
        self.anchor_tcp: np.ndarray | None = None
        self.anchor_joints: np.ndarray | None = None
        self.anchor_generation = 0
        self.fault_reason: str | None = None
        self.position_error_mm: float | None = None
        self.orientation_error_deg: float | None = None
        self._stable_samples = 0
        self._lease_deadline_ns = 0
        self._latest_tracker: np.ndarray | None = None
        self._latest_tracker_ns = 0
        self._resume_anchor: np.ndarray | None = None
        self._last_hold_command_ns = 0
        self._responses: OrderedDict[tuple[str, int, str], dict[str, object]] = OrderedDict()

    @property
    def hold_required(self) -> bool:
        return self.state in HOLD_STATES

    def update_tracker(self, tracker: np.ndarray, sample_ns: int) -> None:
        self._latest_tracker = _finite_vector(tracker, 7, "tracker pose")
        self._latest_tracker_ns = sample_ns

    def take_resume_anchor(self) -> np.ndarray | None:
        if self.state != "TELEOP":
            return None
        anchor = self._resume_anchor
        self._resume_anchor = None
        return None if anchor is None else anchor.copy()

    def _response(self, request: dict[str, object], ok: bool, reason: str = "") -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "response",
            "command_id": request["command_id"],
            "control_session_id": request["control_session_id"],
            "control_epoch": request["control_epoch"],
            "ok": ok,
            "reason": reason,
            "state": self.state,
            "prepared": self.state in HOLD_STATES,
            "active": self.state in {"HOLDING", "VERIFIED", "REANCHOR_ACKED", "FAULT_HOLD"},
            "verified": self.state in {"VERIFIED", "REANCHOR_ACKED"},
            "anchor_generation": self.anchor_generation,
            "fault_reason": self.fault_reason,
            "position_error_mm": self.position_error_mm,
            "orientation_error_deg": self.orientation_error_deg,
            "server_monotonic_ns": time.monotonic_ns(),
        }

    @staticmethod
    def _validate(request: object) -> dict[str, object]:
        if not isinstance(request, dict) or set(request) != HOLD_REQUEST_FIELDS:
            raise ValueError("invalid-request-fields")
        if request.get("schema_version") != 1 or request.get("kind") != "request":
            raise ValueError("protocol-mismatch")
        for name in ("command_id", "control_session_id", "action"):
            if not isinstance(request.get(name), str) or not request[name]:
                raise ValueError(f"invalid-{name}")
        for name in ("control_epoch", "sent_monotonic_ns", "deadline_monotonic_ns", "hold_lease_ns"):
            value = request.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid-{name}")
        if request["action"] not in {"status", "prepare", "enter", "heartbeat", "reanchor", "release"}:
            raise ValueError("invalid-action")
        now_ns = time.monotonic_ns()
        if request["deadline_monotonic_ns"] < now_ns:
            raise ValueError("command-expired")
        if request["deadline_monotonic_ns"] > now_ns + 2_000_000_000:
            raise ValueError("deadline-too-far")
        if not 200_000_000 <= request["hold_lease_ns"] <= 2_000_000_000:
            raise ValueError("invalid-hold-lease")
        return request

    def _identity_matches(self, request: dict[str, object]) -> bool:
        return request["control_session_id"] == self.control_session_id and request["control_epoch"] == self.control_epoch

    def handle(self, raw_request: object) -> dict[str, object]:
        try:
            request = self._validate(raw_request)
        except ValueError as exc:
            raw = raw_request if isinstance(raw_request, dict) else {}
            epoch = raw.get("control_epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int):
                epoch = 0
            fallback = {
                "command_id": str(raw.get("command_id") or "invalid"),
                "control_session_id": str(raw.get("control_session_id") or "invalid"),
                "control_epoch": epoch,
            }
            return self._response(fallback, False, str(exc))
        key = (str(request["control_session_id"]), int(request["control_epoch"]), str(request["command_id"]))
        if key in self._responses:
            return dict(self._responses[key])
        action = str(request["action"])
        now_ns = time.monotonic_ns()
        try:
            if action == "status":
                matched = self.state == "TELEOP" or self._identity_matches(request)
                response = self._response(request, matched, "" if matched else "owned-by-another-session")
            elif action == "prepare":
                if self.state != "TELEOP":
                    response = self._response(request, False, "arm-not-in-teleop")
                else:
                    self.control_session_id = str(request["control_session_id"])
                    self.control_epoch = int(request["control_epoch"])
                    self._lease_deadline_ns = now_ns + int(request["hold_lease_ns"])
                    self._stable_samples = 0
                    self.fault_reason = None
                    self.state = "PREPARED"
                    self.anchor_tcp = _finite_vector(self.owner.hitbot.get_tcp_pose(), 6, "hold TCP")
                    raw_joints = self.owner.hitbot.get_ik(self.anchor_tcp)
                    if isinstance(raw_joints, str):
                        raise RuntimeError(f"hold-anchor-ik-failed:{raw_joints}")
                    self.anchor_joints = _finite_vector(raw_joints, 6, "hold joints")
                    response = self._response(request, True)
            elif not self._identity_matches(request):
                response = self._response(request, False, "control-identity-mismatch")
            elif action == "enter":
                if self.state != "PREPARED":
                    response = self._response(request, False, "hold-not-prepared")
                else:
                    self.state = "HOLDING"
                    self._lease_deadline_ns = now_ns + int(request["hold_lease_ns"])
                    self.tick(force=True)
                    response = self._response(request, True)
            elif action == "heartbeat":
                if self.state not in {"HOLDING", "VERIFIED", "REANCHOR_ACKED"}:
                    response = self._response(request, False, "hold-not-active")
                else:
                    self._lease_deadline_ns = now_ns + int(request["hold_lease_ns"])
                    response = self._response(request, True)
            elif action == "reanchor":
                if self.state != "VERIFIED":
                    response = self._response(request, False, "hold-not-verified")
                elif self._latest_tracker is None or now_ns - self._latest_tracker_ns > TRACKER_STALE_NS:
                    response = self._response(request, False, "tracker-anchor-stale")
                else:
                    self._resume_anchor = self._latest_tracker.copy()
                    self.anchor_generation += 1
                    self.state = "REANCHOR_ACKED"
                    self._lease_deadline_ns = now_ns + int(request["hold_lease_ns"])
                    response = self._response(request, True)
            elif action == "release":
                if self.state != "REANCHOR_ACKED" or self._resume_anchor is None:
                    response = self._response(request, False, "teleop-not-reanchored")
                elif self._latest_tracker is None or now_ns - self._latest_tracker_ns > TRACKER_STALE_NS:
                    response = self._response(request, False, "release-tracker-stale")
                else:
                    self._resume_anchor = self._latest_tracker.copy()
                    self.state = "TELEOP"
                    self.anchor_tcp = self.anchor_joints = None
                    self._stable_samples = 0
                    self._lease_deadline_ns = 0
                    self.fault_reason = None
                    response = self._response(request, True)
            else:
                response = self._response(request, False, "invalid-action")
        except BaseException as exc:
            if self.hold_required:
                self.state = "FAULT_HOLD"
                self.fault_reason = f"{type(exc).__name__}:{exc}"
            response = self._response(request, False, f"{type(exc).__name__}:{exc}")
        self._responses[key] = dict(response)
        while len(self._responses) > 128:
            self._responses.popitem(last=False)
        return response

    def tick(self, force: bool = False) -> None:
        if not self.hold_required or self.anchor_tcp is None or self.anchor_joints is None:
            return
        now_ns = time.monotonic_ns()
        if self._lease_deadline_ns and now_ns > self._lease_deadline_ns:
            self.state = "FAULT_HOLD"
            self.fault_reason = "hold-heartbeat-expired"
        if not force and now_ns - self._last_hold_command_ns < 40_000_000:
            return
        self._last_hold_command_ns = now_ns
        try:
            _, position_error, orientation_error = self.owner.hold_position(
                self.anchor_tcp,
                self.anchor_joints,
                tracker_pose=self._latest_tracker,
                hold_state=self.state,
            )
            self.position_error_mm = position_error
            self.orientation_error_deg = orientation_error
            stable = position_error <= 1.5 and orientation_error <= 1.5
            self._stable_samples = self._stable_samples + 1 if stable else 0
            if self.state == "HOLDING" and self._stable_samples >= 5:
                self.state = "VERIFIED"
        except BaseException as exc:
            self.state = "FAULT_HOLD"
            self.fault_reason = f"hold-command-failed:{type(exc).__name__}:{exc}"


class HoldDatagramServer:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._sock.setblocking(False)

    def poll(self, controller: ArmHoldController) -> None:
        for _ in range(8):
            try:
                data, address = self._sock.recvfrom(8193)
            except BlockingIOError:
                break
            if address[0] != "127.0.0.1" or len(data) > 8192:
                continue
            try:
                request = json.loads(data.decode())
                response = controller.handle(request)
                encoded = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
                self._sock.sendto(encoded, address)
            except (UnicodeDecodeError, ValueError, TypeError, OSError):
                continue

    def close(self) -> None:
        self._sock.close()


class OpenXRWristReceiver:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        # A socket timeout would make every recvfrom() wait for readiness first,
        # so draining an empty socket raises socket.timeout instead of
        # BlockingIOError. Stay non-blocking and pace the loop with select().
        self._sock.setblocking(False)
        self.last_sequence: int | None = None

    def receive(self) -> tuple[np.ndarray, int] | None:
        readable, _, _ = select.select([self._sock], [], [], WRIST_POLL_S)
        if not readable:
            return None
        try:
            data, address = self._sock.recvfrom(48_001)
        except BlockingIOError:
            return None
        # The Hitbot SDK cycle can be slower than OpenXR. Drain the socket and
        # keep only the newest frame so delayed arm calls never build a motion
        # backlog that is replayed later.
        while True:
            try:
                next_data, next_address = self._sock.recvfrom(48_001)
            except BlockingIOError:
                break
            data, address = next_data, next_address
        if address[0] != "127.0.0.1" or len(data) > 48_000:
            return None
        try:
            payload = json.loads(data.decode())
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or payload.get("source") != "dex-teleop-openxr-hand"
                or payload.get("layout") != OPENXR_LAYOUT
                or payload.get("side") != "left"
                or payload.get("session_running") is not True
                or payload.get("session_focused") is not True
            ):
                return None
            sequence = payload.get("source_sequence")
            sample_ns = payload.get("sample_monotonic_ns")
            valid = payload.get("valid_mask")
            joints = payload.get("joints")
            if (
                isinstance(sequence, bool) or not isinstance(sequence, int)
                or isinstance(sample_ns, bool) or not isinstance(sample_ns, int)
                or not isinstance(valid, list) or len(valid) != 26 or valid[1] is not True
                or not isinstance(joints, list) or len(joints) != 26
                or self.last_sequence is not None and sequence <= self.last_sequence
            ):
                return None
            wrist = joints[1]
            tracker = _finite_vector(
                [wrist[key] for key in ("x", "y", "z", "qx", "qy", "qz", "qw")],
                7,
                "OpenXR wrist",
            )
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            return None
        self.last_sequence = sequence
        return tracker, sample_ns

    def close(self) -> None:
        self._sock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--vr-port", type=int, default=8771)
    parser.add_argument("--telemetry-port", type=int, default=8780)
    parser.add_argument("--hold-port", type=int, default=8781)
    parser.add_argument("--teleop-root", default="/home/user/dex_teleop")
    parser.add_argument("--tcp-resync-s", type=float, default=TCP_RESYNC_S)
    parser.add_argument("--tcp-drift-limit-mm", type=float, default=TCP_DRIFT_LIMIT_MM)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("--host must be loopback")
    if not 0.0 < args.tcp_resync_s <= 2.0:
        parser.error("--tcp-resync-s must be within (0, 2]")
    if args.tcp_drift_limit_mm <= 0:
        parser.error("--tcp-drift-limit-mm must be positive")
    if any(not 1 <= port <= 65535 for port in (args.vr_port, args.telemetry_port, args.hold_port)):
        parser.error("UDP ports must be within 1..65535")
    if not Path(args.teleop_root, "main_new.py").is_file():
        parser.error(f"dex_teleop main_new.py is missing under {args.teleop_root}")
    return args


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(Path(args.teleop_root).resolve().parent))
    from dex_teleop.arm_controller import ArmController

    telemetry = ArmTelemetryPublisher(args.host, args.telemetry_port)
    wrist_receiver = OpenXRWristReceiver(args.host, args.vr_port)
    hold_server = HoldDatagramServer(args.host, args.hold_port)
    try:
        controller = ArmController()
        owner = HitbotOwner(
            controller,
            telemetry,
            tcp_resync_s=args.tcp_resync_s,
            tcp_drift_limit_mm=args.tcp_drift_limit_mm,
        )
        hold = ArmHoldController(owner)
        last_tracker = None
        last_sample_ns = None
        print("[arm] OpenXR/Hitbot owner ready", flush=True)
        while True:
            hold_server.poll(hold)
            received = wrist_receiver.receive()
            if received is None:
                if last_sample_ns is not None and time.monotonic_ns() - last_sample_ns > TRACKER_STALE_NS:
                    last_tracker = None
                    last_sample_ns = None
                hold.tick()
                continue
            tracker, sample_ns = received
            if last_sample_ns is not None and (sample_ns <= last_sample_ns or sample_ns - last_sample_ns > TRACKER_STALE_NS):
                print("[arm] OpenXR discontinuity; re-anchoring without motion", flush=True)
                last_tracker = None
            hold.update_tracker(tracker, sample_ns)
            hold.tick()
            resume = hold.take_resume_anchor()
            if resume is not None:
                last_tracker = resume
                last_sample_ns = sample_ns
            if hold.hold_required:
                last_sample_ns = sample_ns
                continue
            if last_tracker is not None:
                delta_pos, delta_ori = controller.calculate_wrist_movement_delta(
                    last_tracker[:3], last_tracker[3:], tracker[:3], tracker[3:]
                )
                robot_pos, robot_ori = controller.delVR_2_delRob(delta_pos, delta_ori)
                owner.move_delta(robot_pos, robot_ori, tracker)
            last_tracker = tracker
            last_sample_ns = sample_ns
    except KeyboardInterrupt:
        print("\n[arm] controller stopped", flush=True)
    finally:
        hold_server.close()
        wrist_receiver.close()
        telemetry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
