from __future__ import annotations

import json
import threading
import time

from dex_contracts import (
    MessageIdentity,
    PROTOCOL_VERSION,
    ResourceId,
    SourceHealth,
    TeleopHandCandidate,
    TimestampedSample,
)
from dex_hardware_linker import FakeLinkerTransport, GatewayConfig, LinkerGateway
from dex_runtime.application import HandOnlyRuntime
from dex_runtime.handoff import HandoffState
from dex_runtime.operator_switch import OperatorSwitchEvent, SwitchEdge
from dex_runtime.preflight import preflight_deployment
from dex_teleop_adapters import ManusSourceStatus
from policy_package_factory import CALIBRATION_LOWER, CALIBRATION_UPPER, write_test_package
from test_deployment_preflight import _write_config


class _FakeManusSource:
    def __init__(self) -> None:
        now_ns = time.monotonic_ns()
        self.sample = TimestampedSample(
            payload="test-manus-frame",
            generated_time_ns=None,
            received_time_ns=now_ns,
            sequence=0,
            source_health=SourceHealth.HEALTHY,
            validity_mask=(True,),
            coordinate_frame_id="test-frame",
            units="meter",
        )

    def start(self, callback) -> None:
        callback(self.sample)

    def status(self, _now_ns: int) -> ManusSourceStatus:
        return ManusSourceStatus(
            "test-manus",
            SourceHealth.HEALTHY,
            1,
            self.sample.received_time_ns,
            "",
        )

    def stop(self) -> None:
        pass


class _FakeRetargeter:
    def __init__(
        self,
        target: tuple[float, ...],
        hand_model: str,
        hand_side: str,
        semantic_schema_id: str,
    ) -> None:
        self.target = target
        self.hand_model = hand_model
        self.hand_side = hand_side
        self.semantic_schema_id = semantic_schema_id
        self.sequence = 0

    def reset(self) -> None:
        self.sequence = 0

    def retarget(
        self,
        sample: TimestampedSample,
        *,
        control_session_id: str,
        control_epoch: int,
        task_id: str | None,
        task_version: str | None,
    ) -> TeleopHandCandidate:
        candidate = TeleopHandCandidate(
            identity=MessageIdentity(
                protocol_version=PROTOCOL_VERSION,
                control_session_id=control_session_id,
                source_id="test-retargeter",
                resource_id=ResourceId.HAND,
                hand_model=self.hand_model,
                hand_side=self.hand_side,
                semantic_schema_id=self.semantic_schema_id,
                task_id=task_id,
                task_version=task_version,
                policy_package_id=None,
                calibration_id=None,
                control_epoch=control_epoch,
                sequence=self.sequence,
            ),
            semantic_position=self.target,
            generated_time_ns=sample.received_time_ns,
            valid_until_ns=sample.received_time_ns + 10_000_000_000,
            source_state_sequence=sample.sequence,
        )
        self.sequence += 1
        return candidate


class _FakeSwitch:
    status = "healthy-test"

    def __init__(self) -> None:
        self.callback = None
        self.sequence = 0

    def start(self, callback) -> None:
        self.callback = callback

    def tap(self) -> None:
        if self.callback is None:
            raise RuntimeError("virtual switch is not started")
        now_ns = time.monotonic_ns()
        for edge in (SwitchEdge.PRESS, SwitchEdge.RELEASE):
            self.callback(
                OperatorSwitchEvent(
                    source_id="virtual-f12",
                    key="F12",
                    edge=edge,
                    generated_time_ns=now_ns,
                    received_time_ns=now_ns,
                    sequence=self.sequence,
                )
            )
            self.sequence += 1

    def stop(self) -> None:
        pass


