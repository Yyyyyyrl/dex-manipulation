"""Provider-neutral task-readiness evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReadinessResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    OPERATOR_CONFIRMED = "operator-confirmed"


class RequirementLevel(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class TaskReadinessEvidence:
    provider_id: str
    provider_version: str
    task_id: str
    hand_model: str
    hand_side: str
    generated_time_ns: int
    valid_until_ns: int
    result: ReadinessResult
    measurements: tuple[tuple[str, Any], ...]
    reason_codes: tuple[str, ...]
    confidence: float | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.provider_id or not self.provider_version or not self.task_id:
            raise ValueError("readiness provider and task identity are required")
        if self.generated_time_ns < 0 or self.valid_until_ns <= self.generated_time_ns:
            raise ValueError("readiness evidence must have a future monotonic expiry")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("readiness confidence must be within [0, 1]")

    def valid_at(self, now_ns: int) -> bool:
        return self.generated_time_ns <= now_ns < self.valid_until_ns


@dataclass(frozen=True)
class ReadinessRequirement:
    provider_id: str
    level: RequirementLevel


@dataclass(frozen=True)
class ReadinessPolicy:
    task_id: str
    requirements: tuple[ReadinessRequirement, ...]


@dataclass(frozen=True)
class ReadinessSnapshot:
    task_id: str
    evaluated_time_ns: int
    evidence: tuple[TaskReadinessEvidence, ...]
    ready: bool
    blocking_reasons: tuple[str, ...]
