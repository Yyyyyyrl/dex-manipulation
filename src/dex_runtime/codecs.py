"""Pure proprioception codecs shared by training, export, and deployment tests.

This module has no Isaac, rl-games, ROS, or hardware dependency.  The runtime
repository carries an independently packaged copy and both implementations are
required to pass the same golden fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import torch


IDENTITY_RADIANS = "identity-radians"
AFFINE_TO_MINUS_ONE_ONE = "affine-limits-to-minus-one-one"
COLLECT_FRESH_HISTORY = "collect-fresh-effective-targets"


@dataclass(frozen=True)
class ProprioCodecSpec:
    codec_id: str
    task_family: str
    joint_count: int
    frame_dim: int
    history_length: int
    actor_frame_count: int
    control_period_ns: int
    measured_position_scaling: str
    measured_lower_rad: tuple[float, ...] | None
    measured_upper_rad: tuple[float, ...] | None
    history_reset_semantics: str = COLLECT_FRESH_HISTORY

    def __post_init__(self) -> None:
        if not self.codec_id or not self.task_family:
            raise ValueError("codec ID and task family are required")
        if self.joint_count <= 0 or self.frame_dim != 2 * self.joint_count:
            raise ValueError("codec frame_dim must equal measured plus effective target widths")
        if not 1 <= self.actor_frame_count <= self.history_length:
            raise ValueError("actor frame count must fit within history")
        if self.control_period_ns <= 0:
            raise ValueError("control period must be positive")
        if self.history_reset_semantics != COLLECT_FRESH_HISTORY:
            raise ValueError("the adopted runtime requires fresh effective-target history")
        if self.measured_position_scaling == IDENTITY_RADIANS:
            if self.measured_lower_rad is not None or self.measured_upper_rad is not None:
                raise ValueError("identity scaling must not declare affine limits")
        elif self.measured_position_scaling == AFFINE_TO_MINUS_ONE_ONE:
            if self.measured_lower_rad is None or self.measured_upper_rad is None:
                raise ValueError("affine scaling requires lower and upper limits")
            if len(self.measured_lower_rad) != self.joint_count or len(self.measured_upper_rad) != self.joint_count:
                raise ValueError("affine limit widths must match joint_count")
            if any(upper <= lower for lower, upper in zip(self.measured_lower_rad, self.measured_upper_rad)):
                raise ValueError("every affine upper limit must exceed its lower limit")
        else:
            raise ValueError(f"unsupported measured-position scaling {self.measured_position_scaling!r}")

    def as_dict(self) -> dict[str, object]:
        return {
            "codec_id": self.codec_id,
            "task_family": self.task_family,
            "joint_count": self.joint_count,
            "frame_dim": self.frame_dim,
            "history_length": self.history_length,
            "actor_frame_count": self.actor_frame_count,
            "control_period_ns": self.control_period_ns,
            "measured_position_scaling": self.measured_position_scaling,
            "measured_lower_rad": None if self.measured_lower_rad is None else list(self.measured_lower_rad),
            "measured_upper_rad": None if self.measured_upper_rad is None else list(self.measured_upper_rad),
            "history_reset_semantics": self.history_reset_semantics,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ProprioCodecSpec":
        required = {
            "codec_id",
            "task_family",
            "joint_count",
            "frame_dim",
            "history_length",
            "actor_frame_count",
            "control_period_ns",
            "measured_position_scaling",
            "measured_lower_rad",
            "measured_upper_rad",
            "history_reset_semantics",
        }
        if set(value) != required:
            missing = sorted(required - set(value))
            extra = sorted(set(value) - required)
            raise ValueError(f"invalid codec fields; missing={missing}, extra={extra}")

        def _optional_tuple(name: str) -> tuple[float, ...] | None:
            raw = value[name]
            if raw is None:
                return None
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"{name} must be a vector or null")
            return tuple(float(item) for item in raw)

        return cls(
            codec_id=str(value["codec_id"]),
            task_family=str(value["task_family"]),
            joint_count=int(value["joint_count"]),
            frame_dim=int(value["frame_dim"]),
            history_length=int(value["history_length"]),
            actor_frame_count=int(value["actor_frame_count"]),
            control_period_ns=int(value["control_period_ns"]),
            measured_position_scaling=str(value["measured_position_scaling"]),
            measured_lower_rad=_optional_tuple("measured_lower_rad"),
            measured_upper_rad=_optional_tuple("measured_upper_rad"),
            history_reset_semantics=str(value["history_reset_semantics"]),
        )


class ProprioCodec:
    def __init__(self, spec: ProprioCodecSpec) -> None:
        self.spec = spec

    def _vector(self, value: torch.Tensor | Sequence[float], label: str) -> torch.Tensor:
        tensor = torch.as_tensor(value)
        if tensor.ndim == 0 or tensor.shape[-1] != self.spec.joint_count:
            raise ValueError(f"{label} must end with {self.spec.joint_count} joints")
        if not tensor.is_floating_point():
            tensor = tensor.to(dtype=torch.float32)
        if not bool(torch.isfinite(tensor).all()):
            raise ValueError(f"{label} contains non-finite values")
        return tensor

    def encode_frame(
        self,
        measured_position_rad: torch.Tensor | Sequence[float],
        effective_target_rad: torch.Tensor | Sequence[float],
    ) -> torch.Tensor:
        measured = self._vector(measured_position_rad, "measured position")
        target = self._vector(effective_target_rad, "effective target").to(
            device=measured.device, dtype=measured.dtype
        )
        if measured.shape != target.shape:
            raise ValueError("measured position and effective target shapes must match")
        if self.spec.measured_position_scaling == AFFINE_TO_MINUS_ONE_ONE:
            lower = torch.as_tensor(
                self.spec.measured_lower_rad, device=measured.device, dtype=measured.dtype
            )
            upper = torch.as_tensor(
                self.spec.measured_upper_rad, device=measured.device, dtype=measured.dtype
            )
            measured = 2.0 * (measured - lower) / (upper - lower) - 1.0
        return torch.cat((measured, target), dim=-1)

    def empty_history(
        self,
        batch_shape: Sequence[int] = (),
        *,
        device: torch.device | str | None = None,
        dtype: torch.dtype = torch.float32,
    ) -> torch.Tensor:
        return torch.zeros(
            (*batch_shape, self.spec.history_length, self.spec.frame_dim),
            device=device,
            dtype=dtype,
        )

    def append(self, history: torch.Tensor, frame: torch.Tensor) -> torch.Tensor:
        expected = (*history.shape[:-2], self.spec.frame_dim)
        if history.shape[-2:] != (self.spec.history_length, self.spec.frame_dim):
            raise ValueError("history shape does not match codec")
        if frame.shape != expected:
            raise ValueError(f"frame shape {tuple(frame.shape)} does not match {expected}")
        return torch.cat((history[..., 1:, :], frame.unsqueeze(-2)), dim=-2)

    def assemble_actor_input(self, history: torch.Tensor) -> torch.Tensor:
        if history.shape[-2:] != (self.spec.history_length, self.spec.frame_dim):
            raise ValueError("history shape does not match codec")
        selected = history[..., -self.spec.actor_frame_count :, :]
        return selected.reshape(*selected.shape[:-2], -1)


def mounted_linker_g20_codec_spec(history_length: int = 30) -> ProprioCodecSpec:
    return ProprioCodecSpec(
        codec_id="linker-g20-mounted-proprio-v1",
        task_family="mounted-screwdriver-rotation",
        joint_count=16,
        frame_dim=32,
        history_length=history_length,
        actor_frame_count=1,
        control_period_ns=100_000_000,
        measured_position_scaling=IDENTITY_RADIANS,
        measured_lower_rad=None,
        measured_upper_rad=None,
    )


def inhand_linker_g20_codec_spec(
    measured_lower_rad: Sequence[float],
    measured_upper_rad: Sequence[float],
    history_length: int = 30,
) -> ProprioCodecSpec:
    return ProprioCodecSpec(
        codec_id="linker-g20-free-object-proprio-v1",
        task_family="free-object-rotation",
        joint_count=16,
        frame_dim=32,
        history_length=history_length,
        actor_frame_count=3,
        control_period_ns=50_000_000,
        measured_position_scaling=AFFINE_TO_MINUS_ONE_ONE,
        measured_lower_rad=tuple(float(value) for value in measured_lower_rad),
        measured_upper_rad=tuple(float(value) for value in measured_upper_rad),
    )
