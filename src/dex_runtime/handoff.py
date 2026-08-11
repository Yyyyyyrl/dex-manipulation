"""Tick-driven hand-only mixed-control supervisor for the adopted M2 path."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from dex_contracts import (
    AcknowledgementLevel,
    AuthorizedHandCommand,
    CommandMode,
    EffectiveHandTarget,
    HandCandidate,
    HandCommandAcknowledgement,
    HandState,
    OwnerKind,
    OwnershipState,
    PolicyCompatibility,
    PolicyHandCandidate,
    ReadinessResult,
    ReadinessSnapshot,
    ResourceId,
    TeleopHandCandidate,
)

from .policy_session import PolicySession, PolicySessionState
from .safety import HandSafetySupervisor


class _Ticket(Protocol):
    def wait(self, timeout_s: float) -> HandCommandAcknowledgement: ...


class HandGateway(Protocol):
    def prepare_ownership(self, ownership: OwnershipState): ...
    def commit_ownership(self, preparation) -> None: ...
    def submit(self, command) -> _Ticket: ...


class ArmGateway(Protocol):
    @property
    def status(self): ...
    def prepare_hold(self) -> None: ...
    def enter_hold(self) -> None: ...
    def verify_hold(self) -> bool: ...
    def reanchor_teleop(self) -> None: ...
    def release_to_teleop(self) -> None: ...


class HandoffState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    SAFE_HOLD = "SAFE_HOLD"
    TELEOP_ACTIVE = "TELEOP_ACTIVE"
    POLICY_PREFLIGHT = "POLICY_PREFLIGHT"
    RL_SHADOW = "RL_SHADOW"
    ARM_HOLD_PREPARE = "ARM_HOLD_PREPARE"
    ARM_HOLD_VERIFY = "ARM_HOLD_VERIFY"
    HAND_BLEND = "HAND_BLEND"
    RL_ACTIVE = "RL_ACTIVE"
    HAND_BACK_PREPARE = "HAND_BACK_PREPARE"
    HAND_BACK_BLEND = "HAND_BACK_BLEND"
    ARM_TELEOP_REANCHOR = "ARM_TELEOP_REANCHOR"
    ESTOP = "ESTOP"


@dataclass(frozen=True)
class HandoffConfig:
    control_session_id: str
    ownership_lease_ns: int
    gateway_ack_timeout_s: float
    teleop_command_period_ns: int
    policy_blend_ticks: int
    handback_blend_ticks: int
    required_readiness_provider_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.control_session_id or self.ownership_lease_ns <= 0:
            raise ValueError("handoff session and positive ownership lease are required")
        if (
            self.gateway_ack_timeout_s <= 0
            or self.teleop_command_period_ns <= 0
            or self.policy_blend_ticks <= 0
            or self.handback_blend_ticks <= 0
        ):
            raise ValueError("handoff acknowledgement and blend bounds must be positive")
        mandatory = {
            "operator-confirmation-v1",
            "hand-state-freshness-v1",
            "gateway-health-v1",
            "policy-compatibility-v1",
        }
        if not mandatory.issubset(self.required_readiness_provider_ids):
            raise ValueError("initial handoff readiness providers are incomplete")


@dataclass(frozen=True)
class SwitchRequestResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class HandoffTickResult:
    state: HandoffState
    effective_target: EffectiveHandTarget
    blend_alpha: float | None
    policy_history_count: int | None
    rejection_reason: str | None


class HandoffSupervisor:
    def __init__(
        self,
        config: HandoffConfig,
        hand_gateway: HandGateway,
        arm_gateway: ArmGateway,
        safety: HandSafetySupervisor,
    ) -> None:
        if safety.binding.control_session_id != config.control_session_id:
            raise ValueError("safety and handoff control-session IDs differ")
        self.config = config
        self.hand_gateway = hand_gateway
        self.arm_gateway = arm_gateway
        self.safety = safety
        self.state = HandoffState.DISCONNECTED
        self.effective_target: EffectiveHandTarget | None = None
        self.policy_session: PolicySession | None = None
        self._hand_epoch = 0
        self._policy_tick = 0
        self._activation_requested = False
        self._handback_requested = False
        self._blend_index = 0
        self._blend_source: EffectiveHandTarget | None = None
        self._last_rejection: str | None = None
        self._last_authorized_command: AuthorizedHandCommand | None = None
        self._last_gateway_acknowledgement: HandCommandAcknowledgement | None = None
        self._last_requested_candidate: HandCandidate | None = None

    @property
    def hand_epoch(self) -> int:
        return self._hand_epoch

    @property
    def last_rejection(self) -> str | None:
        return self._last_rejection

    @property
    def last_authorized_command(self) -> AuthorizedHandCommand | None:
        return self._last_authorized_command

    @property
    def last_gateway_acknowledgement(
        self,
    ) -> HandCommandAcknowledgement | None:
        return self._last_gateway_acknowledgement

    @property
    def last_requested_candidate(self) -> HandCandidate | None:
        return self._last_requested_candidate

    def _transfer(self, owner: OwnerKind, now_ns: int) -> None:
        self._hand_epoch += 1
        ownership = OwnershipState(
            control_session_id=self.config.control_session_id,
            resource_id=ResourceId.HAND,
            owner=owner,
            control_epoch=self._hand_epoch,
            command_mode=(
                CommandMode.SAFE_HOLD
                if owner is OwnerKind.SAFETY
                else CommandMode.SEMANTIC_POSITION
            ),
            start_time_ns=now_ns,
            expiry_time_ns=now_ns + self.config.ownership_lease_ns,
            gateway_acknowledged=True,
            watchdog_healthy=True,
        )
        preparation = self.hand_gateway.prepare_ownership(ownership)
        self.hand_gateway.commit_ownership(preparation)

    def start(self, effective_target: EffectiveHandTarget, *, now_ns: int) -> None:
        if self.state is not HandoffState.DISCONNECTED:
            raise RuntimeError("handoff supervisor is already started")
        self.effective_target = effective_target
        self._transfer(OwnerKind.TELEOP, now_ns)
        self.state = HandoffState.TELEOP_ACTIVE

    def _readiness_ok(self, snapshot: ReadinessSnapshot, now_ns: int) -> tuple[bool, str]:
        validation_time_ns = max(now_ns, snapshot.evaluated_time_ns)
        if not snapshot.ready:
            return False, ";".join(snapshot.blocking_reasons) or "readiness-not-ready"
        by_id = {item.provider_id: item for item in snapshot.evidence}
        for provider_id in self.config.required_readiness_provider_ids:
            item = by_id.get(provider_id)
            if item is None:
                return False, f"readiness-missing:{provider_id}"
            if not item.valid_at(validation_time_ns):
                return False, f"readiness-expired:{provider_id}"
            if item.result not in (ReadinessResult.PASS, ReadinessResult.OPERATOR_CONFIRMED):
                return False, f"readiness-failed:{provider_id}"
        return True, ""

    def arm_policy(
        self,
        session: PolicySession,
        compatibility: PolicyCompatibility,
        hand_state: HandState,
        readiness: ReadinessSnapshot,
        *,
        now_ns: int,
    ) -> None:
        if self.state is not HandoffState.TELEOP_ACTIVE:
            raise RuntimeError("policy can be armed only while teleoperation owns the hand")
        if not compatibility.compatible:
            raise ValueError("policy preflight failed: " + ",".join(compatibility.reason_codes))
        ready, reason = self._readiness_ok(readiness, now_ns)
        if not ready:
            raise ValueError(f"policy readiness failed: {reason}")
        if (
            self.effective_target is None
            or self.effective_target.evidence_level < AcknowledgementLevel.SENT_TO_BUS
        ):
            raise RuntimeError("effective teleoperation target evidence is missing")
        self.state = HandoffState.POLICY_PREFLIGHT
        session.reset(
            hand_state.semantic_position,
            self.effective_target.semantic_position,
            control_session_id=self.config.control_session_id,
            source_id="policy-session",
            control_epoch=self._hand_epoch,
        )
        self.policy_session = session
        self._policy_tick = 0
        self.state = HandoffState.RL_SHADOW

    def request_toggle(self) -> SwitchRequestResult:
        if self.state is HandoffState.RL_SHADOW:
            self._activation_requested = True
            return SwitchRequestResult(True, "activation-requested")
        if self.state is HandoffState.RL_ACTIVE:
            self._handback_requested = True
            return SwitchRequestResult(True, "handback-requested")
        return SwitchRequestResult(False, f"toggle-invalid-in-{self.state.value}")

    def _candidate_valid(self, candidate: TeleopHandCandidate, now_ns: int) -> bool:
        # Source callbacks run against the actual monotonic clock while the
        # supervisor is ticked with the scheduled clock. A sample received
        # just after its scheduled tick is fresh, not a future-dated sample.
        # Bound that allowance to one declared control period so a genuinely
        # future timestamp remains invalid.
        future_skew_ns = candidate.generated_time_ns - now_ns
        if future_skew_ns > self.config.teleop_command_period_ns:
            return False
        validation_time_ns = max(now_ns, candidate.generated_time_ns)
        return candidate.valid_at(validation_time_ns) and (
            candidate.identity.control_session_id == self.config.control_session_id
            and candidate.identity.resource_id is ResourceId.HAND
            and candidate.identity.control_epoch == self._hand_epoch
            and candidate.identity.task_id == self.safety.binding.task_id
            and candidate.identity.task_version == self.safety.binding.task_version
            and candidate.identity.policy_package_id is None
            and candidate.identity.hand_model == self.safety.binding.hand_model
            and candidate.identity.hand_side == self.safety.binding.hand_side
            and candidate.identity.semantic_schema_id == self.safety.binding.semantic_schema_id
        )

    def _send(
        self,
        candidate: HandCandidate,
        hand_state: HandState,
        *,
        owner: OwnerKind,
        now_ns: int,
        control_period_ns: int,
    ) -> EffectiveHandTarget:
        if self.effective_target is None:
            raise RuntimeError("cannot authorize without effective-target evidence")
        self._last_requested_candidate = candidate
        command = self.safety.authorize(
            candidate,
            hand_state,
            self.effective_target,
            owner=owner,
            control_epoch=self._hand_epoch,
            now_ns=now_ns,
            control_period_ns=control_period_ns,
        )
        self._last_authorized_command = command
        acknowledgement = self.hand_gateway.submit(command).wait(self.config.gateway_ack_timeout_s)
        self._last_gateway_acknowledgement = acknowledgement
        self.effective_target = acknowledgement.effective_target
        return acknowledgement.effective_target

    def _shadow_teleop_tick(
        self,
        hand_state: HandState,
        teleop_candidate: TeleopHandCandidate,
        scheduled_time_ns: int,
        decision_time_ns: int,
    ) -> PolicyHandCandidate | None:
        if self.policy_session is None:
            raise RuntimeError("shadow state has no policy session")
        if not self._candidate_valid(teleop_candidate, decision_time_ns):
            raise ValueError("fresh compatible teleoperation candidate is required")
        effective = self._send(
            teleop_candidate,
            hand_state,
            owner=OwnerKind.TELEOP,
            now_ns=decision_time_ns,
            control_period_ns=self.policy_session.codec.spec.control_period_ns,
        )
        self.policy_session.observe(
            hand_state.semantic_position,
            effective.semantic_position,
            tick=self._policy_tick,
            scheduled_time_ns=scheduled_time_ns,
            state_sequence=hand_state.identity.sequence,
        )
        self._policy_tick += 1
        if self.policy_session.status.history_ready:
            return self.policy_session.preview()
        return None

    def _policy_observe_and_preview(
        self, hand_state: HandState, scheduled_time_ns: int
    ) -> tuple[int, PolicyHandCandidate]:
        if self.policy_session is None or self.effective_target is None:
            raise RuntimeError("policy session or effective target is missing")
        tick = self._policy_tick
        self.policy_session.observe(
            hand_state.semantic_position,
            self.effective_target.semantic_position,
            tick=tick,
            scheduled_time_ns=scheduled_time_ns,
            state_sequence=hand_state.identity.sequence,
        )
        self._policy_tick += 1
        return tick, self.policy_session.preview()

    def _blend_candidate(
        self,
        source: EffectiveHandTarget,
        destination: HandCandidate,
        alpha: float,
    ) -> HandCandidate:
        target = tuple(
            min(
                max((1.0 - alpha) * current + alpha * endpoint, lower),
                upper,
            )
            for current, endpoint, lower, upper in zip(
                source.semantic_position,
                destination.semantic_position,
                self.safety.limits.position_lower_rad,
                self.safety.limits.position_upper_rad,
                strict=False,
            )
        )
        identity = replace(destination.identity, source_id="handoff-transition")
        return HandCandidate(
            identity=identity,
            semantic_position=target,
            generated_time_ns=destination.generated_time_ns,
            valid_until_ns=destination.valid_until_ns,
            source_state_sequence=destination.source_state_sequence,
            diagnostics=destination.diagnostics + (("blend_alpha", alpha),),
            confidence=destination.confidence,
        )

    def tick(
        self,
        hand_state: HandState,
        teleop_candidate: TeleopHandCandidate,
        readiness: ReadinessSnapshot,
        *,
        scheduled_time_ns: int,
        actual_time_ns: int | None = None,
    ) -> HandoffTickResult:
        # Policy history is indexed by the nominal cadence, while freshness,
        # safety authorization, leases, and readiness must use the clock time
        # at which this decision is actually made. Keeping these clocks
        # separate prevents a late hardware round trip from making a newly
        # received local Manus sample look future-dated.
        decision_time_ns = scheduled_time_ns if actual_time_ns is None else actual_time_ns
        if decision_time_ns < scheduled_time_ns:
            raise ValueError("actual handoff time cannot precede scheduled time")
        self._last_rejection = None
        self._last_authorized_command = None
        self._last_gateway_acknowledgement = None
        self._last_requested_candidate = None
        alpha: float | None = None
        if self.effective_target is None:
            raise RuntimeError("handoff supervisor has no effective target")

        hold_states = (
            HandoffState.HAND_BLEND,
            HandoffState.RL_ACTIVE,
            HandoffState.HAND_BACK_PREPARE,
            HandoffState.HAND_BACK_BLEND,
            HandoffState.ARM_TELEOP_REANCHOR,
        )
        if self.state in hold_states:
            try:
                hold_verified = self.arm_gateway.verify_hold()
            except BaseException as exc:  # hardware boundary: fail closed
                hold_verified = False
                self._last_rejection = f"arm-hold-check-failed:{type(exc).__name__}"
            if not hold_verified:
                reason = self._last_rejection or "arm-hold-lost"
                self.enter_safe_hold(reason, now_ns=decision_time_ns)

        if self.state is HandoffState.TELEOP_ACTIVE:
            if not self._candidate_valid(teleop_candidate, decision_time_ns):
                raise ValueError("fresh compatible teleoperation candidate is required")
            self._send(
                teleop_candidate,
                hand_state,
                owner=OwnerKind.TELEOP,
                now_ns=decision_time_ns,
                control_period_ns=self.config.teleop_command_period_ns,
            )

        elif self.state in (
            HandoffState.RL_SHADOW,
            HandoffState.ARM_HOLD_PREPARE,
            HandoffState.ARM_HOLD_VERIFY,
        ):
            preview = self._shadow_teleop_tick(
                hand_state,
                teleop_candidate,
                scheduled_time_ns,
                decision_time_ns,
            )
            if self.state is HandoffState.RL_SHADOW and self._activation_requested:
                ready, reason = self._readiness_ok(readiness, decision_time_ns)
                if preview is None:
                    ready, reason = False, "policy-history-not-ready"
                if not ready:
                    self._last_rejection = reason
                    self._activation_requested = False
                else:
                    try:
                        self.arm_gateway.prepare_hold()
                    except BaseException as exc:  # freeze may have happened; fail closed
                        self.enter_safe_hold(
                            f"arm-hold-prepare-failed:{type(exc).__name__}",
                            now_ns=decision_time_ns,
                        )
                    else:
                        self.state = HandoffState.ARM_HOLD_PREPARE
                    self._activation_requested = False
            elif self.state is HandoffState.ARM_HOLD_PREPARE:
                ready, reason = self._readiness_ok(readiness, decision_time_ns)
                if not ready:
                    self.enter_safe_hold(reason, now_ns=decision_time_ns)
                else:
                    try:
                        self.arm_gateway.enter_hold()
                    except BaseException as exc:
                        self.enter_safe_hold(
                            f"arm-hold-enter-failed:{type(exc).__name__}",
                            now_ns=decision_time_ns,
                        )
                    else:
                        self.state = HandoffState.ARM_HOLD_VERIFY
            elif self.state is HandoffState.ARM_HOLD_VERIFY:
                ready, reason = self._readiness_ok(readiness, decision_time_ns)
                if not ready:
                    self.enter_safe_hold(reason, now_ns=decision_time_ns)
                else:
                    try:
                        hold_verified = self.arm_gateway.verify_hold()
                    except BaseException as exc:
                        self.enter_safe_hold(
                            f"arm-hold-verify-failed:{type(exc).__name__}",
                            now_ns=decision_time_ns,
                        )
                        hold_verified = False
                    if self.state is HandoffState.SAFE_HOLD:
                        pass
                    elif not hold_verified:
                        self._last_rejection = "arm-hold-not-verified"
                    else:
                        self._transfer(OwnerKind.TRANSITION, decision_time_ns)
                        self._blend_index = 0
                        self._blend_source = self.effective_target
                        self.state = HandoffState.HAND_BLEND

        elif self.state is HandoffState.HAND_BLEND:
            ready, reason = self._readiness_ok(readiness, decision_time_ns)
            if not ready:
                self.enter_safe_hold(reason, now_ns=decision_time_ns)
            else:
                if self._blend_source is None:
                    raise RuntimeError("policy blend source is missing")
                tick, preview = self._policy_observe_and_preview(hand_state, scheduled_time_ns)
                self._blend_index += 1
                alpha = min(1.0, self._blend_index / self.config.policy_blend_ticks)
                transition = self._blend_candidate(self._blend_source, preview, alpha)
                self._send(
                    transition,
                    hand_state,
                    owner=OwnerKind.TRANSITION,
                    now_ns=decision_time_ns,
                    control_period_ns=self.policy_session.codec.spec.control_period_ns,
                )
                if alpha >= 1.0:
                    self._transfer(OwnerKind.POLICY, decision_time_ns)
                    self.policy_session.activate(tick=tick, control_epoch=self._hand_epoch)
                    self._blend_source = None
                    self.state = HandoffState.RL_ACTIVE

        elif self.state is HandoffState.RL_ACTIVE:
            if self.policy_session is None:
                raise RuntimeError("RL_ACTIVE has no policy session")
            candidate = self.policy_session.step(
                hand_state.semantic_position,
                self.effective_target.semantic_position,
                tick=self._policy_tick,
                scheduled_time_ns=scheduled_time_ns,
                state_sequence=hand_state.identity.sequence,
            )
            self._policy_tick += 1
            self._send(
                candidate,
                hand_state,
                owner=OwnerKind.POLICY,
                now_ns=decision_time_ns,
                control_period_ns=self.policy_session.codec.spec.control_period_ns,
            )
            ready, reason = self._readiness_ok(readiness, decision_time_ns)
            if not ready:
                self.enter_safe_hold(reason, now_ns=decision_time_ns)
            elif self._handback_requested:
                self.state = HandoffState.HAND_BACK_PREPARE
                self._handback_requested = False

        elif self.state is HandoffState.HAND_BACK_PREPARE:
            if self.policy_session is None:
                raise RuntimeError("hand-back has no policy session")
            candidate = self.policy_session.step(
                hand_state.semantic_position,
                self.effective_target.semantic_position,
                tick=self._policy_tick,
                scheduled_time_ns=scheduled_time_ns,
                state_sequence=hand_state.identity.sequence,
            )
            self._policy_tick += 1
            self._send(
                candidate,
                hand_state,
                owner=OwnerKind.POLICY,
                now_ns=decision_time_ns,
                control_period_ns=self.policy_session.codec.spec.control_period_ns,
            )
            if not self._candidate_valid(teleop_candidate, decision_time_ns):
                self._last_rejection = "fresh-teleoperation-target-required-for-handback"
            else:
                self._transfer(OwnerKind.TRANSITION, decision_time_ns)
                self._blend_index = 0
                self._blend_source = self.effective_target
                self.state = HandoffState.HAND_BACK_BLEND

        elif self.state is HandoffState.HAND_BACK_BLEND:
            if not self._candidate_valid(teleop_candidate, decision_time_ns):
                self._last_rejection = "fresh-teleoperation-target-required-for-handback"
            else:
                if self._blend_source is None:
                    raise RuntimeError("hand-back blend source is missing")
                self._blend_index += 1
                alpha = min(1.0, self._blend_index / self.config.handback_blend_ticks)
                transition = self._blend_candidate(self._blend_source, teleop_candidate, alpha)
                self._send(
                    transition,
                    hand_state,
                    owner=OwnerKind.TRANSITION,
                    now_ns=decision_time_ns,
                    control_period_ns=self.policy_session.codec.spec.control_period_ns,
                )
                if alpha >= 1.0:
                    self._transfer(OwnerKind.TELEOP, decision_time_ns)
                    self.policy_session.deactivate()
                    self.arm_gateway.reanchor_teleop()
                    self._blend_source = None
                    self.state = HandoffState.ARM_TELEOP_REANCHOR

        elif self.state is HandoffState.ARM_TELEOP_REANCHOR:
            if not self._candidate_valid(teleop_candidate, decision_time_ns):
                self._last_rejection = "fresh-teleoperation-target-required-after-reanchor"
            else:
                self._send(
                    teleop_candidate,
                    hand_state,
                    owner=OwnerKind.TELEOP,
                    now_ns=decision_time_ns,
                    control_period_ns=self.config.teleop_command_period_ns,
                )
                self.arm_gateway.release_to_teleop()
                self.state = HandoffState.TELEOP_ACTIVE

        history_count = (
            None if self.policy_session is None else self.policy_session.status.history_count
        )
        return HandoffTickResult(
            self.state,
            self.effective_target,
            alpha,
            history_count,
            self._last_rejection,
        )

    def enter_safe_hold(self, reason: str, *, now_ns: int) -> None:
        if self.state in (HandoffState.ESTOP, HandoffState.DISCONNECTED):
            return
        self._last_rejection = reason
        self._transfer(OwnerKind.SAFETY, now_ns)
        if self.policy_session is not None and self.policy_session.status.state in (
            PolicySessionState.SHADOW,
            PolicySessionState.ACTIVE,
        ):
            self.policy_session.deactivate()
        self.state = HandoffState.SAFE_HOLD
