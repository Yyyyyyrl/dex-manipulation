from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from dex_contracts import (
    MessageIdentity,
    PROTOCOL_VERSION,
    ResourceId,
    SourceHealth,
    TimestampedSample,
    canonical_json,
)
from dex_runtime import FakeClock, LatestValueBuffer


def test_contracts_are_immutable_and_deterministically_serialized():
    identity = MessageIdentity(
        protocol_version=PROTOCOL_VERSION,
        control_session_id="session",
        source_id="source",
        resource_id=ResourceId.HAND,
        hand_model="LinkerHand G20",
        hand_side="left",
        semantic_schema_id="schema",
        task_id=None,
        task_version=None,
        policy_package_id=None,
        calibration_id="calibration",
        control_epoch=3,
        sequence=9,
    )
    with pytest.raises(FrozenInstanceError):
        identity.sequence = 10  # type: ignore[misc]
    encoded = canonical_json(identity)
    assert json.loads(encoded)["resource_id"] == "hand"
    assert encoded == canonical_json(identity)


def test_task_identity_requires_id_and_version_together():
    identity = MessageIdentity(
        protocol_version=PROTOCOL_VERSION,
        control_session_id="session",
        source_id="source",
        resource_id=ResourceId.HAND,
        hand_model="LinkerHand G20",
        hand_side="left",
        semantic_schema_id="schema",
        task_id=None,
        task_version=None,
        policy_package_id=None,
        calibration_id="calibration",
        control_epoch=0,
        sequence=0,
    )
    with pytest.raises(ValueError, match="task ID and task version"):
        replace(identity, task_id="task")


def test_timestamp_age_uses_local_receive_time_only():
    sample = TimestampedSample(
        payload=(1, 2),
        generated_time_ns=9_000_000_000_000,
        received_time_ns=100,
        sequence=0,
        source_health=SourceHealth.HEALTHY,
        validity_mask=(True, True),
        coordinate_frame_id="source",
        units="meter",
    )
    assert sample.age_ns(150) == 50


def test_fake_clock_is_monotonic():
    clock = FakeClock(10)
    assert clock.advance_ns(5) == 15
    with pytest.raises(ValueError):
        clock.advance_ns(-1)
    with pytest.raises(ValueError):
        clock.set_ns(14)


def test_latest_buffer_reports_overwrite_instead_of_silent_drop():
    buffer: LatestValueBuffer[str] = LatestValueBuffer()
    first = buffer.publish("a")
    second = buffer.publish("b")
    assert not first.replaced_unread
    assert second.replaced_unread
    assert second.total_replaced_unread == 1
    assert buffer.take_latest(0) == (1, "b")
    assert buffer.take_latest(0) is None
