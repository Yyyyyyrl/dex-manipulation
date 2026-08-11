from __future__ import annotations

import threading
import time

import pytest

from dex_runtime.telemetry import TelemetryEnvelope, TelemetryHealth, TelemetryHub


def _envelope(
    sequence: int,
    *,
    now_ns: int = 1_000_000_000,
    health: TelemetryHealth = TelemetryHealth.HEALTHY,
) -> TelemetryEnvelope:
    return TelemetryEnvelope(
        source="manus",
        sequence=sequence,
        sample_monotonic_ns=now_ns - 2_000_000,
        received_monotonic_ns=now_ns - 1_000_000,
        rate_hz=60.0,
        dropped_since_last=0,
        health=health,
        payload={"nodes": [{"id": 0, "valid": True}], "value": 1.5},
        stale_after_ns=10_000_000,
    )


def test_envelope_freezes_payload_and_rejects_non_finite_values() -> None:
    payload = {"values": [1.0, 2.0]}
    envelope = TelemetryEnvelope(
        source="linker",
        sequence=0,
        sample_monotonic_ns=1,
        received_monotonic_ns=2,
        rate_hz=50.0,
        dropped_since_last=0,
        health=TelemetryHealth.HEALTHY,
        payload=payload,
    )
    payload["values"].append(3.0)
    assert envelope.as_dict(2)["payload"] == {"values": [1.0, 2.0]}

    with pytest.raises(ValueError, match="non-finite"):
        TelemetryEnvelope(
            source="linker",
            sequence=1,
            sample_monotonic_ns=1,
            received_monotonic_ns=2,
            rate_hz=50.0,
            dropped_since_last=0,
            health=TelemetryHealth.HEALTHY,
            payload={"bad": float("nan")},
        )


def test_hub_replaces_per_source_without_reader_backlog() -> None:
    hub = TelemetryHub(clock_ns=lambda: 1_005_000_000)
    for sequence in range(100):
        hub.publish(_envelope(sequence))
    snapshot = hub.snapshot()
    assert snapshot["revision"] == 100
    source = snapshot["sources"]["manus"]
    assert source["sequence"] == 99
    assert source["payload"]["nodes"][0]["id"] == 0

    with pytest.raises(ValueError, match="must increase"):
        hub.publish(_envelope(99))


def test_hub_computes_stale_health_at_read_time() -> None:
    hub = TelemetryHub(clock_ns=lambda: 1_020_000_000)
    hub.publish(_envelope(0))
    source = hub.snapshot()["sources"]["manus"]
    assert source["health"] == "stale"
    assert source["age_ms"] == 21.0


def test_wait_for_revision_wakes_on_publish() -> None:
    hub = TelemetryHub()
    result: list[int] = []

    def wait() -> None:
        result.append(hub.wait_for_revision(0, 1.0))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.01)
    hub.publish(_envelope(0, now_ns=time.monotonic_ns()))
    thread.join(1.0)

    assert not thread.is_alive()
    assert result == [1]
