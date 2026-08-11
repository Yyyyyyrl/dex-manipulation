"""Single-process hand-only runtime composition for the adopted M0-M3 path."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from dex_contracts import (
    AcknowledgementLevel,
    EffectiveHandTarget,
    HandState,
    PolicyCompatibility,
    ReadinessPolicy,
    ReadinessRequirement,
    RequirementLevel,
    to_primitive,
)
from dex_teleop_adapters import ManusHandSource, ManusRetargeter

from .deployment import DeploymentBinding
from .fake_arm import FakeArmGateway
from .handoff import HandoffConfig, HandoffState, HandoffSupervisor
from .latest import LatestValueBuffer
from .observability import ControlTraceRecorder, EventLogger, RuntimeEvent
from .operator_switch import EvdevF12SwitchSource, OperatorSwitchEvent, is_toggle_request
from .policy_session import PolicySession
from .preflight import PreflightResult
from .readiness import (
    GatewayHealthProvider,
    HandStateFreshnessProvider,
    OperatorConfirmationProvider,
    PolicyCompatibilityProvider,
    ReadinessAggregator,
)
from .real_arm import RealArmGateway
from .safety import HandGatewayBinding, HandSafetyLimits, HandSafetySupervisor
from .status import RuntimeStatus, TerminalStatusRenderer
from .telemetry import ControlLoopTelemetry


class RuntimeGateway(Protocol):
    fault_reason: str | None
    latest_state: HandState | None

    def start(self, timeout_s: float = 5.0) -> None: ...
    def stop(self, timeout_s: float = 5.0) -> None: ...
    def prepare_ownership(self, ownership): ...
    def commit_ownership(self, preparation) -> None: ...
    def submit(self, command): ...


@dataclass(frozen=True)
class ApplicationResult:
    exit_code: int
    ticks: int
    final_state: str
    last_rejection: str | None


def _wall_time_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HandOnlyRuntime:
    def __init__(
        self,
        preflight: PreflightResult,
        gateway: RuntimeGateway,
        manus_source: ManusHandSource,
        retargeter: ManusRetargeter,
        switch_source: EvdevF12SwitchSource,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        status_renderer: TerminalStatusRenderer | None = None,
        arm_gateway=None,
    ) -> None:
        self.preflight = preflight
        self.binding: DeploymentBinding = preflight.binding
        self.gateway = gateway
        self.manus_source = manus_source
        self.retargeter = retargeter
        self.switch_source = switch_source
        self.monotonic_ns = monotonic_ns
        self.status_renderer = status_renderer or TerminalStatusRenderer(
            use_ansi=self.binding.status.use_ansi
        )
        if arm_gateway is not None:
            self.arm_gateway = arm_gateway
        elif self.binding.arm.mode == "fake-hold":
            self.arm_gateway = FakeArmGateway()
        else:
            arm = self.binding.arm
            assert (
                arm.control_host is not None
                and arm.control_port is not None
                and arm.request_timeout_s is not None
                and arm.command_ttl_ns is not None
                and arm.hold_lease_ns is not None
            )
            self.arm_gateway = RealArmGateway(
                self.binding.control_session_id,
                arm.control_host,
                arm.control_port,
                request_timeout_s=arm.request_timeout_s,
                command_ttl_ns=arm.command_ttl_ns,
                hold_lease_ns=arm.hold_lease_ns,
                clock_ns=monotonic_ns,
            )
        self.event_logger = EventLogger(self.binding.logging.events_path)
        self.trace_recorder = ControlTraceRecorder(
            self.binding.logging.trace_path,
            minimum_period_ns=self.binding.logging.trace_minimum_period_ns,
        )
        self.operator_confirmation = OperatorConfirmationProvider()
        self._manus_buffer: LatestValueBuffer[Any] = LatestValueBuffer()
        self._switch_events: queue.Queue[OperatorSwitchEvent] = queue.Queue(maxsize=32)
        self._switch_overflow = False
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._started = False
        self._closed = False
        self._supervisor: HandoffSupervisor | None = None
        self._policy_session: PolicySession | None = None
        self._compatibility = PolicyCompatibility(True, ())
        self._operator_id: str | None = None
        self._ticks = 0
        self._telemetry_lock = threading.Lock()
        self._latest_control_telemetry: ControlLoopTelemetry | None = None

    @property
    def supervisor(self) -> HandoffSupervisor | None:
        return self._supervisor

    @property
    def latest_control_telemetry(self) -> ControlLoopTelemetry | None:
        with self._telemetry_lock:
            return self._latest_control_telemetry

    def confirm_operator(self, operator_id: str) -> None:
        if not operator_id:
            raise ValueError("operator ID is required")
        now_ns = self.monotonic_ns()
        self._operator_id = operator_id
        self.operator_confirmation.confirm(
            task_id=self.preflight.policy_package.descriptor.task_id,
            hand_model=self.binding.hand.model,
            hand_side=self.binding.hand.side,
            operator_id=operator_id,
            control_session_id=self.binding.control_session_id,
            policy_package_id=self.preflight.policy_package.descriptor.package_id,
            displayed_evidence_digest=self.preflight.policy_package.descriptor.package_digest,
            now_ns=now_ns,
            validity_ns=self.binding.readiness.operator_confirmation_validity_ns,
        )
        self._emit_event(
            "operator-confirmation",
            state=self._supervisor.state.value if self._supervisor else "DISCONNECTED",
            operator_action=f"confirm:{operator_id}",
        )

    def request_stop(self) -> None:
        self._stop.set()

    def wait_until_connected(self, timeout_s: float | None = None) -> bool:
        return self._connected.wait(timeout_s)

    def _on_switch(self, event: OperatorSwitchEvent) -> None:
        try:
            self._switch_events.put_nowait(event)
        except queue.Full:
            self._switch_overflow = True

    def _emit_event(
        self,
        event_type: str,
        *,
        state: str,
        requested_transition: str | None = None,
        reason_code: str | None = None,
        operator_action: str | None = None,
        readiness: dict | None = None,
        deadline_ns: int | None = None,
        gateway_acknowledgement: dict | None = None,
        safe_response: str | None = None,
    ) -> None:
        supervisor = self._supervisor
        package_id = self.preflight.policy_package.descriptor.package_id
        hand_owner, arm_owner = self._owners(
            HandoffState(state)
            if state in HandoffState._value2member_map_
            else HandoffState.DISCONNECTED
        )
        self.event_logger.emit(
            RuntimeEvent(
                monotonic_time_ns=self.monotonic_ns(),
                wall_time_utc=_wall_time_utc(),
                control_session_id=self.binding.control_session_id,
                event_type=event_type,
                state=state,
                requested_transition=requested_transition,
                hand_owner=hand_owner,
                arm_owner=arm_owner,
                control_epoch=0 if supervisor is None else supervisor.hand_epoch,
                policy_package_id=package_id,
                readiness=readiness,
                reason_code=reason_code,
                deadline_ns=deadline_ns,
                gateway_acknowledgement=gateway_acknowledgement,
                safe_response=safe_response,
                operator_action=operator_action,
            )
        )

    @staticmethod
    def _owners(state: HandoffState) -> tuple[str, str]:
        if state in (HandoffState.DISCONNECTED,):
            return "none", "none"
        if state in (HandoffState.SAFE_HOLD, HandoffState.ESTOP):
            return "safety", "safety"
        if state in (HandoffState.HAND_BLEND, HandoffState.HAND_BACK_BLEND):
            return "transition-controller", "arm-hold"
        if state in (HandoffState.RL_ACTIVE, HandoffState.HAND_BACK_PREPARE):
            return "selected-policy", "arm-hold"
        if state in (HandoffState.ARM_TELEOP_REANCHOR,):
            return "teleoperation", "arm-hold"
        if state in (HandoffState.ARM_HOLD_PREPARE, HandoffState.ARM_HOLD_VERIFY):
            return "teleoperation", "arm-hold"
        return "teleoperation", "teleoperation"

    def _build_supervisor(self, initial_state: HandState) -> HandoffSupervisor:
        mapper = self.preflight.mapper
        safety_cfg = self.binding.safety
        safety = HandSafetySupervisor(
            HandSafetyLimits(
                safety_cfg.position_lower_rad,
                safety_cfg.position_upper_rad,
                safety_cfg.maximum_delta_per_tick_rad,
                safety_cfg.maximum_target_rate_rad_s,
                safety_cfg.maximum_following_error_rad,
                safety_cfg.maximum_state_age_ns,
                safety_cfg.command_deadline_ns,
            ),
            HandGatewayBinding(
                self.binding.control_session_id,
                "command-arbiter",
                self.binding.hand.model,
                self.binding.hand.side,
                mapper.calibration.semantic_schema_id,
                self.preflight.policy_package.descriptor.task_id,
                self.preflight.policy_package.descriptor.task_version,
                self.preflight.policy_package.descriptor.package_id,
                mapper.calibration.calibration_id,
                mapper.calibration.artifact_digest,
            ),
        )
        handoff_cfg = self.binding.handoff
        supervisor = HandoffSupervisor(
            HandoffConfig(
                self.binding.control_session_id,
                handoff_cfg.ownership_lease_ns,
                handoff_cfg.gateway_ack_timeout_s,
                handoff_cfg.teleop_command_period_ns,
                handoff_cfg.policy_blend_ticks,
                handoff_cfg.handback_blend_ticks,
                self.binding.readiness.required_provider_ids,
            ),
            self.gateway,
            self.arm_gateway,
            safety,
        )
        initial_effective = EffectiveHandTarget(
            semantic_position=initial_state.semantic_position,
            command_id="startup-measured-anchor",
            evidence_level=AcknowledgementLevel.NONE,
            evidence_time_ns=initial_state.acquisition_time_ns,
        )
        supervisor.start(initial_effective, now_ns=self.monotonic_ns())
        return supervisor

    def _readiness(self, hand_state: HandState, now_ns: int):
        package = self.preflight.policy_package
        validity = self.binding.readiness.evidence_validity_ns
        evidence = []
        if self.operator_confirmation.current is not None:
            evidence.append(self.operator_confirmation.current)
        evidence.append(
            HandStateFreshnessProvider().evaluate(
                hand_state,
                task_id=package.descriptor.task_id,
                now_ns=now_ns,
                maximum_age_ns=self.binding.safety.maximum_state_age_ns,
                validity_ns=validity,
            )
        )
        evidence.append(
            GatewayHealthProvider().evaluate(
                task_id=package.descriptor.task_id,
                hand_model=self.binding.hand.model,
                hand_side=self.binding.hand.side,
                healthy=self.gateway.fault_reason is None,
                watchdog_healthy=self.gateway.fault_reason is None,
                fault_reason=self.gateway.fault_reason,
                now_ns=now_ns,
                validity_ns=validity,
            )
        )
        evidence.append(
            PolicyCompatibilityProvider().evaluate(
                self._compatibility,
                task_id=package.descriptor.task_id,
                hand_model=self.binding.hand.model,
                hand_side=self.binding.hand.side,
                package_id=package.descriptor.package_id,
                now_ns=now_ns,
                validity_ns=validity,
            )
        )
        policy = ReadinessPolicy(
            package.descriptor.task_id,
            tuple(
                ReadinessRequirement(provider_id, RequirementLevel.REQUIRED)
                for provider_id in self.binding.readiness.required_provider_ids
            ),
        )
        return ReadinessAggregator().evaluate(policy, tuple(evidence), now_ns=now_ns)

    def _wait_for_initial_inputs(self, timeout_s: float) -> tuple[HandState, Any]:
        deadline = time.monotonic() + timeout_s
        manus_sample = None
        hand_state = None
        while time.monotonic() < deadline and not self._stop.is_set():
            latest = self._manus_buffer.take_latest(timeout_s=0.02)
            if latest is not None:
                _, manus_sample = latest
            hand_state = self.gateway.latest_state
            if manus_sample is not None and hand_state is not None:
                return hand_state, manus_sample
        raise TimeoutError(
            "fresh initial teleoperation input and hand state were not both available"
        )

    def _render_status(self, readiness, result, manus_health: str) -> None:
        supervisor = self._supervisor
        session = self._policy_session
        hand_owner, arm_owner = self._owners(supervisor.state)
        status = None if session is None else session.status
        self.status_renderer.render(
            RuntimeStatus(
                state=supervisor.state.value,
                hand_owner=hand_owner,
                arm_owner=arm_owner,
                control_epoch=supervisor.hand_epoch,
                hand_health="fresh",
                manus_health=manus_health,
                gateway_health="healthy" if self.gateway.fault_reason is None else "faulted",
                policy_name=self.preflight.policy_package.descriptor.display_name,
                policy_compatible=self._compatibility.compatible,
                history_count=None if status is None else status.history_count,
                history_required=None if status is None else status.history_required,
                blend_alpha=result.blend_alpha,
                readiness_ready=readiness.ready,
                rejection_reason=result.rejection_reason,
                recording=True,
            )
        )

    def run(
        self, *, max_ticks: int = 0, initial_input_timeout_s: float = 10.0
    ) -> ApplicationResult:
        if self._started:
            raise RuntimeError("HandOnlyRuntime can be run only once")
        self._started = True
        try:
            self.gateway.start()
            self.retargeter.reset()
            self.manus_source.start(self._manus_buffer.publish)
            self.switch_source.start(self._on_switch)
            initial_state, latest_manus = self._wait_for_initial_inputs(initial_input_timeout_s)
            self._supervisor = self._build_supervisor(initial_state)
            prepare_retargeter = getattr(self.retargeter, "prepare", None)
            if prepare_retargeter is not None:
                prepare_retargeter(
                    latest_manus,
                    control_session_id=self.binding.control_session_id,
                    control_epoch=self._supervisor.hand_epoch,
                    task_id=self.preflight.policy_package.descriptor.task_id,
                    task_version=self.preflight.policy_package.descriptor.task_version,
                )
            self._policy_session = PolicySession(self.preflight.policy_package)
            self._emit_event("runtime-start", state=self._supervisor.state.value)
            self._connected.set()
            period_ns = self.preflight.policy_package.codec_spec.control_period_ns
            if self.binding.handoff.teleop_command_period_ns != period_ns:
                raise ValueError(
                    "hand-only runtime requires teleop and selected policy cadence to match"
                )
            next_tick_ns = self.monotonic_ns()
            next_status_ns = next_tick_ns
            previous_state = self._supervisor.state

            while not self._stop.is_set() and (max_ticks == 0 or self._ticks < max_ticks):
                now_ns = self.monotonic_ns()
                wait_ns = next_tick_ns - now_ns
                if wait_ns > 0:
                    self._stop.wait(wait_ns / 1_000_000_000)
                    if self._stop.is_set():
                        break
                actual_ns = self.monotonic_ns()
                scheduled_ns = next_tick_ns
                lateness_ns = max(0, actual_ns - scheduled_ns)
                next_tick_ns += period_ns

                latest = self._manus_buffer.take_latest(timeout_s=0.0)
                if latest is not None:
                    _, latest_manus = latest
                source_status = self.manus_source.status(actual_ns)
                if source_status.health.value != "healthy":
                    self.operator_confirmation.invalidate("Manus-source-not-healthy")
                hand_state = self.gateway.latest_state
                if hand_state is None:
                    raise RuntimeError("exclusive hand gateway has no state")
                teleop_candidate = self.retargeter.retarget(
                    latest_manus,
                    control_session_id=self.binding.control_session_id,
                    control_epoch=self._supervisor.hand_epoch,
                    task_id=self.preflight.policy_package.descriptor.task_id,
                    task_version=self.preflight.policy_package.descriptor.task_version,
                )
                readiness = self._readiness(hand_state, actual_ns)

                if self._switch_overflow:
                    raise RuntimeError("operator switch event queue overflowed")

                result = self._supervisor.tick(
                    hand_state,
                    teleop_candidate,
                    readiness,
                    scheduled_time_ns=scheduled_ns,
                    actual_time_ns=actual_ns,
                )
                self._ticks += 1

                if (
                    self._supervisor.state is HandoffState.TELEOP_ACTIVE
                    and self.operator_confirmation.current is not None
                    and readiness.ready
                    and self._policy_session.status.state.value in ("loaded", "deactivated")
                ):
                    self._supervisor.arm_policy(
                        self._policy_session,
                        self._compatibility,
                        hand_state,
                        readiness,
                        now_ns=actual_ns,
                    )
                    result = result.__class__(
                        self._supervisor.state,
                        result.effective_target,
                        result.blend_alpha,
                        self._policy_session.status.history_count,
                        result.rejection_reason,
                    )

                readiness_record = to_primitive(readiness)
                authorized_command = self._supervisor.last_authorized_command
                gateway_acknowledgement = (
                    None
                    if self._supervisor.last_gateway_acknowledgement is None
                    else to_primitive(self._supervisor.last_gateway_acknowledgement)
                )
                command_deadline_ns = (
                    None if authorized_command is None else authorized_command.deadline_ns
                )

                while True:
                    try:
                        switch_event = self._switch_events.get_nowait()
                    except queue.Empty:
                        break
                    if is_toggle_request(switch_event):
                        request = self._supervisor.request_toggle()
                        self._emit_event(
                            "operator-switch",
                            state=self._supervisor.state.value,
                            requested_transition=request.reason,
                            reason_code=None if request.accepted else request.reason,
                            operator_action="F12-press",
                            readiness=readiness_record,
                            deadline_ns=command_deadline_ns,
                            gateway_acknowledgement=gateway_acknowledgement,
                        )

                if self._supervisor.state is not previous_state:
                    self._emit_event(
                        "transition",
                        state=self._supervisor.state.value,
                        requested_transition=self._supervisor.state.value,
                        readiness=readiness_record,
                        deadline_ns=command_deadline_ns,
                        gateway_acknowledgement=gateway_acknowledgement,
                    )
                    previous_state = self._supervisor.state
                if result.rejection_reason:
                    self._emit_event(
                        "rejection",
                        state=self._supervisor.state.value,
                        reason_code=result.rejection_reason,
                        readiness=readiness_record,
                        deadline_ns=command_deadline_ns,
                        gateway_acknowledgement=gateway_acknowledgement,
                    )

                hand_owner, arm_owner = self._owners(self._supervisor.state)
                mapping_preview = self.preflight.mapper.preview(
                    result.effective_target.semantic_position
                )
                session_trace = getattr(self._policy_session, "last_inference", None)
                live_telemetry = ControlLoopTelemetry(
                    tick=self._ticks - 1,
                    actual_time_ns=actual_ns,
                    scheduled_time_ns=scheduled_ns,
                    lateness_ns=lateness_ns,
                    control_period_ns=period_ns,
                    state=self._supervisor.state.value,
                    hand_owner=hand_owner,
                    arm_owner=arm_owner,
                    control_epoch=self._supervisor.hand_epoch,
                    manus_sample=latest_manus,
                    manus_source_status=source_status,
                    teleop_candidate=teleop_candidate,
                    policy_candidate=self._policy_session.last_preview,
                    requested_candidate=self._supervisor.last_requested_candidate,
                    hand_state=hand_state,
                    authorized_command=authorized_command,
                    gateway_acknowledgement=self._supervisor.last_gateway_acknowledgement,
                    effective_target=result.effective_target,
                    readiness=readiness,
                    mapping_preview=mapping_preview,
                    blend_alpha=result.blend_alpha,
                    rejection_reason=result.rejection_reason,
                )
                with self._telemetry_lock:
                    self._latest_control_telemetry = live_telemetry
                self.trace_recorder.record(
                    monotonic_time_ns=actual_ns,
                    control_session_id=self.binding.control_session_id,
                    state=self._supervisor.state.value,
                    hand_owner=hand_owner,
                    arm_owner=arm_owner,
                    control_epoch=self._supervisor.hand_epoch,
                    policy_package_id=self.preflight.policy_package.descriptor.package_id,
                    payload={
                        "manus_sample": {
                            "sequence": latest_manus.sequence,
                            "generated_time_ns": latest_manus.generated_time_ns,
                            "received_time_ns": latest_manus.received_time_ns,
                            "health": latest_manus.source_health.value,
                            "coordinate_frame_id": latest_manus.coordinate_frame_id,
                            "units": latest_manus.units,
                            "diagnostics": latest_manus.diagnostics,
                        },
                        "manus_source_status": source_status,
                        "teleop_candidate": teleop_candidate,
                        "policy_candidate": self._policy_session.last_preview,
                        "policy_inference": session_trace,
                        "hand_state": hand_state,
                        "authorized_command": authorized_command,
                        "gateway_acknowledgement": gateway_acknowledgement,
                        "arbitration_result": result,
                        "effective_target": result.effective_target,
                        "readiness": readiness,
                        "arm_hold": self._supervisor.arm_gateway.status,
                        "mapping_preview": mapping_preview,
                        "scheduler": {
                            "scheduled_time_ns": scheduled_ns,
                            "actual_time_ns": actual_ns,
                            "lateness_ns": lateness_ns,
                            "control_period_ns": period_ns,
                        },
                        "switch_status": self.switch_source.status,
                    },
                )
                if actual_ns >= next_status_ns:
                    self._render_status(readiness, result, source_status.health.value)
                    next_status_ns = actual_ns + self.binding.status.period_ns
            self._emit_event("runtime-stop", state=self._supervisor.state.value)
            return ApplicationResult(
                0,
                self._ticks,
                self._supervisor.state.value,
                self._supervisor.last_rejection,
            )
        except BaseException as exc:
            state = "DISCONNECTED" if self._supervisor is None else self._supervisor.state.value
            self._emit_event(
                "runtime-fault",
                state=state,
                reason_code=f"{type(exc).__name__}:{exc}",
                safe_response="gateway-watchdog-or-safe-hold",
            )
            raise
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._policy_session is not None:
            self._policy_session.close()
        self._stop.set()
        for component in (self.switch_source, self.manus_source, self.gateway):
            try:
                component.stop()
            except BaseException:
                pass
        try:
            self.arm_gateway.close()
        except BaseException:
            pass
        self.status_renderer.close()
        self.trace_recorder.close()
        self.event_logger.close()
