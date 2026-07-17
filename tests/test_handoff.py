from __future__ import annotations
from dataclasses import replace

import pytest

from dex_contracts import (
    AcknowledgementLevel,
    EffectiveHandTarget,
    HandState,
    MessageIdentity,
    OwnerKind,
    PolicyCompatibility,
    PROTOCOL_VERSION,
    ReadinessResult,
    ReadinessSnapshot,
    ResourceId,
    TaskReadinessEvidence,
    TeleopHandCandidate,
)
from dex_runtime.clock import FakeClock
from dex_runtime.fake_arm import FakeArmGateway
from dex_runtime.fake_hand import FakeHandGateway
from dex_runtime.handoff import HandoffConfig, HandoffState, HandoffSupervisor
from dex_runtime.policy_package import validate_policy_package
from dex_runtime.policy_session import PolicySession, PolicySessionState
from dex_runtime.safety import (
    HandGatewayBinding,
    HandSafetyLimits,
    HandSafetySupervisor,
)
from policy_package_factory import (
    CALIBRATION_ID,
    CALIBRATION_LOWER,
    CALIBRATION_UPPER,
    SCHEMA_ID,
    write_test_package,
)


MIDPOINT = tuple((lower + upper) * 0.5 for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER))

REQUIRED = (
    "operator-confirmation-v1",
    "hand-state-freshness-v1",
    "gateway-health-v1",
    "policy-compatibility-v1",
)


def _identity(epoch: int, sequence: int, source_id: str) -> MessageIdentity:
    return MessageIdentity(
        protocol_version=PROTOCOL_VERSION,
        control_session_id="session",
        source_id=source_id,
        resource_id=ResourceId.HAND,
        hand_model="LinkerHand G20",
        hand_side="left",
        semantic_schema_id=SCHEMA_ID,
        task_id="mounted-screwdriver-rotation",
        task_version="1.0",
        policy_package_id=None,
        calibration_id=CALIBRATION_ID,
        control_epoch=epoch,
        sequence=sequence,
    )


def _readiness(now_ns: int) -> ReadinessSnapshot:
    evidence = []
    for provider_id in REQUIRED:
        evidence.append(
            TaskReadinessEvidence(
                provider_id=provider_id,
                provider_version="1",
                task_id="mounted-screwdriver-rotation",
                hand_model="LinkerHand G20",
                hand_side="left",
                generated_time_ns=now_ns,
                valid_until_ns=now_ns + 100_000_000_000,
                result=(
                    ReadinessResult.OPERATOR_CONFIRMED
                    if provider_id == "operator-confirmation-v1"
                    else ReadinessResult.PASS
                ),
                measurements=(),
                reason_codes=(),
                confidence=1.0,
                evidence_refs=(),
            )
        )
    return ReadinessSnapshot(
        task_id="mounted-screwdriver-rotation",
        evaluated_time_ns=now_ns,
        evidence=tuple(evidence),
        ready=True,
        blocking_reasons=(),
    )


