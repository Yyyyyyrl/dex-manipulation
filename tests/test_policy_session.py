from __future__ import annotations

import pytest
import torch

from dex_runtime.policy_package import validate_policy_package
from dex_runtime.policy_session import PolicySession, PolicySessionState
from policy_package_factory import CALIBRATION_LOWER, CALIBRATION_UPPER, write_test_package

MIDPOINT = [
    (lower + upper) * 0.5
    for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER, strict=False)
]
MEASURED = [
    lower + 0.45 * (upper - lower)
    for lower, upper in zip(CALIBRATION_LOWER, CALIBRATION_UPPER, strict=False)
]


@pytest.mark.parametrize(
    ("free_object", "period_ns", "actor_width"),
    ((False, 100_000_000, 32), (True, 50_000_000, 96)),
)
def test_continuous_shadow_exact_rate_and_activation_has_no_double_step(
    tmp_path, free_object: bool, period_ns: int, actor_width: int
) -> None:
    package = validate_policy_package(
        write_test_package(tmp_path / "policy", free_object=free_object),
        allow_unsigned_local=True,
    )
    session = PolicySession(package)
    measured = MEASURED
    effective = MIDPOINT
    session.reset(
        measured,
        effective,
        control_session_id="session",
        source_id="policy-session",
        control_epoch=4,
    )
    assert session.status.state is PolicySessionState.SHADOW
    assert session.status.history_count == 0
    with pytest.raises(RuntimeError, match="history"):
        session.preview()

    for tick in range(30):
        session.observe(
            measured,
            effective,
            tick=tick,
            scheduled_time_ns=1_000_000_000 + tick * period_ns,
            state_sequence=tick,
        )
    assert session.status.history_ready
    assert session.codec.assemble_actor_input(session._history).shape == (1, actor_width)
    preview = session.preview()
    trace = session.last_inference
    assert trace is not None
    assert trace.tick == 29
    assert len(trace.codec_input) == actor_width
    assert len(trace.latent) == 8
    assert len(trace.action) == len(trace.target) == 16
    assert session.preview() is preview
    before = session.status
    activated = session.activate(tick=29, control_epoch=5)
    after = session.status
    assert activated.semantic_position == preview.semantic_position
    assert activated.identity.control_epoch == 5
    assert after.state is PolicySessionState.ACTIVE
    assert after.history_count == before.history_count == 30
    assert after.preview_sequence == before.preview_sequence == 1
    assert (
        max(
            abs(value - expected)
            for value, expected in zip(activated.semantic_position, effective, strict=False)
        )
        <= 0.05 + 1e-6
    )

    candidate = session.step(
        measured,
        activated.semantic_position,
        tick=30,
        scheduled_time_ns=1_000_000_000 + 30 * period_ns,
        state_sequence=30,
    )
    assert candidate.identity.control_epoch == 5
    assert session.status.preview_sequence == 2
    session.close()
    assert session.status.state is PolicySessionState.CLOSED
    session.close()


def test_policy_session_rejects_duplicate_tick_and_wrong_cadence(tmp_path) -> None:
    package = validate_policy_package(
        write_test_package(tmp_path / "policy"), allow_unsigned_local=True
    )
    session = PolicySession(package)
    value = MIDPOINT
    session.reset(
        value,
        value,
        control_session_id="session",
        source_id="policy-session",
        control_epoch=1,
    )
    session.observe(value, value, tick=0, scheduled_time_ns=1_000, state_sequence=1)
    count = session.status.history_count
    with pytest.raises(ValueError, match="consecutive"):
        session.observe(value, value, tick=0, scheduled_time_ns=1_000, state_sequence=2)
    assert session.status.history_count == count
    with pytest.raises(ValueError, match="cadence"):
        session.observe(value, value, tick=1, scheduled_time_ns=1_001, state_sequence=2)
    assert session.status.history_count == count


def test_preview_preserves_manifest_limits_across_float32_boundary(tmp_path) -> None:
    package = validate_policy_package(
        write_test_package(tmp_path / "policy"), allow_unsigned_local=True
    )
    session = PolicySession(package)
    with torch.no_grad():
        for parameter in session.actor.parameters():
            parameter.zero_()
        session.actor.mu.bias.fill_(-1.0)

    lower = list(CALIBRATION_LOWER)
    session.reset(
        lower,
        lower,
        control_session_id="session",
        source_id="policy-session",
        control_epoch=1,
    )
    for tick in range(session.codec.spec.history_length):
        session.observe(
            lower,
            lower,
            tick=tick,
            scheduled_time_ns=1_000 + tick * session.codec.spec.control_period_ns,
            state_sequence=tick + 1,
        )

    preview = session.preview()
    assert preview.semantic_position == CALIBRATION_LOWER
    assert session.last_inference is not None
    assert session.last_inference.target == CALIBRATION_LOWER
