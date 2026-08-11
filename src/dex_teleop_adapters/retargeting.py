"""Manus-to-semantic retargeting with name projection and explicit profile bias."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from dex_contracts import (
    PROTOCOL_VERSION,
    MessageIdentity,
    ResourceId,
    SourceHealth,
    TeleopHandCandidate,
    TimestampedSample,
)

from .manus import ManusKeypoints
from .manus_math import compute_ref_value, manus_to_joint_pos
from .profiles import TeleopProfile


@dataclass(frozen=True)
class RetargeterStatus:
    profile_id: str
    reset_count: int
    output_sequence: int
    last_solver_time_ns: int | None
    last_error: str


class ManusRetargeter:
    def __init__(
        self,
        retargeting: Any,
        profile: TeleopProfile,
        *,
        source_id: str = "manus-retargeter",
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
            raise ValueError(f"Manus source is not healthy: {sample.source_health.value}")
        if not isinstance(sample.payload, ManusKeypoints):
            raise TypeError("ManusRetargeter needs a ManusKeypoints payload")
        if sample.payload.hand_side != self.profile.hand_side:
            raise ValueError("Manus sample side does not match TeleopProfile")
        if not all(sample.validity_mask):
            raise ValueError("Manus sample validity mask contains missing nodes")

        points = np.asarray(sample.payload.points_m, dtype=np.float64)
        joint_pos = manus_to_joint_pos(points, hand_type=self.profile.hand_side)
        reference = compute_ref_value(self.retargeting, joint_pos)
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
        by_name = dict(zip(self._retarget_joint_names, (float(value) for value in qpos), strict=False))
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
                ("thumb_cmc_roll_bias_rad", self.profile.thumb_cmc_roll_bias_rad),
            ),
            confidence=1.0,
        )


def build_dexpilot_retargeter(
    profile: TeleopProfile,
    *,
    model_directory: str | Path,
    candidate_ttl_ns: int,
    source_id: str = "manus-retargeter",
) -> ManusRetargeter:
    """Build the pinned dex-retargeting backend lazily, without ROS or hardware."""

    from dex_retargeting.retargeting_config import RetargetingConfig

    RetargetingConfig.set_default_urdf_dir(str(Path(model_directory)))
    backend = RetargetingConfig.load_from_file(profile.retargeting_config).build()
    return ManusRetargeter(
        backend,
        profile,
        source_id=source_id,
        candidate_ttl_ns=candidate_ttl_ns,
    )