def test_fake_arm_full_teleop_policy_handback_cycle_is_bumpless(tmp_path) -> None:
    clock = FakeClock(1_000_000_000)
    hand_gateway = FakeHandGateway("session", clock_ns=clock.now_ns)
    arm_gateway = FakeArmGateway()
    package = validate_policy_package(
        write_test_package(tmp_path / "policy"), allow_unsigned_local=True
    )
    binding = HandGatewayBinding(
        control_session_id="session",
        command_source_id="command-arbiter",
        hand_model="LinkerHand G20",
        hand_side="left",
        semantic_schema_id=SCHEMA_ID,
        task_id=package.descriptor.task_id,
        task_version=package.descriptor.task_version,
        policy_package_id=package.descriptor.package_id,
        calibration_id=CALIBRATION_ID,
        mapping_digest="mapping-digest",
    )
    safety = HandSafetySupervisor(
        HandSafetyLimits(
            position_lower_rad=CALIBRATION_LOWER,
            position_upper_rad=CALIBRATION_UPPER,
            maximum_delta_per_tick_rad=0.2,
            maximum_target_rate_rad_s=2.0,
            maximum_following_error_rad=0.5,
            maximum_state_age_ns=100_000_000,
            command_deadline_ns=50_000_000,
        ),
        binding,
    )
    supervisor = HandoffSupervisor(
        HandoffConfig(
            control_session_id="session",
            ownership_lease_ns=100_000_000_000,
            gateway_ack_timeout_s=0.1,
            teleop_command_period_ns=100_000_000,
            policy_blend_ticks=3,
            handback_blend_ticks=3,
            required_readiness_provider_ids=REQUIRED,
        ),
        hand_gateway,
        arm_gateway,
        safety,
    )
    initial = EffectiveHandTarget(
        semantic_position=MIDPOINT,
        command_id="initial",
        evidence_level=AcknowledgementLevel.SENT_TO_BUS,
        evidence_time_ns=clock.now_ns(),
    )
    supervisor.start(initial, now_ns=clock.now_ns())
    readiness = _readiness(clock.now_ns())
    session = PolicySession(package)

    sequence = 0

    def hand_state(now_ns: int) -> HandState:
        effective = supervisor.effective_target
        return HandState(
            identity=_identity(supervisor.hand_epoch, sequence, "fake-hand-state"),
            semantic_position=effective.semantic_position,
            semantic_velocity=None,
            semantic_effort=None,
            acquisition_time_ns=now_ns,
            raw_native_state_ref=None,
            state_quality="fresh",
            missing_joint_mask=(False,) * 16,
            hardware_faults=(),
            temperatures_c=None,
            last_effective_target=effective,
            acknowledgement_capability=AcknowledgementLevel.SENT_TO_BUS,
        )

    mismatched_time_ns = clock.now_ns()
    mismatched_candidate = TeleopHandCandidate(
        identity=replace(
            _identity(supervisor.hand_epoch, sequence, "manus-retargeter"),
            task_version="wrong-version",
        ),
        semantic_position=MIDPOINT,
        generated_time_ns=mismatched_time_ns,
        valid_until_ns=mismatched_time_ns + 100_000_000,
        source_state_sequence=sequence,
    )
    with pytest.raises(ValueError, match="fresh compatible Manus candidate"):
        supervisor.tick(
            hand_state(mismatched_time_ns),
            mismatched_candidate,
            readiness,
            scheduled_time_ns=mismatched_time_ns,
        )

    supervisor.arm_policy(
        session,
        PolicyCompatibility(True, ()),
        hand_state(clock.now_ns()),
        readiness,
        now_ns=clock.now_ns(),
    )

    def run_tick():
        nonlocal sequence
        clock.advance_ns(100_000_000)
        sequence += 1
        now_ns = clock.now_ns()
        candidate = TeleopHandCandidate(
            identity=_identity(supervisor.hand_epoch, sequence, "manus-retargeter"),
            semantic_position=MIDPOINT,
            generated_time_ns=now_ns,
            valid_until_ns=now_ns + 100_000_000,
            source_state_sequence=sequence,
            diagnostics=(),
            confidence=1.0,
        )
        return supervisor.tick(
            hand_state(now_ns),
            candidate,
            readiness,
            scheduled_time_ns=now_ns,
        )

    for _ in range(30):
        result = run_tick()
    assert result.state is HandoffState.RL_SHADOW
    assert result.policy_history_count == 30

    assert supervisor.request_toggle().accepted
    assert run_tick().state is HandoffState.ARM_HOLD_PREPARE
    assert run_tick().state is HandoffState.ARM_HOLD_VERIFY
    assert run_tick().state is HandoffState.HAND_BLEND
    blend_alphas = [run_tick().blend_alpha for _ in range(3)]
    assert blend_alphas == [1 / 3, 2 / 3, 1.0]
    assert supervisor.state is HandoffState.RL_ACTIVE
    assert arm_gateway.status.verified
    assert session.status.state is PolicySessionState.ACTIVE

    assert supervisor.request_toggle().accepted
    assert run_tick().state is HandoffState.HAND_BACK_PREPARE
    assert run_tick().state is HandoffState.HAND_BACK_BLEND
    back_alphas = [run_tick().blend_alpha for _ in range(3)]
    assert back_alphas == [1 / 3, 2 / 3, 1.0]
    assert supervisor.state is HandoffState.ARM_TELEOP_REANCHOR
    assert run_tick().state is HandoffState.TELEOP_ACTIVE
    assert hand_gateway.ownership.owner is OwnerKind.TELEOP
    assert not arm_gateway.status.active
    assert arm_gateway.status.anchor_generation == 1
    assert session.status.state is PolicySessionState.DEACTIVATED

    targets = [command.semantic_position for command in hand_gateway.sent_commands]
    for previous, current in zip(targets, targets[1:]):
        assert max(abs(a - b) for a, b in zip(previous, current)) <= 0.2 + 1e-9
