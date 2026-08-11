from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from dex_runtime.telemetry import TelemetryHub
from test_control_console import _Controller, _OpenXR
from tools.control_console.arm_listener import ArmTelemetryListener
from tools.control_console.telemetry import ConsoleTelemetryPump


def _payload(sequence: int = 0, *, success: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "dex-teleop-hitbot-control-cycle",
        "mode": "live",
        "connected": True,
        "cycle_success": success,
        "source_sequence": sequence,
        "sample_monotonic_ns": 900_000_000 + sequence,
        "tracker_pose": [0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0],
        "transformed_delta": [0.001, 0.002, 0.003, 0.0, 0.0, 0.0, 1.0],
        "tcp_actual": [420.0, 10.0, 300.0, 180.0, 0.0, -10.0],
        "tcp_target": [421.0, 12.0, 303.0, 180.0, 1.0, -9.0],
        "ik_target": [421.0, 12.0, 303.0, 180.0, 1.0, -9.0],
        "ik_result": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] if success else None,
        "ik_ok": success,
        "servo_result": "ok" if success else None,
        "servo_ok": success,
        "tcp_query_ms": 2.0,
        "ik_ms": 3.0,
        "servo_call_ms": 1.0 if success else 0.0,
        "cycle_latency_ms": 6.5,
        "servo_interval_ms": 20.0 if success else None,
        "failure_reason": None if success else "ik-failed:test",
        "consecutive_failures": 0 if success else 1,
        "position_units": "mm",
        "orientation_units": "deg",
    }


def _listener(clock: list[int]) -> ArmTelemetryListener:
    return ArmTelemetryListener(
        "127.0.0.1",
        0,
        stale_after_ns=500_000_000,
        clock_ns=lambda: clock[0],
    )


def _heartbeat(now_ns: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "dex-teleop-hitbot-controller-heartbeat",
        "mode": "live",
        "controller_monotonic_ns": now_ns,
        "sent_monotonic_ns": now_ns,
    }


def test_live_arm_listener_preserves_control_cycle_and_pump_health() -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        assert listener.ingest_payload(_payload(), received_time_ns=clock[0])
        snapshot = listener.snapshot()
        assert snapshot["source_health"] == "healthy"
        assert snapshot["tcp_actual"] == [420.0, 10.0, 300.0, 180.0, 0.0, -10.0]
        assert snapshot["tcp_target"] == [421.0, 12.0, 303.0, 180.0, 1.0, -9.0]
        assert snapshot["ik_result"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

        hub = TelemetryHub(clock_ns=lambda: clock[0])
        pump = ConsoleTelemetryPump(
            hub,
            controller=_Controller(),
            vr=_OpenXR(),
            arm=listener,
            clock_ns=lambda: clock[0],
        )
        pump.publish_once()
        hitbot = hub.snapshot()["sources"]["hitbot"]
        assert hitbot["health"] == "healthy"
        assert hitbot["payload"]["mode"] == "live"
    finally:
        listener.stop()


def test_arm_listener_exposes_cycle_failure_drop_and_staleness() -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        assert listener.ingest_payload(_payload(2), received_time_ns=clock[0])
        failed = _payload(5, success=False)
        clock[0] += 20_000_000
        assert listener.ingest_payload(failed, received_time_ns=clock[0])
        snapshot = listener.snapshot()
        assert snapshot["source_health"] == "degraded"
        assert snapshot["source_reason"] == "ik-failed:test"
        assert snapshot["dropped_since_last"] == 2

        clock[0] += 500_000_001
        stale = listener.snapshot()
        assert stale["source_health"] == "stale"
        assert stale["source_reason"] == "source-stale"
    finally:
        listener.stop()


def test_arm_heartbeat_keeps_stationary_controller_live_without_adding_trail() -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        assert listener.ingest_payload(_payload(), received_time_ns=clock[0])
        assert len(listener.snapshot()["trail_actual"]) == 1

        clock[0] += 600_000_000
        assert listener.ingest_heartbeat(_heartbeat(clock[0]), received_time_ns=clock[0])
        snapshot = listener.snapshot()

        assert snapshot["connected"] is True
        assert snapshot["source_health"] == "healthy"
        assert snapshot["source_sequence"] == 0
        assert snapshot["controller_alive"] is True
        assert snapshot["motion_sample_fresh"] is False
        assert snapshot["rate_hz"] == 0.0
        assert snapshot["cycle_received_monotonic_ns"] == 1_000_000_000
        assert snapshot["received_monotonic_ns"] == clock[0]
        assert len(snapshot["trail_actual"]) == 1
    finally:
        listener.stop()


def test_arm_listener_degrades_successful_but_slow_control_cycle() -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        slow = _payload()
        slow["servo_interval_ms"] = 601.0
        assert listener.ingest_payload(slow, received_time_ns=clock[0])
        snapshot = listener.snapshot()
        assert snapshot["cycle_success"] is True
        assert snapshot["source_health"] == "degraded"
        assert snapshot["source_reason"] == "servo-interval-high"

        slow_latency = _payload(1)
        slow_latency["cycle_latency_ms"] = 250.0
        assert listener.ingest_payload(slow_latency, received_time_ns=clock[0] + 1)
        snapshot = listener.snapshot()
        assert snapshot["source_health"] == "degraded"
        assert snapshot["source_reason"] == "cycle-latency-high"
    finally:
        listener.stop()


def test_arm_listener_exposes_verified_hold_and_fault_hold() -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        verified = _payload()
        verified.update(
            control_mode="hold",
            hold_state="VERIFIED",
            hold_verified=True,
            hold_position_error_mm=0.4,
            hold_orientation_error_deg=0.2,
        )
        assert listener.ingest_payload(verified, received_time_ns=clock[0])
        snapshot = listener.snapshot()
        assert snapshot["source_health"] == "healthy"
        assert snapshot["hold_verified"] is True
        assert snapshot["hold_position_error_mm"] == 0.4

        fault = _payload(1)
        fault.update(
            control_mode="hold",
            hold_state="FAULT_HOLD",
            hold_verified=False,
            hold_position_error_mm=0.4,
            hold_orientation_error_deg=0.2,
        )
        assert listener.ingest_payload(fault, received_time_ns=clock[0] + 1)
        snapshot = listener.snapshot()
        assert snapshot["source_health"] == "fault"
        assert snapshot["source_reason"] == "arm-hold-fault"
    finally:
        listener.stop()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=2),
        lambda payload: payload["tcp_actual"].pop(),
        lambda payload: payload["tcp_target"].__setitem__(0, float("nan")),
        lambda payload: payload.update(cycle_latency_ms=-1.0),
    ],
)
def test_arm_listener_rejects_malformed_cycles_without_replacing_latest(mutation) -> None:
    clock = [1_000_000_000]
    listener = _listener(clock)
    try:
        assert listener.ingest_payload(_payload(), received_time_ns=clock[0])
        malformed = deepcopy(_payload(1))
        mutation(malformed)
        assert not listener.ingest_payload(malformed, received_time_ns=clock[0] + 1)
        snapshot = listener.snapshot()
        assert snapshot["source_sequence"] == 0
        assert snapshot["rejected_frames"] == 1
    finally:
        listener.stop()


def test_console_arm_listener_contains_no_hitbot_transport_or_command_code() -> None:
    source = Path("tools/control_console/arm_listener.py").read_text()
    assert "HitbotSixAxiscall" not in source
    assert "HitBotInterface" not in source
    assert "net_control" not in source
    assert "ServoJ" not in source
