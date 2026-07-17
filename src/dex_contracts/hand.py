"""Semantic hand state, candidate, command, and acknowledgement contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .identity import (
    AcknowledgementLevel,
    CommandMode,
    GatewayAcknowledgement,
    MessageIdentity,
    OwnerKind,
    ResourceId,
)


def _finite_vector(values: tuple[float, ...], label: str) -> None:
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label} must be a non-empty finite vector")


@dataclass(frozen=True)
class EffectiveHandTarget:
    semantic_position: tuple[float, ...]
    command_id: str
    evidence_level: AcknowledgementLevel
    evidence_time_ns: int

    def __post_init__(self) -> None:
        _finite_vector(self.semantic_position, "effective semantic target")
        if not self.command_id or self.evidence_time_ns < 0:
            raise ValueError("effective target needs command identity and monotonic evidence time")


@dataclass(frozen=True)
class HandState:
    identity: MessageIdentity
    semantic_position: tuple[float, ...]
    semantic_velocity: tuple[float, ...] | None
    semantic_effort: tuple[float, ...] | None
    acquisition_time_ns: int
    raw_native_state_ref: str | None
    state_quality: str
    missing_joint_mask: tuple[bool, ...]
    hardware_faults: tuple[str, ...]
    temperatures_c: tuple[float, ...] | None
    last_effective_target: EffectiveHandTarget | None
    acknowledgement_capability: AcknowledgementLevel

    def __post_init__(self) -> None:
        if self.identity.resource_id is not ResourceId.HAND:
            raise ValueError("HandState identity must name the hand resource")
        _finite_vector(self.semantic_position, "semantic_position")
        if self.acquisition_time_ns < 0:
            raise ValueError("acquisition_time_ns must be monotonic and non-negative")
        if len(self.missing_joint_mask) != len(self.semantic_position):
            raise ValueError("missing_joint_mask length must match semantic_position")
        for label, vector in (
            ("semantic_velocity", self.semantic_velocity),
            ("semantic_effort", self.semantic_effort),
            ("temperatures_c", self.temperatures_c),
        ):
            if vector is not None and len(vector) != len(self.semantic_position):
                raise ValueError(f"{label} length must match semantic_position")


@dataclass(frozen=True)
class HandCandidate:
    identity: MessageIdentity
    semantic_position: tuple[float, ...]
    generated_time_ns: int
    valid_until_ns: int
    source_state_sequence: int
    diagnostics: tuple[tuple[str, float | int | str | bool], ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.identity.resource_id is not ResourceId.HAND:
            raise ValueError("hand candidate identity must name the hand resource")
        _finite_vector(self.semantic_position, "candidate semantic_position")
        if self.generated_time_ns < 0 or self.valid_until_ns <= self.generated_time_ns:
            raise ValueError("candidate validity must end after generation")
        if self.source_state_sequence < 0:
            raise ValueError("source_state_sequence must be non-negative")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")

    def valid_at(self, now_ns: int) -> bool:
        return self.generated_time_ns <= now_ns < self.valid_until_ns


@dataclass(frozen=True)
class TeleopHandCandidate(HandCandidate):
    pass


@dataclass(frozen=True)
class PolicyHandCandidate(HandCandidate):
    pass


@dataclass(frozen=True)
class AuthorizedHandCommand:
    identity: MessageIdentity
    semantic_position: tuple[float, ...]
    owner: OwnerKind
    command_id: str
    command_mode: CommandMode
    authorized_time_ns: int
    deadline_ns: int
    safety_decision: str
    calibration_id: str
    mapping_id: str

    def __post_init__(self) -> None:
        if self.identity.resource_id is not ResourceId.HAND:
            raise ValueError("authorized command identity must name the hand resource")
        if self.command_mode is not CommandMode.SEMANTIC_POSITION:
            raise ValueError("initial Linker gateway accepts semantic position only")
        _finite_vector(self.semantic_position, "authorized semantic_position")
        if not self.command_id or self.deadline_ns <= self.authorized_time_ns:
            raise ValueError("authorized command needs an ID and future deadline")
        if not self.calibration_id or not self.mapping_id:
            raise ValueError("calibration and mapping IDs are required")


@dataclass(frozen=True)
class HandCommandAcknowledgement:
    gateway: GatewayAcknowledgement
    effective_target: EffectiveHandTarget
