from __future__ import annotations

import json
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

    def start(self, callback) -> None:
        self.callback = callback

    def stop(self) -> None:
        pass


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
