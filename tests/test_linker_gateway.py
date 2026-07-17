from __future__ import annotations
from dataclasses import replace

import time
import uuid

import pytest

from dex_contracts import (
    AcknowledgementLevel,
    AuthorizedHandCommand,
    CommandMode,
    MessageIdentity,
    OwnerKind,
    OwnershipState,
    PROTOCOL_VERSION,
    ResourceId,
)
from dex_hardware_linker import (
    FakeLinkerTransport,
    GatewayConfig,
    GatewayRejected,
    LinkerGateway,
    LinkerMapper,
)


def _identity(mapper: LinkerMapper, *, epoch: int, sequence: int = 0) -> MessageIdentity:
    calibration = mapper.calibration
    return MessageIdentity(
        protocol_version=PROTOCOL_VERSION,
        control_session_id="session",
        source_id="test-arbiter",
        resource_id=ResourceId.HAND,
        hand_model=calibration.hand_model,
        hand_side=calibration.hand_side,
        semantic_schema_id=calibration.semantic_schema_id,
        task_id="test-task",
        task_version="1.0",
        policy_package_id=None,
        calibration_id=calibration.calibration_id,
        control_epoch=epoch,
        sequence=sequence,
    )


def _ownership(epoch: int, now: int) -> OwnershipState:
    return OwnershipState(
        control_session_id="session",
        resource_id=ResourceId.HAND,
        owner=OwnerKind.TELEOP,
        control_epoch=epoch,
        command_mode=CommandMode.SEMANTIC_POSITION,
        start_time_ns=now - 1,
        expiry_time_ns=now + 2_000_000_000,
        gateway_acknowledged=True,
        watchdog_healthy=True,
    )


def _command(mapper: LinkerMapper, epoch: int, target: tuple[float, ...]) -> AuthorizedHandCommand:
    now = time.monotonic_ns()
    return AuthorizedHandCommand(
        identity=_identity(mapper, epoch=epoch),
        semantic_position=target,
        owner=OwnerKind.TELEOP,
        command_id=uuid.uuid4().hex,
        command_mode=CommandMode.SEMANTIC_POSITION,
        authorized_time_ns=now,
        deadline_ns=now + 500_000_000,
        safety_decision="pass",
        calibration_id=mapper.calibration.calibration_id,
        mapping_id=mapper.calibration.artifact_digest,
    )


def test_gateway_is_exclusive_epoch_checked_and_truthful():
    mapper = LinkerMapper.load()
    open_command = mapper.prepare([joint.lower for joint in mapper.calibration.joints]).native_range
    transport = FakeLinkerTransport(open_command)
    gateway = LinkerGateway(
        GatewayConfig("linker", "session", 200.0, 100_000_000, 1_000_000_000, 0.01),
        mapper,
        transport,
    )
    gateway.start()
    try:
        now = time.monotonic_ns()
        prepared = gateway.prepare_ownership(_ownership(1, now))
        gateway.commit_ownership(prepared)
        original_ownership = gateway.ownership
        target = tuple(0.5 * (joint.lower + joint.upper) for joint in mapper.calibration.joints)
        acknowledgement = gateway.submit(_command(mapper, 1, target)).wait(1.0)
        assert acknowledgement.gateway.level is AcknowledgementLevel.SENT_TO_BUS
        assert acknowledgement.effective_target.evidence_level is AcknowledgementLevel.SENT_TO_BUS
        assert acknowledgement.gateway.identity.task_id == "test-task"
        assert acknowledgement.gateway.identity.task_version == "1.0"
        renewed_ownership = gateway.ownership
        assert original_ownership is not None and renewed_ownership is not None
        assert renewed_ownership.control_epoch == original_ownership.control_epoch
        assert renewed_ownership.start_time_ns == acknowledgement.gateway.acknowledged_time_ns
        assert (
            renewed_ownership.expiry_time_ns - renewed_ownership.start_time_ns
            == original_ownership.expiry_time_ns - original_ownership.start_time_ns
        )
        assert len(transport.sent_commands) == 1
        wrong_hand = _command(mapper, 1, target)
        wrong_hand = replace(
            wrong_hand,
            identity=replace(wrong_hand.identity, hand_model="wrong-hand"),
        )
        with pytest.raises(GatewayRejected, match="hand model"):
            gateway.submit(wrong_hand)
        stale = _command(mapper, 0, target)
        with pytest.raises(GatewayRejected, match="epoch"):
            gateway.submit(stale)
    finally:
        gateway.stop()


def test_gateway_rejects_saturation_and_calibration_mismatch():
    mapper = LinkerMapper.load()
    initial = mapper.prepare([joint.lower for joint in mapper.calibration.joints]).native_range
    gateway = LinkerGateway(
        GatewayConfig("linker", "session", 200.0, 100_000_000, 1_000_000_000, 0.01),
        mapper,
        FakeLinkerTransport(initial),
    )
    gateway.start()
    try:
        gateway.commit_ownership(gateway.prepare_ownership(_ownership(1, time.monotonic_ns())))
        bad_target = tuple([10.0] + [0.0] * 15)
        with pytest.raises(GatewayRejected, match="saturated"):
            gateway.submit(_command(mapper, 1, bad_target)).wait(1.0)
        command = _command(mapper, 1, tuple([0.0] * 16))
        wrong = AuthorizedHandCommand(
            identity=command.identity,
            semantic_position=command.semantic_position,
            owner=command.owner,
            command_id=command.command_id,
            command_mode=command.command_mode,
            authorized_time_ns=command.authorized_time_ns,
            deadline_ns=command.deadline_ns,
            safety_decision=command.safety_decision,
            calibration_id="wrong",
            mapping_id=command.mapping_id,
        )
        with pytest.raises(GatewayRejected, match="calibration"):
            gateway.submit(wrong)
    finally:
        gateway.stop()
