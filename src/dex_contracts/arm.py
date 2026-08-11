"""Capability-driven arm contracts; no vendor behavior is encoded here."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .identity import CommandMode, MessageIdentity, ResourceId


class ArmTargetKind(str, Enum):
    ABSOLUTE_JOINT_POSITION = "absolute-joint-position"
    ABSOLUTE_TCP_POSE = "absolute-tcp-pose"
    BOUNDED_CARTESIAN_TWIST = "bounded-cartesian-twist"


@dataclass(frozen=True)
class ArmCapabilities:
    adapter_id: str
    controller_id: str
    supported_modes: tuple[CommandMode, ...]
    state_fields: tuple[str, ...]
    minimum_command_hz: float
    maximum_command_hz: float
    atomic_mode_switch: bool
    controller_side_hold: bool
    watchdog_supported: bool
    keepalive_supported: bool
    acknowledgement_levels: tuple[str, ...]
    joint_limits: tuple[tuple[float, float], ...]
    cartesian_limits: tuple[tuple[float, float], ...]
    fault_visibility: tuple[str, ...]
    shutdown_semantics: str

    def __post_init__(self) -> None:
        if not self.adapter_id or not self.controller_id:
            raise ValueError("arm adapter and controller identity are required")
        if self.minimum_command_hz <= 0 or self.maximum_command_hz < self.minimum_command_hz:
            raise ValueError("arm command-rate capability is invalid")


@dataclass(frozen=True)
class ArmState:
    identity: MessageIdentity
    acquisition_time_ns: int
    joint_position: tuple[float, ...] | None
    joint_velocity: tuple[float, ...] | None
    tcp_pose: tuple[float, ...] | None
    tcp_twist: tuple[float, ...] | None
    controller_mode: CommandMode
    following_error: float | None
    wrench: tuple[float, ...] | None
    faults: tuple[str, ...]
    heartbeat_age_ns: int

    def __post_init__(self) -> None:
        if self.identity.resource_id is not ResourceId.ARM:
            raise ValueError("ArmState identity must name the arm resource")
        if self.acquisition_time_ns < 0 or self.heartbeat_age_ns < 0:
            raise ValueError("arm times must be monotonic and non-negative")


@dataclass(frozen=True)
class ArmTarget:
    identity: MessageIdentity
    kind: ArmTargetKind
    values: tuple[float, ...]
    coordinate_frame_id: str
    generated_time_ns: int
    valid_until_ns: int

    def __post_init__(self) -> None:
        if self.identity.resource_id is not ResourceId.ARM:
            raise ValueError("ArmTarget identity must name the arm resource")
        if not self.values or any(not math.isfinite(value) for value in self.values):
            raise ValueError("arm target values must be finite")
        if not self.coordinate_frame_id:
            raise ValueError("arm target coordinate frame is required")
        if self.valid_until_ns <= self.generated_time_ns:
            raise ValueError("arm target deadline must follow generation")
