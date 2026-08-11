from __future__ import annotations

import json
import socket

import numpy as np
import pytest

from tools.vr_hitbot_controller import (
    HitbotOwner,
    OPENXR_LAYOUT,
    OpenXRWristReceiver,
    SERVO_FALLBACK_S,
)


class _Telemetry:
    def __init__(self) -> None:
        self.payload = None

    def publish(self, payload) -> None:
        self.payload = payload


class _Hitbot:
    def __init__(self) -> None:
        self.ik_target = None
        self.servo = None
        self.tcp_pose_calls = 0
        self.tcp_pose = np.asarray([100.0, 200.0, 300.0, 10.0, 20.0, 30.0])

    def get_tcp_pose(self):
        self.tcp_pose_calls += 1
        return np.asarray(self.tcp_pose, dtype=float)

    def get_ik(self, target):
        self.ik_target = np.asarray(target)
        return np.asarray([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def servo_j(self, joints, *, t):
        self.servo = (np.asarray(joints), t)
        return True


def _owner(hitbot: _Hitbot, telemetry: _Telemetry, **overrides) -> HitbotOwner:
    """Build an owner without importing the dex_teleop SDK."""

    controller = type("ArmController", (), {"hitbot_interface": hitbot})()
    owner = HitbotOwner.__new__(HitbotOwner)
    owner.arm_controller = controller
    owner.hitbot = hitbot
    owner.telemetry = telemetry
    owner.sequence = 0
    owner.last_servo_t = None
    owner.consecutive_failures = 0
    owner.tcp_resync_s = overrides.get("tcp_resync_s", 0.5)
    owner.tcp_drift_limit_mm = overrides.get("tcp_drift_limit_mm", 50.0)
    owner.commanded_tcp = None
    owner.measured_tcp = None
    owner.measured_tcp_t = None
    owner.quaternion_from_euler = lambda *_: np.asarray([0.0, 0.0, 0.0, 1.0])
    owner.quaternion_multiply = lambda left, right: np.asarray([0.0, 0.0, 0.0, 1.0])
    owner.euler_from_quaternion = lambda value: np.zeros(3)
    return owner


def test_hitbot_delta_uses_current_tcp_world_left_multiply_and_measured_servo_dt() -> None:
    hitbot = _Hitbot()
    telemetry = _Telemetry()
    owner = _owner(hitbot, telemetry)
    owner.quaternion_from_euler = lambda *_: np.asarray([0.1, 0.2, 0.3, 0.9])
    calls = []

    def multiply(left, right):
        calls.append((np.asarray(left), np.asarray(right)))
        return np.asarray([0.4, 0.5, 0.6, 0.7])

    owner.quaternion_multiply = multiply
    owner.euler_from_quaternion = lambda value: np.asarray([0.01, 0.02, 0.03])

    delta_pos = np.asarray([0.01, -0.02, 0.03])
    delta_ori = np.asarray([0.0, 0.0, 0.2, 0.98])
    owner.move_delta(delta_pos, delta_ori, np.zeros(7))

    assert np.allclose(hitbot.ik_target[:3], [107.0, 186.0, 321.0])
    assert np.allclose(hitbot.ik_target[3:], np.rad2deg([0.01, 0.02, 0.03]))
    assert np.allclose(calls[0][0], delta_ori)
    assert np.allclose(calls[0][1], [0.1, 0.2, 0.3, 0.9])
    assert hitbot.servo[1] == pytest.approx(SERVO_FALLBACK_S)
    assert telemetry.payload["cycle_success"] is True
    assert telemetry.payload["input_source"] == "openxr-left-wrist"


def _wrist_frame(sequence: int, x: float) -> bytes:
    wrist = {"x": x, "y": 0.0, "z": 0.0, "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0}
    return json.dumps(
        {
            "schema_version": 1,
            "source": "dex-teleop-openxr-hand",
            "layout": OPENXR_LAYOUT,
            "side": "left",
            "session_running": True,
            "session_focused": True,
            "source_sequence": sequence,
            "sample_monotonic_ns": 1_000_000 * sequence,
            "valid_mask": [True] * 26,
            "joints": [wrist] * 26,
        }
    ).encode()


def test_wrist_receiver_drains_backlog_and_keeps_newest_frame() -> None:
    receiver = OpenXRWristReceiver("127.0.0.1", 0)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        destination = receiver._sock.getsockname()
        for sequence in (1, 2, 3):
            sender.sendto(_wrist_frame(sequence, sequence / 10.0), destination)

        # Draining an emptied socket must not escalate to socket.timeout.
        received = receiver.receive()

        assert received is not None
        tracker, sample_ns = received
        assert tracker[0] == pytest.approx(0.3)
        assert sample_ns == 3_000_000
        assert receiver.receive() is None
    finally:
        sender.close()
        receiver.close()


def test_hitbot_integrates_between_tcp_resyncs_instead_of_querying_every_cycle() -> None:
    # GetActualTCPPose costs ~0.63 s on the real controller, so the per-cycle
    # query is what capped teleop near 1.5 Hz against a 185 Hz wrist stream.
    hitbot = _Hitbot()
    telemetry = _Telemetry()
    owner = _owner(hitbot, telemetry, tcp_resync_s=10.0)
    delta = np.asarray([0.01, 0.0, 0.0])
    identity = np.asarray([0.0, 0.0, 0.0, 1.0])

    owner.move_delta(delta, identity, np.zeros(7))
    assert hitbot.tcp_pose_calls == 1
    assert telemetry.payload["tcp_pose_measured"] is True
    # 100 mm base + 0.01 m * 1000 * 0.7 scale
    assert hitbot.ik_target[0] == pytest.approx(107.0)

    # Subsequent cycles integrate the commanded pose: no new SDK query, and the
    # target keeps advancing instead of restarting from the same reading.
    owner.move_delta(delta, identity, np.zeros(7))
    assert hitbot.tcp_pose_calls == 1
    assert hitbot.ik_target[0] == pytest.approx(114.0)
    assert telemetry.payload["tcp_pose_measured"] is False
    assert telemetry.payload["tcp_pose_age_ms"] >= 0.0
    # The published pose stays the last real measurement, never the integrated one.
    assert telemetry.payload["tcp_actual"][0] == pytest.approx(100.0)
    assert telemetry.payload["connected"] is True


def test_hitbot_resync_reanchors_on_the_measured_pose_and_reports_drift() -> None:
    hitbot = _Hitbot()
    telemetry = _Telemetry()
    owner = _owner(hitbot, telemetry, tcp_resync_s=0.0)
    delta = np.asarray([0.01, 0.0, 0.0])
    identity = np.asarray([0.0, 0.0, 0.0, 1.0])

    owner.move_delta(delta, identity, np.zeros(7))
    # The arm reports it did not reach the commanded 107 mm; the next cycle must
    # build on the measurement, not on the commanded target.
    hitbot.tcp_pose = np.asarray([104.0, 200.0, 300.0, 10.0, 20.0, 30.0])
    owner.move_delta(delta, identity, np.zeros(7))

    assert hitbot.tcp_pose_calls == 2
    assert hitbot.ik_target[0] == pytest.approx(111.0)
    assert telemetry.payload["tcp_drift_mm"] == pytest.approx(3.0)
    assert telemetry.payload["tcp_pose_measured"] is True


def test_hitbot_faults_when_tracking_drift_exceeds_the_limit() -> None:
    hitbot = _Hitbot()
    telemetry = _Telemetry()
    owner = _owner(hitbot, telemetry, tcp_resync_s=0.0, tcp_drift_limit_mm=5.0)
    delta = np.asarray([0.01, 0.0, 0.0])
    identity = np.asarray([0.0, 0.0, 0.0, 1.0])

    owner.move_delta(delta, identity, np.zeros(7))
    hitbot.tcp_pose = np.asarray([50.0, 200.0, 300.0, 10.0, 20.0, 30.0])
    with pytest.raises(RuntimeError, match="tcp-tracking-drift"):
        owner.move_delta(delta, identity, np.zeros(7))

    assert telemetry.payload["cycle_success"] is False
    assert "tcp-tracking-drift" in telemetry.payload["failure_reason"]
    # A failed cycle must not leave a stale integration base behind.
    assert owner.commanded_tcp is None


def test_hitbot_hold_refreshes_measurement_and_drops_the_integration_base() -> None:
    hitbot = _Hitbot()
    telemetry = _Telemetry()
    owner = _owner(hitbot, telemetry, tcp_resync_s=10.0)
    delta = np.asarray([0.01, 0.0, 0.0])
    identity = np.asarray([0.0, 0.0, 0.0, 1.0])
    owner.move_delta(delta, identity, np.zeros(7))
    assert owner.commanded_tcp is not None

    anchor_tcp = np.asarray([100.0, 200.0, 300.0, 10.0, 20.0, 30.0])
    owner.hold_position(
        anchor_tcp,
        np.asarray([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
        tracker_pose=None,
        hold_state="HOLDING",
    )

    assert owner.commanded_tcp is None
    assert telemetry.payload["control_mode"] == "hold"
    # Resuming teleop must re-query rather than integrate from the pre-hold pose.
    calls_before = hitbot.tcp_pose_calls
    owner.move_delta(delta, identity, np.zeros(7))
    assert hitbot.tcp_pose_calls == calls_before + 1
