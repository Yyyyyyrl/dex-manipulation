"""Versioned teleoperation behavior profiles."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _digest(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("profile_digest", None)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TeleopProfile:
    profile_id: str
    profile_version: int
    profile_digest: str
    hand_model: str
    hand_side: str
    semantic_schema_id: str
    semantic_schema_digest: str
    semantic_joint_names: tuple[str, ...]
    retargeting_config: str
    retargeting_config_sha256: str
    low_pass_alpha: float
    thumb_cmc_roll_bias_rad: float
    source_coordinate_conversion: str
    filter_reset: str

    @classmethod
    def load(cls, path: str | Path, repository_root: str | Path) -> TeleopProfile:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("TeleopProfile must be a JSON object")
        actual = _digest(raw)
        if raw.get("profile_digest") != actual:
            raise ValueError(
                f"TeleopProfile digest mismatch: declared {raw.get('profile_digest')}, actual {actual}"
            )
        names = raw.get("semantic_joint_names")
        if not isinstance(names, list) or len(names) != 16 or len(set(names)) != 16:
            raise ValueError("TeleopProfile needs 16 unique semantic joint names")
        alpha = float(raw["low_pass_alpha"])
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("low_pass_alpha must be within [0, 1]")
        bias = float(raw["thumb_cmc_roll_bias_rad"])
        if not math.isfinite(bias):
            raise ValueError("thumb bias must be finite")
        config = Path(repository_root) / str(raw["retargeting_config"])
        if not config.is_file():
            raise ValueError(f"retargeting config is missing: {config}")
        config_digest = hashlib.sha256(config.read_bytes()).hexdigest()
        if config_digest != raw["retargeting_config_sha256"]:
            raise ValueError("retargeting configuration digest mismatch")
        if raw.get("filter_reset") != "session-start-and-tracking-recovery":
            raise ValueError("unsupported filter reset behavior")
        return cls(
            profile_id=str(raw["profile_id"]),
            profile_version=int(raw["profile_version"]),
            profile_digest=str(raw["profile_digest"]),
            hand_model=str(raw["hand_model"]),
            hand_side=str(raw["hand_side"]),
            semantic_schema_id=str(raw["semantic_schema_id"]),
            semantic_schema_digest=str(raw["semantic_schema_digest"]),
            semantic_joint_names=tuple(str(name) for name in names),
            retargeting_config=str(config),
            retargeting_config_sha256=str(raw["retargeting_config_sha256"]),
            low_pass_alpha=alpha,
            thumb_cmc_roll_bias_rad=bias,
            source_coordinate_conversion=str(raw["source_coordinate_conversion"]),
            filter_reset=str(raw["filter_reset"]),
        )
