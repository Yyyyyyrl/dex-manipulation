"""OpenXR hand tracking contracts and DexPilot retargeting.

The live console receives the exact 26-joint ``XR_EXT_hand_tracking`` layout
used by ``dex_teleop.vr_utils.vr_hand_reader.VRHandReader``.  This module is
deliberately free of OpenXR and robot SDK imports: the OpenXR runtime stays in
its isolated producer process while the existing LinkerGateway remains the
only CAN owner.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Any, Callable

import numpy as np

from dex_contracts import (
    MessageIdentity,
    PROTOCOL_VERSION,
    ResourceId,
    SourceHealth,
    TeleopHandCandidate,
    TimestampedSample,
)

from .hand_frame import (
    OPERATOR2MANO_LEFT,
    OPERATOR2MANO_RIGHT,
    estimate_frame_from_hand_points,
)
from .profiles import TeleopProfile
from .retargeting import RetargeterStatus


OPENXR_LAYOUT_ID = "openxr-hand-26-v1"
OPENXR_JOINT_NAMES = (
    "palm",
    "wrist",
    "thumb_metacarpal",
    "thumb_proximal",
    "thumb_distal",
    "thumb_tip",
    "index_metacarpal",
    "index_proximal",
    "index_intermediate",
    "index_distal",
    "index_tip",
    "middle_metacarpal",
    "middle_proximal",
    "middle_intermediate",
    "middle_distal",
    "middle_tip",
    "ring_metacarpal",
    "ring_proximal",
    "ring_intermediate",
    "ring_distal",
    "ring_tip",
    "little_metacarpal",
    "little_proximal",
    "little_intermediate",
    "little_distal",
    "little_tip",
)
OPENXR_PARENT_IDS = (
    1, -1,
    1, 2, 3, 4,
    1, 6, 7, 8, 9,
    1, 11, 12, 13, 14,
    1, 16, 17, 18, 19,
    1, 21, 22, 23, 24,
)

# OpenXR 26 layout -> the MediaPipe/MANO 21 layout expected by DexPilot.
OPENXR_TO_MANO = {
    1: 0,    # wrist
    4: 3,    # thumb distal
    5: 4,    # thumb tip
    7: 5,    # index proximal / MCP
    10: 8,   # index tip
    12: 9,   # middle proximal / MCP
    15: 12,  # middle tip
    20: 16,  # ring tip
    25: 20,  # little tip
}
NEEDED_OPENXR_INDICES = tuple(sorted(OPENXR_TO_MANO))


@dataclass(frozen=True)
class OpenXRKeypoints:
    source_id: str
    hand_side: str
    points_m: tuple[tuple[float, float, float], ...]
    orientations_xyzw: tuple[tuple[float, float, float, float], ...]
    radii_m: tuple[float, ...]
    device: str = "Quest 3S"
    runtime: str = "WiVRn"
    layout_id: str = OPENXR_LAYOUT_ID
    pinch_m: float | None = None


@dataclass(frozen=True)
class OpenXRSourceStatus:
    source_id: str
    health: SourceHealth
    sequence: int
    last_receive_time_ns: int | None
    reason: str


def needed_openxr_joints_valid(validity_mask: tuple[bool, ...] | list[bool]) -> bool:
    if len(validity_mask) != len(OPENXR_JOINT_NAMES):
        return False
    return all(validity_mask[index] for index in NEEDED_OPENXR_INDICES)


def openxr_to_joint_pos(points_m: np.ndarray, hand_type: str = "left") -> np.ndarray:
    """Convert the OpenXR 26-joint layout to the MANO-normalized 21 layout."""

    points = np.asarray(points_m, dtype=np.float64)
    if points.shape != (len(OPENXR_JOINT_NAMES), 3) or not np.isfinite(points).all():
        raise ValueError("OpenXR points must be a finite (26, 3) array")
    if hand_type not in ("left", "right"):
        raise ValueError("OpenXR hand type must be left or right")
    keypoint = np.zeros((21, 3), dtype=np.float64)
    for openxr_index, mano_index in OPENXR_TO_MANO.items():
        keypoint[mano_index] = points[openxr_index]
    keypoint -= keypoint[0:1, :]
    operator_to_mano = (
        OPERATOR2MANO_LEFT if hand_type == "left" else OPERATOR2MANO_RIGHT
    )
    wrist_rotation = estimate_frame_from_hand_points(keypoint)
    return keypoint @ wrist_rotation @ operator_to_mano


def _compute_reference(retargeting: Any, joint_pos: np.ndarray) -> np.ndarray:
    indices = retargeting.optimizer.target_link_human_indices
    if retargeting.optimizer.retargeting_type == "POSITION":
        return joint_pos[indices, :]
    origin_indices = indices[0, :]
    task_indices = indices[1, :]
    return joint_pos[task_indices, :] - joint_pos[origin_indices, :]


class OpenXRRetargeter:
    """Retarget one validated OpenXR frame into the G20 semantic joint order."""

    def __init__(
        self,
        retargeting: Any,
        profile: TeleopProfile,
        *,
        source_id: str = "openxr-retargeter",
        candidate_ttl_ns: int = 100_000_000,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if candidate_ttl_ns <= 0:
            raise ValueError("candidate_ttl_ns must be positive")
        self.retargeting = retargeting
        self.profile = profile
        self.source_id = source_id
        self.candidate_ttl_ns = candidate_ttl_ns
        self._clock_ns = clock_ns
        self._sequence = 0
        self._reset_count = 0
        self._last_solver_ns: int | None = None
        self._last_error = ""
        names = tuple(str(name) for name in retargeting.joint_names)
        missing = [name for name in profile.semantic_joint_names if name not in names]
        if missing:
            raise ValueError(f"retargeter model is missing semantic joints: {missing}")
        self._retarget_joint_names = names

    def reset(self) -> None:
        self.retargeting.reset()
        filter_object = getattr(self.retargeting, "filter", None)
        if filter_object is not None:
            filter_object.reset()
        self._reset_count += 1
        self._last_error = ""

    def status(self) -> RetargeterStatus:
        return RetargeterStatus(
            self.profile.profile_id,
            self._reset_count,
            self._sequence,
            self._last_solver_ns,
            self._last_error,
        )

    def retarget(
        self,
        sample: TimestampedSample,
        *,
        control_session_id: str,
        control_epoch: int,
        task_id: str | None,
        task_version: str | None,
    ) -> TeleopHandCandidate:
        if sample.source_health is not SourceHealth.HEALTHY:
            raise ValueError(f"OpenXR source is not healthy: {sample.source_health.value}")
        if not isinstance(sample.payload, OpenXRKeypoints):
            raise TypeError("OpenXRRetargeter needs an OpenXRKeypoints payload")
        if sample.payload.hand_side != self.profile.hand_side:
            raise ValueError("OpenXR sample side does not match TeleopProfile")
        if not needed_openxr_joints_valid(sample.validity_mask):
            raise ValueError("OpenXR sample is missing a required retargeting joint")

        joint_pos = openxr_to_joint_pos(
            np.asarray(sample.payload.points_m, dtype=np.float64),
            hand_type=self.profile.hand_side,
        )
        reference = _compute_reference(self.retargeting, joint_pos)
        start = self._clock_ns()
        try:
            qpos = np.asarray(self.retargeting.retarget(reference), dtype=np.float64)
        except Exception as exc:
            self._last_error = f"solver-fault:{type(exc).__name__}:{exc}"
            raise
        end = self._clock_ns()
        self._last_solver_ns = max(0, end - start)
        if qpos.shape != (len(self._retarget_joint_names),):
            raise ValueError(
                f"retargeter output shape {qpos.shape} does not match joint-name count"
            )
        by_name = dict(zip(self._retarget_joint_names, (float(value) for value in qpos)))
        semantic = [by_name[name] for name in self.profile.semantic_joint_names]
        thumb_index = self.profile.semantic_joint_names.index("thumb_cmc_roll")
        semantic[thumb_index] += self.profile.thumb_cmc_roll_bias_rad
        if any(not math.isfinite(value) for value in semantic):
            raise ValueError("retargeter produced a non-finite semantic target")

        identity = MessageIdentity(
            protocol_version=PROTOCOL_VERSION,
            control_session_id=control_session_id,
            source_id=self.source_id,
            resource_id=ResourceId.HAND,
            hand_model=self.profile.hand_model,
            hand_side=self.profile.hand_side,
            semantic_schema_id=self.profile.semantic_schema_id,
            task_id=task_id,
            task_version=task_version,
            policy_package_id=None,
            calibration_id=None,
            control_epoch=control_epoch,
            sequence=self._sequence,
        )
        self._sequence += 1
        generated = sample.received_time_ns
        return TeleopHandCandidate(
            identity=identity,
            semantic_position=tuple(semantic),
            generated_time_ns=generated,
            valid_until_ns=generated + self.candidate_ttl_ns,
            source_state_sequence=sample.sequence,
            diagnostics=(
                ("solver_time_ns", self._last_solver_ns),
                ("profile_id", self.profile.profile_id),
                ("input_layout", OPENXR_LAYOUT_ID),
                ("thumb_cmc_roll_bias_rad", self.profile.thumb_cmc_roll_bias_rad),
            ),
            confidence=1.0,
        )


def build_openxr_dexpilot_retargeter(
    profile: TeleopProfile,
    *,
    model_directory: str | Path,
    candidate_ttl_ns: int,
    source_id: str = "openxr-retargeter",
) -> OpenXRRetargeter:
    """Build the pinned backend lazily without importing an OpenXR runtime."""

    from dex_retargeting.retargeting_config import RetargetingConfig

    RetargetingConfig.set_default_urdf_dir(str(Path(model_directory)))
    backend = RetargetingConfig.load_from_file(profile.retargeting_config).build()
    return OpenXRRetargeter(
        backend,
        profile,
        source_id=source_id,
        candidate_ttl_ns=candidate_ttl_ns,
    )
