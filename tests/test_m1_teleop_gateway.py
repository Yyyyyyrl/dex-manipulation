from __future__ import annotations

import time
import uuid
from pathlib import Path

import numpy as np

from dex_contracts import (
    PROTOCOL_VERSION,
    AcknowledgementLevel,
    AuthorizedHandCommand,
    CommandMode,
    MessageIdentity,
    OwnerKind,
    OwnershipState,
    ResourceId,
    SourceHealth,
    TimestampedSample,
)
from dex_hardware_linker import FakeLinkerTransport, GatewayConfig, LinkerGateway, LinkerMapper
from dex_teleop_adapters import ManusKeypoints, ManusRetargeter, TeleopProfile

ROOT = Path(__file__).resolve().parents[1]


class _Optimizer:
    target_link_human_indices = np.arange(5)
    retargeting_type = "POSITION"


class _Filter:
    def reset(self) -> None:
        pass


class _SafeRetargeting:
    def __init__(self, semantic_joint_names: tuple[str, ...]) -> None:
        self.joint_names = tuple(reversed(semantic_joint_names))
        self.optimizer = _Optimizer()
        self.filter = _Filter()

    def reset(self) -> None:
        pass

    def retarget(self, _reference) -> np.ndarray:
        return np.asarray(
            [0.25 if name == "thumb_cmc_roll" else 0.0 for name in self.joint_names],
            dtype=np.float64,
        )


def _manus_sample(now_ns: int) -> TimestampedSample:
    points = []
    for index in range(25):
        finger = max(0, (index - 1) // 5)
        depth = index if index < 5 else (index - 5) % 5
        points.append((0.018 * (finger - 2), 0.018 * depth, 0.002 * finger))
    points[0] = (0.0, 0.0, 0.0)
    points[6] = (-0.03, 0.04, 0.0)
    points[11] = (0.0, 0.05, 0.005)
    payload = ManusKeypoints(
        source_id="manus-left",
        glove_id="fixture-glove",
        hand_side="left",
        points_m=tuple(points),
    )
    return TimestampedSample(
        payload=payload,
        generated_time_ns=None,
        received_time_ns=now_ns,
        sequence=0,
        source_health=SourceHealth.HEALTHY,
        validity_mask=(True,) * 25,
        coordinate_frame_id="manus-wrist-local-native",
        units="meter",
    )


def test_manus_candidate_reaches_hand_only_through_exclusive_gateway() -> None:
    profile = TeleopProfile.load(
        ROOT / "configs/teleop/linker_g20_left_manus_dexpilot_v1.json",
        ROOT,
    )
    now_ns = time.monotonic_ns()
    retargeter = ManusRetargeter(
        _SafeRetargeting(profile.semantic_joint_names),
        profile,
    )
    candidate = retargeter.retarget(
        _manus_sample(now_ns),
        control_session_id="m1-session",
        control_epoch=1,
        task_id=None,
            task_version=None,
    )

    mapper = LinkerMapper.load()
    initial = mapper.prepare([joint.lower for joint in mapper.calibration.joints]).native_range
    transport = FakeLinkerTransport(initial)
    gateway = LinkerGateway(
        GatewayConfig(
            "linker-g20-left",
            "m1-session",
            200.0,
            100_000_000,
            1_000_000_000,
            0.01,
        ),
        mapper,
        transport,
    )
    gateway.start()
    try:
        ownership = OwnershipState(
            control_session_id="m1-session",
            resource_id=ResourceId.HAND,
            owner=OwnerKind.TELEOP,
            control_epoch=1,
            command_mode=CommandMode.SEMANTIC_POSITION,
            start_time_ns=now_ns - 1,
            expiry_time_ns=now_ns + 2_000_000_000,
            gateway_acknowledged=True,
            watchdog_healthy=True,
        )
        gateway.commit_ownership(gateway.prepare_ownership(ownership))
        identity = MessageIdentity(
            protocol_version=PROTOCOL_VERSION,
            control_session_id="m1-session",
            source_id="m1-command-arbiter",
            resource_id=ResourceId.HAND,
            hand_model=profile.hand_model,
            hand_side=profile.hand_side,
            semantic_schema_id=profile.semantic_schema_id,
            task_id=None,
            task_version=None,
            policy_package_id=None,
            calibration_id=mapper.calibration.calibration_id,
            control_epoch=1,
            sequence=0,
        )
        authorized_ns = time.monotonic_ns()
        command = AuthorizedHandCommand(
            identity=identity,
            semantic_position=candidate.semantic_position,
            owner=OwnerKind.TELEOP,
            command_id=uuid.uuid4().hex,
            command_mode=CommandMode.SEMANTIC_POSITION,
            authorized_time_ns=authorized_ns,
            deadline_ns=authorized_ns + 500_000_000,
            safety_decision="m1-test-pass",
            calibration_id=mapper.calibration.calibration_id,
            mapping_id=mapper.calibration.artifact_digest,
        )
        acknowledgement = gateway.submit(command).wait(1.0)
        assert acknowledgement.gateway.level is AcknowledgementLevel.SENT_TO_BUS
        assert acknowledgement.effective_target.semantic_position == mapper.prepare(
            candidate.semantic_position
        ).diagnostic_semantic
        assert len(transport.sent_commands) == 1
    finally:
        gateway.stop()
