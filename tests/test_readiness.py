from __future__ import annotations

from dex_contracts import (
    ReadinessPolicy,
    ReadinessRequirement,
    ReadinessResult,
    RequirementLevel,
    TaskReadinessEvidence,
)
from dex_runtime.readiness import OperatorConfirmationProvider, ReadinessAggregator


def _evidence(provider_id: str, result: ReadinessResult, *, valid_until_ns: int = 200) -> TaskReadinessEvidence:
    return TaskReadinessEvidence(
        provider_id=provider_id,
        provider_version="1",
        task_id="task",
        hand_model="LinkerHand G20",
        hand_side="left",
        generated_time_ns=100,
        valid_until_ns=valid_until_ns,
        result=result,
        measurements=(),
        reason_codes=() if result is ReadinessResult.PASS else ("failed",),
        confidence=1.0,
        evidence_refs=(),
    )


def test_required_readiness_is_provider_neutral_and_expiry_aware() -> None:
    policy = ReadinessPolicy(
        "task",
        (
            ReadinessRequirement("operator", RequirementLevel.REQUIRED),
            ReadinessRequirement("freshness", RequirementLevel.REQUIRED),
            ReadinessRequirement("camera", RequirementLevel.OPTIONAL),
        ),
    )
    aggregator = ReadinessAggregator()
    ready = aggregator.evaluate(
        policy,
        (
            _evidence("operator", ReadinessResult.OPERATOR_CONFIRMED),
            _evidence("freshness", ReadinessResult.PASS),
        ),
        now_ns=150,
    )
    assert ready.ready
    expired = aggregator.evaluate(policy, ready.evidence, now_ns=201)
    assert not expired.ready
    assert set(expired.blocking_reasons) == {
        "required-evidence-expired:operator",
        "required-evidence-expired:freshness",
    }


def test_operator_confirmation_records_identity_and_can_be_invalidated() -> None:
    provider = OperatorConfirmationProvider()
    evidence = provider.confirm(
        task_id="task",
        hand_model="LinkerHand G20",
        hand_side="left",
        operator_id="operator-1",
        control_session_id="session",
        policy_package_id="sha256:package",
        displayed_evidence_digest="sha256:evidence",
        now_ns=100,
        validity_ns=50,
    )
    assert evidence.result is ReadinessResult.OPERATOR_CONFIRMED
    assert provider.current == evidence
    provider.invalidate("material-movement")
    assert provider.current is None
