"""Identity, ownership, timing, and acknowledgement contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


PROTOCOL_VERSION = "1.0"


class ResourceId(str, Enum):
    ARM = "arm"
    HAND = "hand"


class OwnerKind(str, Enum):
    NONE = "none"
    SAFETY = "safety"
    TELEOP = "teleoperation"
    ARM_HOLD = "arm-hold"
    TRANSITION = "transition-controller"
    POLICY = "selected-policy"


class CommandMode(str, Enum):
    SEMANTIC_POSITION = "semantic-position"
    ABSOLUTE_JOINT_POSITION = "absolute-joint-position"
    ABSOLUTE_TCP_POSE = "absolute-tcp-pose"
    BOUNDED_CARTESIAN_TWIST = "bounded-cartesian-twist"
    SAFE_HOLD = "safe-hold"


class SourceHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    FAULTED = "faulted"
    DISCONNECTED = "disconnected"


class AcknowledgementLevel(IntEnum):
    NONE = 0
    CANDIDATE_PROPOSED = 1
    COMMAND_AUTHORIZED = 2
    COMMAND_ENCODED = 3
    SENT_TO_BUS = 4
    DEVICE_ACCEPTED = 5
    SERVO_APPLIED = 6


@dataclass(frozen=True)
class MessageIdentity:
    protocol_version: str
    control_session_id: str
    source_id: str
    resource_id: ResourceId
    hand_model: str | None
    hand_side: str | None
    semantic_schema_id: str | None
    task_id: str | None
    task_version: str | None
    policy_package_id: str | None
    calibration_id: str | None
    control_epoch: int
    sequence: int

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported protocol {self.protocol_version!r}; expected {PROTOCOL_VERSION!r}"
            )
        if not self.control_session_id or not self.source_id:
            raise ValueError("control_session_id and source_id are required")
        if self.control_epoch < 0 or self.sequence < 0:
            raise ValueError("control_epoch and sequence must be non-negative")
        if (self.task_id is None) != (self.task_version is None):
            raise ValueError("task ID and task version must be present together")


@dataclass(frozen=True)
class OwnershipState:
    control_session_id: str
    resource_id: ResourceId
    owner: OwnerKind
    control_epoch: int
    command_mode: CommandMode
    start_time_ns: int
    expiry_time_ns: int
    gateway_acknowledged: bool
    watchdog_healthy: bool

    def __post_init__(self) -> None:
        if self.control_epoch < 0:
            raise ValueError("control_epoch must be non-negative")
        if self.start_time_ns < 0 or self.expiry_time_ns <= self.start_time_ns:
            raise ValueError("ownership expiry must be after its monotonic start")

    def valid_at(self, now_ns: int) -> bool:
        return (
            self.start_time_ns <= now_ns < self.expiry_time_ns
            and self.gateway_acknowledged
            and self.watchdog_healthy
        )


@dataclass(frozen=True)
class TimestampedSample:
    payload: Any
    generated_time_ns: int | None
    received_time_ns: int
    sequence: int
    source_health: SourceHealth
    validity_mask: tuple[bool, ...]
    coordinate_frame_id: str
    units: str
    diagnostics: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.generated_time_ns is not None and self.generated_time_ns < 0:
            raise ValueError("generated_time_ns must be non-negative")
        if self.received_time_ns < 0 or self.sequence < 0:
            raise ValueError("received_time_ns and sequence must be non-negative")
        if not self.coordinate_frame_id or not self.units:
            raise ValueError("coordinate frame and units are required")

    def age_ns(self, now_ns: int) -> int:
        return max(0, now_ns - self.received_time_ns)


@dataclass(frozen=True)
class GatewayAcknowledgement:
    identity: MessageIdentity
    command_id: str
    level: AcknowledgementLevel
    acknowledged_time_ns: int
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.command_id or self.acknowledged_time_ns < 0:
            raise ValueError("command_id and a monotonic acknowledgement time are required")
