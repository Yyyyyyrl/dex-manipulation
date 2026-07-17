from __future__ import annotations

import io
import json

from dex_runtime.clock import FakeClock
from dex_runtime.observability import ControlTraceRecorder, EventLogger, RuntimeEvent
from dex_runtime.operator_switch import (
    EvdevF12SwitchSource,
    F12Debouncer,
    SwitchEdge,
    is_toggle_request,
)
from dex_runtime.status import RuntimeStatus, TerminalStatusRenderer


def test_jsonl_events_and_bounded_trace_are_canonical_and_complete(tmp_path) -> None:
    event_path = tmp_path / "events.jsonl"
    trace_path = tmp_path / "trace.jsonl"
    events = EventLogger(event_path)
    events.emit(
        RuntimeEvent(
            monotonic_time_ns=100,
            wall_time_utc="2026-07-16T12:00:00Z",
            control_session_id="session",
            event_type="transition",
            state="HAND_BLEND",
            requested_transition="RL_ACTIVE",
            hand_owner="transition-controller",
            arm_owner="arm-hold",
            control_epoch=4,
            policy_package_id="sha256:package",
            readiness={"ready": True},
            reason_code=None,
            deadline_ns=200,
            gateway_acknowledgement={"level": "sent-to-bus"},
            safe_response=None,
            operator_action="F12-press",
        )
    )
    events.close()
    event_line = event_path.read_text().strip()
    assert event_line == json.dumps(json.loads(event_line), sort_keys=True, separators=(",", ":"))
    event = json.loads(event_line)
    assert event["record_type"] == "event"
    assert event["gateway_acknowledgement"]["level"] == "sent-to-bus"

    trace = ControlTraceRecorder(trace_path, minimum_period_ns=50)
    assert trace.record(
        monotonic_time_ns=100,
        control_session_id="session",
        state="RL_ACTIVE",
        hand_owner="selected-policy",
        arm_owner="arm-hold",
        control_epoch=5,
        policy_package_id="sha256:package",
        payload={"codec_input": [0.0] * 32, "blend_alpha": 1.0},
    )
    assert not trace.record(
        monotonic_time_ns=120,
        control_session_id="session",
        state="RL_ACTIVE",
        hand_owner="selected-policy",
        arm_owner="arm-hold",
        control_epoch=5,
        policy_package_id="sha256:package",
        payload={},
    )
    assert trace.record(
        monotonic_time_ns=150,
        control_session_id="session",
        state="RL_ACTIVE",
        hand_owner="selected-policy",
        arm_owner="arm-hold",
        control_epoch=5,
        policy_package_id="sha256:package",
        payload={},
    )
    trace.close()
    assert trace.recorded_count == 2 and trace.rate_limited_count == 1
    assert len(trace_path.read_text().splitlines()) == 2


def test_f12_switch_is_debounced_and_only_press_toggles() -> None:
    clock = FakeClock(1_000)
    debouncer = F12Debouncer(
        source_id="footswitch",
        debounce_ns=50,
        clock_ns=clock.now_ns,
    )
    press = debouncer.ingest(SwitchEdge.PRESS)
    assert press is not None and is_toggle_request(press)
    assert press.key == "F12"
    assert debouncer.ingest(SwitchEdge.PRESS) is None
    clock.advance_ns(20)
    assert debouncer.ingest(SwitchEdge.RELEASE) is None
    clock.advance_ns(30)
    release = debouncer.ingest(SwitchEdge.RELEASE)
    assert release is not None and not is_toggle_request(release)

    source = EvdevF12SwitchSource(
        device_path="/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd",
        source_id="footswitch",
        debounce_ns=50,
        clock_ns=clock.now_ns,
    )
    assert source.ingest_key_value("KEY_F13", 1) is None
    f12 = source.ingest_key_value("KEY_F12", 1)
    assert f12 is not None and f12.key == "F12"


def test_terminal_status_exposes_operational_gates() -> None:
    stream = io.StringIO()
    renderer = TerminalStatusRenderer(stream, use_ansi=False)
    renderer.render(
        RuntimeStatus(
            state="RL_SHADOW",
            hand_owner="teleoperation",
            arm_owner="teleoperation",
            control_epoch=3,
            hand_health="fresh",
            manus_health="healthy",
            gateway_health="healthy",
            policy_name="mounted-v1",
            policy_compatible=True,
            history_count=24,
            history_required=30,
            blend_alpha=None,
            readiness_ready=False,
            rejection_reason="operator-confirmation-missing",
            recording=True,
        )
    )
    line = stream.getvalue()
    assert "state=RL_SHADOW" in line
    assert "history=24/30" in line
    assert "ready=NO" in line
    assert "operator-confirmation-missing" in line
