from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from dex_hardware_linker import LinkerMapper

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_mapping_matches_cross_repository_golden_trace():
    mapper = LinkerMapper.load()
    fixture = json.loads(
        (ROOT / "tests/fixtures/golden/linker_mapping_golden_v1.json").read_text()
    )
    assert mapper.calibration.artifact_digest == fixture["calibration_digest"]
    assert mapper.calibration.semantic_schema_digest == fixture["semantic_schema_digest"]
    for case in fixture["cases"]:
        prepared = mapper.prepare(case["semantic_radians"])
        assert list(prepared.native_range) == case["native_range"], case["name"]
        assert max(abs(value) for value in prepared.round_trip_error) < 0.01


def test_calibration_cannot_be_mutated_after_preflight():
    calibration = LinkerMapper.load().calibration
    with pytest.raises(FrozenInstanceError):
        calibration.hand_joint = "L20"  # type: ignore[misc]


def test_saturation_is_reported_in_preview():
    mapper = LinkerMapper.load()
    target = [0.0] * 16
    target[12] = 10.0
    prepared = mapper.prepare(target)
    assert "thumb_cmc_yaw" in prepared.preview.saturated_joints
