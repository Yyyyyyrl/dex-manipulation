"""Semantic hand-command safety checks and authorization."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from dex_contracts import (
    PROTOCOL_VERSION,
    AuthorizedHandCommand,
    CommandMode,
    EffectiveHandTarget,
    HandCandidate,
    HandState,
    MessageIdentity,
    OwnerKind,
    ResourceId,
)


@dataclass(frozen=True)
class HandSafetyLimits:
    """Deployment-owned envelope every hand command is checked against.

    These are runtime limits, deliberately independent of whatever bounds a
    policy package declares: a package can only ever be more conservative than
    the deployment, never less. All angles are semantic joint radians in the
    order given by the calibration's semantic schema.
    """

    #: Per-joint lower bound, radians. Length defines the semantic width, and
    #: any candidate of a different width is rejected outright.
    position_lower_rad: tuple[float, ...]
    #: Per-joint upper bound, radians. Must exceed the lower bound.
    position_upper_rad: tuple[float, ...]
    #: Largest movement of any single joint between consecutive commands, rad.
    maximum_delta_per_tick_rad: float
    #: Same bound expressed as a velocity, rad/s. The effective per-tick cap is
    #: the smaller of this scaled by the control period and the field above, so
    #: shortening the control period tightens the step automatically.
    maximum_target_rate_rad_s: float
    #: Largest tolerated gap between commanded target and measured position,
    #: rad. Catches a hand that is jammed, unpowered, or fighting the command.
    maximum_following_error_rad: float
    #: Oldest hand state that may still justify a command, nanoseconds.
    maximum_state_age_ns: int
    #: How long an authorized command stays valid, nanoseconds. Should not
    #: outlive one control period, or a stale command could supersede a newer
    #: one at the gateway.
    command_deadline_ns: int

    def __post_init__(self) -> None:
        if not self.position_lower_rad or len(self.position_lower_rad) != len(
            self.position_upper_rad
        ):
            raise ValueError("hand safety position limits are incomplete")
        if any(
            upper <= lower
            for lower, upper in zip(self.position_lower_rad, self.position_upper_rad, strict=False)
        ):
            raise ValueError("hand safety upper limits must exceed lower limits")
        if any(
            value <= 0
            for value in (
                self.maximum_delta_per_tick_rad,
                self.maximum_target_rate_rad_s,
                self.maximum_following_error_rad,
                self.maximum_state_age_ns,
                self.command_deadline_ns,
            )
        ):
            raise ValueError("hand safety limits and deadlines must be positive")


@dataclass(frozen=True)
class HandGatewayBinding:
    """The identity every command and every input must agree on.

    Proven once during preflight and then held fixed for the session. Each
    field is compared against the incoming candidate and hand state, so a
    mismatch (wrong hand, stale calibration, a candidate from a previous
    session) is a rejection rather than a silently mis-actuated command.
    """

    control_session_id: str
    command_source_id: str
    hand_model: str
    hand_side: str
    semantic_schema_id: str
    task_id: str
    task_version: str
    policy_package_id: str
    calibration_id: str
    mapping_digest: str


@dataclass(frozen=True)
class HandSafetyDecision:
    accepted: bool
    reason_codes: tuple[str, ...]


class HandSafetySupervisor:
    def __init__(self, limits: HandSafetyLimits, binding: HandGatewayBinding) -> None:
        self.limits = limits
        self.binding = binding
        self._sequence = 0

    def evaluate(
        self,
        candidate: HandCandidate,
        hand_state: HandState,
        effective_target: EffectiveHandTarget,
        *,
        now_ns: int,
        control_period_ns: int,
    ) -> HandSafetyDecision:
        """Check a candidate without authorizing it, returning every reason it fails.

        Deliberately collects all reason codes instead of returning on the first
        one, so an operator sees the whole picture in the trace rather than
        fixing faults one restart at a time. The codes fall into five groups:

        - shape:    ``semantic-width-mismatch``, ``non-finite-hand-vector``
        - identity: ``*-mismatch`` for hand model/side/schema/session/task/
                    package/calibration, on both the candidate and the hand state
        - freshness: ``candidate-expired``, ``hand-state-stale``
        - health:   ``hand-state-unhealthy`` (not fresh, faulted, or missing joints)
        - envelope: ``position-limit:<joint index>``, ``target-delta-limit``,
                    ``following-error-limit``

        A width mismatch returns immediately, because every later check indexes
        the vectors and would raise instead of reporting.
        """
        reasons: list[str] = []
        target = candidate.semantic_position
        measured = hand_state.semantic_position
        effective = effective_target.semantic_position
        width = len(self.limits.position_lower_rad)
        if len(target) != width or len(measured) != width or len(effective) != width:
            reasons.append("semantic-width-mismatch")
            return HandSafetyDecision(False, tuple(reasons))
        if any(not math.isfinite(value) for value in (*target, *measured, *effective)):
            reasons.append("non-finite-hand-vector")
        if candidate.identity.hand_model != self.binding.hand_model:
            reasons.append("hand-model-mismatch")
        if candidate.identity.hand_side != self.binding.hand_side:
            reasons.append("hand-side-mismatch")
        if candidate.identity.semantic_schema_id != self.binding.semantic_schema_id:
            reasons.append("semantic-schema-mismatch")
        if candidate.identity.control_session_id != self.binding.control_session_id:
            reasons.append("control-session-mismatch")
        if candidate.identity.task_id != self.binding.task_id:
            reasons.append("task-id-mismatch")
        if candidate.identity.task_version != self.binding.task_version:
            reasons.append("task-version-mismatch")
        if candidate.identity.policy_package_id not in (None, self.binding.policy_package_id):
            reasons.append("policy-package-mismatch")
        if candidate.identity.calibration_id not in (None, self.binding.calibration_id):
            reasons.append("calibration-mismatch")
        if hand_state.identity.control_session_id != self.binding.control_session_id:
            reasons.append("hand-state-session-mismatch")
        if hand_state.identity.hand_model != self.binding.hand_model:
            reasons.append("hand-state-model-mismatch")
        if hand_state.identity.hand_side != self.binding.hand_side:
            reasons.append("hand-state-side-mismatch")
        if hand_state.identity.semantic_schema_id != self.binding.semantic_schema_id:
            reasons.append("hand-state-schema-mismatch")
        if hand_state.identity.calibration_id != self.binding.calibration_id:
            reasons.append("hand-state-calibration-mismatch")
        # Candidate timestamps come from the actual source callback clock, while
        # authorization is scheduled against the nominal control tick. Accept
        # bounded intra-period arrival skew, but reject genuinely future data.
        future_skew_ns = candidate.generated_time_ns - now_ns
        candidate_validation_ns = max(now_ns, candidate.generated_time_ns)
        if future_skew_ns > control_period_ns or not candidate.valid_at(candidate_validation_ns):
            reasons.append("candidate-expired")
        if now_ns - hand_state.acquisition_time_ns > self.limits.maximum_state_age_ns:
            reasons.append("hand-state-stale")
        if (
            hand_state.state_quality != "fresh"
            or hand_state.hardware_faults
            or any(hand_state.missing_joint_mask)
        ):
            reasons.append("hand-state-unhealthy")
        for index, (value, lower, upper) in enumerate(
            zip(
                target, self.limits.position_lower_rad, self.limits.position_upper_rad, strict=False
            )
        ):
            if value < lower or value > upper:
                reasons.append(f"position-limit:{index}")
        tick_limit = min(
            self.limits.maximum_delta_per_tick_rad,
            self.limits.maximum_target_rate_rad_s * control_period_ns / 1_000_000_000,
        )
        if (
            max(abs(value - previous) for value, previous in zip(target, effective, strict=False))
            > tick_limit
        ):
            reasons.append("target-delta-limit")
        if (
            max(
                abs(value - value_measured)
                for value, value_measured in zip(target, measured, strict=False)
            )
            > self.limits.maximum_following_error_rad
        ):
            reasons.append("following-error-limit")
        return HandSafetyDecision(not reasons, tuple(reasons))

    def authorize(
        self,
        candidate: HandCandidate,
        hand_state: HandState,
        effective_target: EffectiveHandTarget,
        *,
        owner: OwnerKind,
        control_epoch: int,
        now_ns: int,
        control_period_ns: int,
    ) -> AuthorizedHandCommand:
        """Evaluate a candidate and, if it passes, stamp it as an authorized command.

        This is the only way a command reaches a gateway. The returned command
        carries the session-fixed identity, the supervisor's `control_epoch`
        (so a gateway can reject anything issued by a superseded owner), a fresh
        `command_id`, and a deadline `command_deadline_ns` in the future.

        Raises ValueError listing every reason code when the candidate fails;
        callers are expected to treat that as a fall-back-to-safe-hold signal,
        not to retry the same candidate.
        """
        decision = self.evaluate(
            candidate,
            hand_state,
            effective_target,
            now_ns=now_ns,
            control_period_ns=control_period_ns,
        )
        if not decision.accepted:
            raise ValueError("hand safety rejected candidate: " + ",".join(decision.reason_codes))
        identity = MessageIdentity(
            protocol_version=PROTOCOL_VERSION,
            control_session_id=self.binding.control_session_id,
            source_id=self.binding.command_source_id,
            resource_id=ResourceId.HAND,
            hand_model=self.binding.hand_model,
            hand_side=self.binding.hand_side,
            semantic_schema_id=self.binding.semantic_schema_id,
            task_id=candidate.identity.task_id,
            task_version=candidate.identity.task_version,
            policy_package_id=candidate.identity.policy_package_id,
            calibration_id=self.binding.calibration_id,
            control_epoch=control_epoch,
            sequence=self._sequence,
        )
        self._sequence += 1
        return AuthorizedHandCommand(
            identity=identity,
            semantic_position=candidate.semantic_position,
            owner=owner,
            command_id=uuid.uuid4().hex,
            command_mode=CommandMode.SEMANTIC_POSITION,
            authorized_time_ns=now_ns,
            deadline_ns=now_ns + self.limits.command_deadline_ns,
            safety_decision="pass",
            calibration_id=self.binding.calibration_id,
            mapping_id=self.binding.mapping_digest,
        )
