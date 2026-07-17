"""Policy package descriptor and compatibility contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyDescriptor:
    package_id: str
    package_digest: str
    display_name: str
    task_id: str
    task_version: str
    hand_model: str
    hand_side: str
    semantic_schema_id: str
    semantic_schema_digest: str
    calibration_compatibility: tuple[str, ...]
    control_period_ns: int
    codec_id: str
    runtime_api_min: str
    runtime_api_max: str
    promotion_status: str
    evaluation_summary: tuple[tuple[str, float | int | str], ...]
    readiness_provider_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        required = (
            self.package_id,
            self.package_digest,
            self.task_id,
            self.hand_model,
            self.hand_side,
            self.semantic_schema_id,
            self.semantic_schema_digest,
            self.codec_id,
        )
        if any(not value for value in required):
            raise ValueError("policy descriptor safety-critical identity is incomplete")
        if self.control_period_ns <= 0:
            raise ValueError("policy control period must be positive")


@dataclass(frozen=True)
class PolicyCompatibility:
    compatible: bool
    reason_codes: tuple[str, ...]
