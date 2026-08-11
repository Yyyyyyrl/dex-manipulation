from __future__ import annotations

from copy import deepcopy
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from dex_contracts import SourceHealth
from dex_teleop_adapters import ManusKeypoints
from tools.control_console.manus_source import LAYOUT_ID, PARENT_IDS, UdpManusSource
from tools.manus_glove_bridge import ManusDatagramPublisher, _ros_node_record


def _payload(sequence: int = 0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "test-manus-bridge",
        "mode": "fake",
        "control_correlated": False,
        "glove_id": 0,
        "side": "left",
        "layout": LAYOUT_ID,
        "node_count": 25,
        "source_sequence": sequence,
        "sample_monotonic_ns": 900_000_000 + sequence,
        "valid_mask": [True] * 25,
        "nodes": [
            {
                "id": index,
                "parent": parent,
                "x": index * 0.001,
                "y": index * 0.002,
                "z": index * 0.003,
            }
            for index, parent in enumerate(PARENT_IDS)
        ],
    }


def _source(clock: list[int]) -> UdpManusSource:
    return UdpManusSource(
        "127.0.0.1",
        0,
        source_id="manus-left-test",
        hand_side="left",
        stale_after_ns=100_000_000,
        clock_ns=lambda: clock[0],
    )


def test_validated_sample_is_shared_with_callback_and_display_only_mirrors_x() -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    received = []
    source.start(received.append)
    try:
        sample = source.ingest_payload(_payload(), received_time_ns=clock[0])
        assert sample is received[0]
        assert sample is not None
        assert isinstance(sample.payload, ManusKeypoints)
        assert sample.payload.layout_id == LAYOUT_ID
        assert sample.payload.points_m[10] == (0.01, 0.02, 0.03)
        assert sample.sequence == 0
        assert source.status().health is SourceHealth.HEALTHY

        snapshot = source.snapshot()
        assert snapshot["control_correlated"] is False
        assert snapshot["nodes"][10]["x"] == -0.01
        assert snapshot["nodes"][10]["y"] == 0.02
        assert snapshot["source_sequence"] == 0
        assert snapshot["source_health"] == "healthy"
    finally:
        source.stop()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda payload: payload.update(schema_version=2), "schema-version-mismatch"),
        (lambda payload: payload.update(side="right"), "side-mismatch"),
        (lambda payload: payload.update(layout="unknown-layout"), "layout-mismatch"),
        (lambda payload: payload["nodes"].pop(), "node-count-mismatch"),
        (
            lambda payload: payload["nodes"][7].update(parent=0),
            "parent-layout-mismatch",
        ),
        (lambda payload: payload["nodes"][4].update(x=float("nan")), "must be finite"),
        (
            lambda payload: payload["valid_mask"].__setitem__(3, False),
            "invalid-node-mask",
        ),
    ],
)
def test_invalid_frames_are_rejected_and_exposed_as_degraded(mutation, reason) -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    try:
        assert source.ingest_payload(_payload(), received_time_ns=clock[0]) is not None
        invalid = deepcopy(_payload(1))
        mutation(invalid)
        assert source.ingest_payload(invalid, received_time_ns=clock[0] + 1) is None
        status = source.status(clock[0] + 2)
        assert status.health is SourceHealth.DEGRADED
        assert reason in status.reason
        snapshot = source.snapshot()
        assert snapshot["source_sequence"] == 0
        assert snapshot["rejected_frames"] == 1
    finally:
        source.stop()


def test_source_counts_sequence_gaps_rejects_reordering_and_becomes_stale() -> None:
    clock = [1_000_000_000]
    source = _source(clock)
    try:
        assert source.ingest_payload(_payload(2), received_time_ns=clock[0]) is not None
        clock[0] += 20_000_000
        assert source.ingest_payload(_payload(5), received_time_ns=clock[0]) is not None
        assert source.snapshot()["dropped_since_last"] == 2

        assert source.ingest_payload(_payload(4), received_time_ns=clock[0] + 1) is None
        assert source.status(clock[0] + 2).health is SourceHealth.DEGRADED

        clock[0] += 100_000_001
        status = source.status()
        assert status.health is SourceHealth.STALE
        assert status.reason == "source-stale"
    finally:
        source.stop()


def test_ros_bridge_offer_does_not_wait_for_serialization(monkeypatch) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    publisher = ManusDatagramPublisher(
        sender,
        "127.0.0.1",
        receiver.getsockname()[1],
    )
    serialization_started = threading.Event()
    serialization_release = threading.Event()
    original_dumps = json.dumps

    def blocking_dumps(*args, **kwargs):
        serialization_started.set()
        assert serialization_release.wait(1.0)
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr("tools.manus_glove_bridge.json.dumps", blocking_dumps)
    nodes = _payload()["nodes"]
    try:
        started_ns = time.perf_counter_ns()
        assert publisher.offer(
            0,
            nodes,
            sequence=7,
            mode="fake",
            side="left",
        )
        offer_ns = time.perf_counter_ns() - started_ns
        assert offer_ns < 5_000_000
        assert serialization_started.wait(1.0)
        serialization_release.set()
        payload = json.loads(receiver.recvfrom(16_384)[0])
        assert payload["source_sequence"] == 7
        assert payload["node_count"] == 25
    finally:
        serialization_release.set()
        publisher.close()
        sender.close()
        receiver.close()


def test_ros_bridge_normalizes_manus_core_root_parent() -> None:
    def node(node_id: int, parent: int):
        return SimpleNamespace(
            node_id=node_id,
            parent_node_id=parent,
            pose=SimpleNamespace(
                position=SimpleNamespace(x=0.1, y=0.2, z=0.3)
            ),
        )

    assert _ros_node_record(node(0, 0))["parent"] == -1
    assert _ros_node_record(node(1, 0))["parent"] == 0
