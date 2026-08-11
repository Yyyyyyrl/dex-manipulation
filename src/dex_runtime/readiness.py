"""Provider-neutral readiness evidence aggregation and initial providers."""

from __future__ import annotations

from dataclasses import dataclass

from dex_contracts import (
    HandState,
    PolicyCompatibility,
    ReadinessPolicy,
    ReadinessResult,
    ReadinessSnapshot,
    RequirementLevel,
    TaskReadinessEvidence,
)


class ReadinessAggregator:
    def evaluate(
        self,
        policy: ReadinessPolicy,
        evidence: tuple[TaskReadinessEvidence, ...],
        *,
        now_ns: int,
    ) -> ReadinessSnapshot:
        by_provider: dict[str, TaskReadinessEvidence] = {}
        blockers: list[str] = []
        for item in evidence:
            if item.task_id != policy.task_id:
                blockers.append(f"evidence-task-mismatch:{item.provider_id}")
                continue
            previous = by_provider.get(item.provider_id)
            if previous is None or item.generated_time_ns > previous.generated_time_ns:
                by_provider[item.provider_id] = item
        for requirement in policy.requirements:
            item = by_provider.get(requirement.provider_id)
            if requirement.level is not RequirementLevel.REQUIRED:
                continue
            if item is None:
                blockers.append(f"required-evidence-missing:{requirement.provider_id}")
                continue
            if not item.valid_at(now_ns):
                blockers.append(f"required-evidence-expired:{requirement.provider_id}")
                continue
            if item.result not in (ReadinessResult.PASS, ReadinessResult.OPERATOR_CONFIRMED):
                reason = item.reason_codes[0] if item.reason_codes else item.result.value
                blockers.append(f"required-evidence-failed:{requirement.provider_id}:{reason}")
        return ReadinessSnapshot(
            task_id=policy.task_id,
            evaluated_time_ns=now_ns,
            evidence=tuple(by_provider[key] for key in sorted(by_provider)),
            ready=not blockers,
            blocking_reasons=tuple(blockers),
        )


@dataclass
class OperatorConfirmationProvider:
    provider_id: str = "operator-confirmation-v1"
    provider_version: str = "1"
    _current: TaskReadinessEvidence | None = None

    def confirm(
        self,
        *,
        task_id: str,
        hand_model: str,
        hand_side: str,
        operator_id: str,
        control_session_id: str,
        policy_package_id: str,
        displayed_evidence_digest: str,
        now_ns: int,
        validity_ns: int,
    ) -> TaskReadinessEvidence:
        if (
            not all((operator_id, control_session_id, policy_package_id, displayed_evidence_digest))
            or validity_ns <= 0
        ):
            raise ValueError("operator confirmation identity and positive validity are required")
        self._current = TaskReadinessEvidence(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            task_id=task_id,
            hand_model=hand_model,
            hand_side=hand_side,
            generated_time_ns=now_ns,
            valid_until_ns=now_ns + validity_ns,
            result=ReadinessResult.OPERATOR_CONFIRMED,
            measurements=(
                ("operator_id", operator_id),
                ("control_session_id", control_session_id),
                ("policy_package_id", policy_package_id),
            ),
            reason_codes=(),
            confidence=1.0,
            evidence_refs=(displayed_evidence_digest,),
        )
        return self._current

    def invalidate(self, reason: str) -> None:
        if not reason:
            raise ValueError("operator confirmation invalidation needs a reason")
        self._current = None

    @property
    def current(self) -> TaskReadinessEvidence | None:
        return self._current


class HandStateFreshnessProvider:
    provider_id = "hand-state-freshness-v1"
    provider_version = "1"

    def evaluate(
        self,
        hand_state: HandState,
        *,
        task_id: str,
        now_ns: int,
        maximum_age_ns: int,
        validity_ns: int,
    ) -> TaskReadinessEvidence:
        age = max(0, now_ns - hand_state.acquisition_time_ns)
        passed = (
            age <= maximum_age_ns
            and hand_state.state_quality == "fresh"
            and not hand_state.hardware_faults
            and not any(hand_state.missing_joint_mask)
            and hand_state.last_effective_target is not None
        )
        reasons: list[str] = []
        if age > maximum_age_ns:
            reasons.append("hand-state-stale")
        if hand_state.state_quality != "fresh":
            reasons.append("hand-state-quality")
        if hand_state.hardware_faults:
            reasons.append("hand-hardware-fault")
        if any(hand_state.missing_joint_mask):
            reasons.append("hand-joints-missing")
        if hand_state.last_effective_target is None:
            reasons.append("effective-target-evidence-missing")
        return TaskReadinessEvidence(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            task_id=task_id,
            hand_model=str(hand_state.identity.hand_model),
            hand_side=str(hand_state.identity.hand_side),
            generated_time_ns=now_ns,
            valid_until_ns=now_ns + validity_ns,
            result=ReadinessResult.PASS if passed else ReadinessResult.FAIL,
            measurements=(("age_ns", age),),
            reason_codes=tuple(reasons),
            confidence=1.0,
            evidence_refs=(),
        )


class GatewayHealthProvider:
    provider_id = "gateway-health-v1"
    provider_version = "1"

    def evaluate(
        self,
        *,
        task_id: str,
        hand_model: str,
        hand_side: str,
        healthy: bool,
        watchdog_healthy: bool,
        fault_reason: str | None,
        now_ns: int,
        validity_ns: int,
    ) -> TaskReadinessEvidence:
        passed = healthy and watchdog_healthy and fault_reason is None
        return TaskReadinessEvidence(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            task_id=task_id,
            hand_model=hand_model,
            hand_side=hand_side,
            generated_time_ns=now_ns,
            valid_until_ns=now_ns + validity_ns,
            result=ReadinessResult.PASS if passed else ReadinessResult.FAIL,
            measurements=(
                ("gateway_healthy", healthy),
                ("watchdog_healthy", watchdog_healthy),
            ),
            reason_codes=() if passed else (fault_reason or "gateway-unhealthy",),
            confidence=1.0,
            evidence_refs=(),
        )


class PolicyCompatibilityProvider:
    provider_id = "policy-compatibility-v1"
    provider_version = "1"

    def evaluate(
        self,
        compatibility: PolicyCompatibility,
        *,
        task_id: str,
        hand_model: str,
        hand_side: str,
        package_id: str,
        now_ns: int,
        validity_ns: int,
    ) -> TaskReadinessEvidence:
        return TaskReadinessEvidence(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            task_id=task_id,
            hand_model=hand_model,
            hand_side=hand_side,
            generated_time_ns=now_ns,
            valid_until_ns=now_ns + validity_ns,
            result=ReadinessResult.PASS if compatibility.compatible else ReadinessResult.FAIL,
            measurements=(("package_id", package_id),),
            reason_codes=compatibility.reason_codes,
            confidence=1.0,
            evidence_refs=(package_id,),
        )
