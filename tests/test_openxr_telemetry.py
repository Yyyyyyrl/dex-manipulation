from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from dex_contracts import SourceHealth, TimestampedSample
from dex_teleop_adapters import (
    OPENXR_JOINT_NAMES,
    OPENXR_LAYOUT_ID,
    OpenXRKeypoints,
    OpenXRRetargeter,
    TeleopProfile,
    needed_openxr_joints_valid,
    openxr_to_joint_pos,
)
from tools.control_console.openxr_source import UdpOpenXRSource
from tools.openxr_hand_bridge import _fake_pose, _joint_records

ROOT = Path(__file__).resolve().parents[1]


def _payload(sequence: int = 0) -> dict[str, object]:
    positions, orientations, radii, valid = _fake_pose(0.4)
    return {
        "schema_version": 1,
        "source": "dex-teleop-openxr-hand",
        "mode": "fake",
        "device": "SYNTHETIC QUEST 3S",
        "runtime": "SYNTHETIC WIVRN",
        "session_running": True,
        "session_focused": True,
        "side": "left",
        "layout": OPENXR_LAYOUT_ID,
        "joint_count": 26,
        "source_sequence": sequence,
        "sample_monotonic_ns": 900_000_000 + sequence,
        "valid_mask": [True] * 26,
        "pinch_m": 0.031,
        "joints": _joint_records(positions, orientations, radii, valid),
    }


def _source(clock: list[int]) -> UdpOpenXRSource:
    return UdpOpenXRSource(
        "127.0.0.1",
        0,
        source_id="openxr-left-test",
        hand_side="left",
        stale_after_ns=100_000_000,
        clock_ns=lambda: clock[0],
    )


def test_openxr_source_validates_exact_layout_and_exposes_same_control_frame() -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    received: list[TimestampedSample] = []
    source.start(received.append)
    try:
        sample = source.ingest_payload(_payload(), received_time_ns=clock[0])
        assert sample is received[0]
        assert isinstance(sample.payload, OpenXRKeypoints)
        assert len(sample.payload.points_m) == 26
        assert sample.coordinate_frame_id == "openxr-view-right-handed"
        assert source.status().health is SourceHealth.HEALTHY

        snapshot = source.snapshot()
        assert snapshot["layout"] == OPENXR_LAYOUT_ID
        assert snapshot["joint_count"] == 26
        assert snapshot["valid_joint_count"] == 26
        assert snapshot["device"] == "SYNTHETIC QUEST 3S"
        assert snapshot["control_correlated"] is False
        assert snapshot["nodes"][25]["name"] == OPENXR_JOINT_NAMES[25]
    finally:
        source.stop()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update(session_focused=False), "session-not-focused"),
        (lambda value: value.update(side="right"), "side-mismatch"),
        (lambda value: value.update(layout="wrong"), "layout-mismatch"),
        (lambda value: value["joints"].pop(), "joint-count-mismatch"),
        (lambda value: value["joints"][7].update(parent=0), "parent-layout-mismatch"),
        (lambda value: value["valid_mask"].__setitem__(10, False), "required-joint-invalid"),
        (lambda value: value["joints"][4].update(qw=float("nan")), "must be finite"),
    ],
)
def test_openxr_source_rejects_unusable_control_frames(mutation, reason) -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    try:
        assert source.ingest_payload(_payload(), received_time_ns=clock[0]) is not None
        invalid = deepcopy(_payload(1))
        mutation(invalid)
        assert source.ingest_payload(invalid, received_time_ns=clock[0] + 1) is None
        assert source.status(clock[0] + 2).health is SourceHealth.DEGRADED
        assert reason in source.status(clock[0] + 2).reason
        assert source.snapshot()["source_sequence"] == 0
    finally:
        source.stop()


def test_openxr_source_counts_drops_rejects_reorder_and_becomes_stale() -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    try:
        assert source.ingest_payload(_payload(2), received_time_ns=clock[0]) is not None
        clock[0] += 20_000_000
        assert source.ingest_payload(_payload(5), received_time_ns=clock[0]) is not None
        assert source.snapshot()["dropped_since_last"] == 2
        assert source.ingest_payload(_payload(4), received_time_ns=clock[0] + 1) is None
        clock[0] += 100_000_001
        assert source.status().health is SourceHealth.STALE
    finally:
        source.stop()


def test_openxr_to_mano_contract_is_finite_and_wrist_relative() -> None:
    positions, _, _, valid = _fake_pose(0.2)
    converted = openxr_to_joint_pos(positions, "left")
    assert converted.shape == (21, 3)
    assert np.isfinite(converted).all()
    assert np.allclose(converted[0], 0.0)
    assert needed_openxr_joints_valid(valid.tolist())
    valid[10] = False
    assert not needed_openxr_joints_valid(valid.tolist())


class _Optimizer:
    target_link_human_indices = np.arange(5)
    retargeting_type = "POSITION"


class _Retargeting:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.joint_names = tuple(reversed(names))
        self.optimizer = _Optimizer()
        self.filter = None

    def reset(self) -> None:
        pass

    def retarget(self, _reference) -> np.ndarray:
        return np.asarray([0.25 if name == "thumb_cmc_roll" else 0.0 for name in self.joint_names])


def test_openxr_frame_retargets_to_the_existing_g20_semantic_contract() -> None:
    profile = TeleopProfile.load(
        ROOT / "configs/teleop/linker_g20_left_openxr_dexpilot_v1.json", ROOT
    )
    positions, orientations, radii, valid = _fake_pose(0.3)
    now_ns = time.monotonic_ns()
    sample = TimestampedSample(
        payload=OpenXRKeypoints(
            source_id="openxr-left",
            hand_side="left",
            points_m=tuple(map(tuple, positions)),
            orientations_xyzw=tuple(map(tuple, orientations)),
            radii_m=tuple(radii),
        ),
        generated_time_ns=now_ns,
        received_time_ns=now_ns,
        sequence=8,
        source_health=SourceHealth.HEALTHY,
        validity_mask=tuple(bool(value) for value in valid),
        coordinate_frame_id="openxr-view-right-handed",
        units="meter",
    )
    candidate = OpenXRRetargeter(_Retargeting(profile.semantic_joint_names), profile).retarget(
        sample,
        control_session_id="openxr-test-session",
        control_epoch=3,
        task_id=None,
        task_version=None,
    )
    assert len(candidate.semantic_position) == 16
    thumb = profile.semantic_joint_names.index("thumb_cmc_roll")
    assert candidate.semantic_position[thumb] == pytest.approx(
        0.25 + profile.thumb_cmc_roll_bias_rad
    )
    assert candidate.source_state_sequence == 8
