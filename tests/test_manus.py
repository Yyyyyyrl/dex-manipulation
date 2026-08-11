from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from dex_contracts import SourceHealth
from dex_teleop_adapters import ManusHandSource, ManusRetargeter, TeleopProfile
from dex_teleop_adapters.manus_math import EXPECTED_LAYOUT

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _Position:
    x: float
    y: float
    z: float


@dataclass
class _Pose:
    position: _Position


@dataclass
class _Node:
    node_id: int
    chain_type: str
    joint_type: str
    pose: _Pose


class _Message:
    def __init__(self, side: str = "Left") -> None:
        self.side = side
        self.glove_id = "glove-1"
        self.raw_node_count = 25
        points = []
        for index in range(25):
            finger = max(0, (index - 1) // 5)
            depth = index if index < 5 else (index - 5) % 5
            points.append((0.018 * (finger - 2), 0.018 * depth, 0.002 * finger))
        points[0] = (0.0, 0.0, 0.0)
        points[6] = (-0.03, 0.04, 0.0)
        points[11] = (0.0, 0.05, 0.005)
        self.raw_nodes = [
            _Node(i, EXPECTED_LAYOUT[i][0], EXPECTED_LAYOUT[i][1], _Pose(_Position(*points[i])))
            for i in range(25)
        ]


class _Optimizer:
    target_link_human_indices = np.arange(5)
    retargeting_type = "POSITION"


class _Filter:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1


class _Retargeting:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.joint_names = tuple(reversed(names))
        self.optimizer = _Optimizer()
        self.filter = _Filter()
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1

    def retarget(self, _reference):
        values = {name: float(index) / 10.0 for index, name in enumerate(self.joint_names)}
        return np.array([values[name] for name in self.joint_names])


def _profile() -> TeleopProfile:
    return TeleopProfile.load(
        ROOT / "configs/teleop/linker_g20_left_manus_dexpilot_v1.json",
        ROOT,
    )


def test_manus_source_validates_side_layout_sequence_and_staleness():
    now = {"value": 1_000}
    source = ManusHandSource(
        source_id="manus-left",
        hand_side="left",
        topic="manus_glove_0",
        stale_after_ns=100,
        clock_ns=lambda: now["value"],
    )
    assert source.ingest_message(_Message("Right")) is None
    assert source.status().health is SourceHealth.DISCONNECTED
    first = source.ingest_message(_Message())
    assert first is not None and first.sequence == 0
    now["value"] += 20
    second = source.ingest_message(_Message())
    assert second is not None and second.sequence == 1
    assert source.status().health is SourceHealth.HEALTHY
    now["value"] += 101
    assert source.status().health is SourceHealth.STALE


def test_retargeter_projects_by_name_and_keeps_confirmed_bias_in_profile():
    profile = _profile()
    backend = _Retargeting(profile.semantic_joint_names)
    retargeter = ManusRetargeter(backend, profile, clock_ns=lambda: 2_000)
    retargeter.reset()
    assert backend.reset_count == 1 and backend.filter.reset_count == 1
    source = ManusHandSource(
        source_id="manus-left",
        hand_side="left",
        topic="manus_glove_0",
        stale_after_ns=1_000_000,
        clock_ns=lambda: 1_000,
    )
    sample = source.ingest_message(_Message())
    assert sample is not None
    candidate = retargeter.retarget(
        sample,
        control_session_id="session",
        control_epoch=3,
        task_id="task",
        task_version="1.0",
    )
    by_name = dict(zip(backend.joint_names, backend.retarget(None), strict=False))
    for name, value in zip(profile.semantic_joint_names, candidate.semantic_position, strict=False):
        expected = by_name[name]
        if name == "thumb_cmc_roll":
            expected += profile.thumb_cmc_roll_bias_rad
        assert value == pytest.approx(expected)
    assert profile.thumb_cmc_roll_bias_rad == pytest.approx(-math.pi / 18.0)


def test_teleop_package_has_no_actuator_imports():
    forbidden = ("dex_hardware_linker", "LinkerHand", "python-can", "can.interface")
    for path in (ROOT / "src/dex_teleop_adapters").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{path.name} imports or names actuator dependency {name}"