def _wait_until(predicate, *, timeout_s: float, description: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {description}")


def test_hand_only_application_runs_real_gateway_and_records_trace(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    preflight = preflight_deployment(str(_write_config(tmp_path, package)))
    binding = preflight.binding
    midpoint = tuple(
        (lower + upper) * 0.5
        for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER)
    )
    transport = FakeLinkerTransport(preflight.mapper.prepare(midpoint).native_range)
    gateway = LinkerGateway(
        GatewayConfig(
            binding.gateway.gateway_id,
            binding.control_session_id,
            binding.gateway.gateway_hz,
            binding.gateway.state_stale_ns,
            binding.gateway.command_watchdog_ns,
            binding.gateway.maximum_round_trip_error_rad,
        ),
        preflight.mapper,
        transport,
    )
    application = HandOnlyRuntime(
        preflight,
        gateway,
        _FakeManusSource(),
        _FakeRetargeter(
            midpoint,
            preflight.mapper.calibration.hand_model,
            preflight.mapper.calibration.hand_side,
            preflight.mapper.calibration.semantic_schema_id,
        ),
        _FakeSwitch(),
    )

    result = application.run(max_ticks=2, initial_input_timeout_s=1.0)

    assert result.exit_code == 0
    assert result.ticks == 2
    assert result.final_state == "TELEOP_ACTIVE"
    assert len(transport.sent_commands) == 2
    events = [json.loads(line) for line in binding.logging.events_path.read_text().splitlines()]
    traces = [json.loads(line) for line in binding.logging.trace_path.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["runtime-start", "runtime-stop"]
    assert len(traces) == 2
    assert all(trace["payload"]["switch_status"] == "healthy-test" for trace in traces)
    payload = traces[-1]["payload"]
    assert payload["authorized_command"]["safety_decision"] == "pass"
    assert payload["gateway_acknowledgement"]["gateway"]["level"] == 4
    assert payload["effective_target"]["evidence_level"] == 4
    assert payload["arbitration_result"]["state"] == "TELEOP_ACTIVE"
    assert payload["readiness"]["task_id"] == "mounted-screwdriver-rotation"
    assert payload["mapping_preview"]["saturated_joints"] == []
    assert payload["scheduler"]["control_period_ns"] == 100_000_000

    frame = application.latest_control_telemetry
    assert frame is not None
    assert frame.tick == 1
    assert frame.manus_sample.sequence == frame.teleop_candidate.source_state_sequence
    assert frame.requested_candidate is frame.teleop_candidate
    assert frame.authorized_command is not None
    assert frame.gateway_acknowledgement is not None
    assert (
        frame.authorized_command.command_id
        == frame.gateway_acknowledgement.gateway.command_id
        == frame.gateway_acknowledgement.effective_target.command_id
        == frame.effective_target.command_id
    )
    assert (
        frame.hand_state.identity.sequence
        == payload["hand_state"]["identity"]["sequence"]
    )

    from tools.switch_web_demo import DemoController

    controller = DemoController.__new__(DemoController)
    controller.runtime = application
    controller.gateway = gateway
    linker = controller.linker_snapshot()
    assert linker["runtime_tick"] == frame.tick
    assert linker["control_sample_sequence"] == frame.manus_sample.sequence
    assert linker["candidate_source_sequence"] == frame.teleop_candidate.source_state_sequence
    assert linker["command_identity_match"] is True
    assert linker["authorized_command_id"] == linker["acknowledged_command_id"]
    assert linker["authorized_command_id"] == linker["effective_command_id"]
    assert len(linker["joints"]) == 16
    assert linker["joints"][0]["requested_target"] == frame.requested_candidate.semantic_position[0]
    assert linker["joints"][0]["authorized_target"] == frame.authorized_command.semantic_position[0]
    assert linker["joints"][0]["effective_target"] == frame.effective_target.semantic_position[0]
    assert linker["joints"][0]["measured"] == frame.hand_state.semantic_position[0]
    assert len(linker["native_mapping"]["native_arc"]) == 20


def test_virtual_manus_and_f12_complete_policy_cycle_through_application(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config_path = _write_config(tmp_path, package)
    config = json.loads(config_path.read_text())
    config["status"]["period_ns"] = 100_000_000_000
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    preflight = preflight_deployment(str(config_path))
    binding = preflight.binding
    midpoint = tuple(
        (lower + upper) * 0.5
        for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER)
    )
    transport = FakeLinkerTransport(preflight.mapper.prepare(midpoint).native_range)
    gateway = LinkerGateway(
        GatewayConfig(
            binding.gateway.gateway_id,
            binding.control_session_id,
            binding.gateway.gateway_hz,
            binding.gateway.state_stale_ns,
            binding.gateway.command_watchdog_ns,
            binding.gateway.maximum_round_trip_error_rad,
        ),
        preflight.mapper,
        transport,
    )
    virtual_switch = _FakeSwitch()
    application = HandOnlyRuntime(
        preflight,
        gateway,
        _FakeManusSource(),
        _FakeRetargeter(
            midpoint,
            preflight.mapper.calibration.hand_model,
            preflight.mapper.calibration.hand_side,
            preflight.mapper.calibration.semantic_schema_id,
        ),
        virtual_switch,
    )
    outcome = {}

    def run_application() -> None:
        try:
            outcome["result"] = application.run(
                max_ticks=120,
                initial_input_timeout_s=1.0,
            )
        except BaseException as exc:  # surfaced in the test thread below
            outcome["exception"] = exc

    thread = threading.Thread(target=run_application, name="virtual-control-cycle")
    thread.start()
    try:
        assert application.wait_until_connected(1.0)
        application.confirm_operator("virtual-integration-test")
        _wait_until(
            lambda: (
                application.supervisor is not None
                and application.supervisor.state is HandoffState.RL_SHADOW
                and application.supervisor.policy_session is not None
                and application.supervisor.policy_session.status.history_ready
            ),
            timeout_s=5.0,
            description="30-tick RL shadow history",
        )

        virtual_switch.tap()
        _wait_until(
            lambda: (
                application.supervisor is not None
                and application.supervisor.state is HandoffState.RL_ACTIVE
            ),
            timeout_s=3.0,
            description="RL_ACTIVE",
        )
        active_epoch = application.supervisor.hand_epoch

        virtual_switch.tap()
        _wait_until(
            lambda: (
                application.supervisor is not None
                and application.supervisor.state is HandoffState.RL_SHADOW
                and application.supervisor.hand_epoch > active_epoch
                and application.supervisor.arm_gateway.status.anchor_generation == 1
            ),
            timeout_s=3.0,
            description="teleoperation hand-back and shadow re-prime",
        )
    finally:
        application.request_stop()
        thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert "exception" not in outcome, repr(outcome.get("exception"))
    assert outcome["result"].exit_code == 0
    assert application.supervisor is not None
    assert application.supervisor.state is HandoffState.RL_SHADOW

    events = [json.loads(line) for line in binding.logging.events_path.read_text().splitlines()]
    switch_events = [event for event in events if event["event_type"] == "operator-switch"]
    assert [event["requested_transition"] for event in switch_events] == [
        "activation-requested",
        "handback-requested",
    ]
    transition_states = [
        event["state"] for event in events if event["event_type"] == "transition"
    ]
    for state in (
        "ARM_HOLD_PREPARE",
        "ARM_HOLD_VERIFY",
        "HAND_BLEND",
        "RL_ACTIVE",
        "HAND_BACK_PREPARE",
        "HAND_BACK_BLEND",
        "ARM_TELEOP_REANCHOR",
        "RL_SHADOW",
    ):
        assert state in transition_states

    traces = [json.loads(line) for line in binding.logging.trace_path.read_text().splitlines()]
    assert any(trace["hand_owner"] == "selected-policy" for trace in traces)
    assert traces[-1]["hand_owner"] == "teleoperation"
    assert max(trace["control_epoch"] for trace in traces) >= 5
