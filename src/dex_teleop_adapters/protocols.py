"""Structural contracts for operator sources and retargeters.

These are `typing.Protocol` definitions, not base classes. Nothing subclasses
them and nothing checks them at runtime: they exist so the contract that
`ManusHandSource`, `ManusRetargeter`, and `OpenXRRetargeter` already satisfy by
duck typing is written down once, in a form a type checker can verify and a
newcomer can read without reverse-engineering an implementation.

Adding a new operator device means satisfying `TeleopSource` (if it produces
raw tracking data) and `Retargeter` (to turn that data into hand targets).
See docs/interfaces/teleop.md for the step-by-step recipe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from dex_contracts import SourceHealth, TeleopHandCandidate, TimestampedSample

from .retargeting import RetargeterStatus

__all__ = ["Retargeter", "SourceStatus", "TeleopSource"]


class SourceStatus(Protocol):
    """Health view every operator source exposes.

    `ManusSourceStatus` and `OpenXRSourceStatus` both match this shape. The
    runtime reads it to decide whether an input is trustworthy, so `health` must
    reflect staleness, not merely whether the transport is still open.
    """

    #: Stable identifier for this device, echoed into command identity.
    source_id: str
    #: HEALTHY only while samples are arriving and passing validation.
    health: SourceHealth
    #: Monotonically increasing count of accepted samples.
    sequence: int
    #: Local monotonic time of the last accepted sample; None before the first.
    last_receive_time_ns: int | None
    #: Human-readable explanation when `health` is not HEALTHY; empty otherwise.
    reason: str


class TeleopSource(Protocol):
    """A device that publishes timestamped operator tracking data.

    Implementations own their transport (ROS subscription, UDP socket, SDK
    callback) and validate the payload before publishing: handedness, joint
    layout, and per-node validity are the source's job, not the retargeter's.

    A source must never actuate anything. It publishes `TimestampedSample`
    objects through the callback and reports health through `status`.
    """

    def start(self, callback: Callable[[TimestampedSample], None]) -> None:
        """Begin publishing samples to `callback`.

        The callback runs on the source's own transport thread, so it must stay
        cheap. The runtime's callback only stores into a `LatestValueBuffer`.
        """
        ...

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop publishing and release the transport, within `timeout_s`."""
        ...

    def status(self, now_ns: int | None = None) -> SourceStatus:
        """Report current health.

        Passing `now_ns` lets staleness be judged against the caller's clock
        rather than the source's own.
        """
        ...


class Retargeter(Protocol):
    """Converts operator tracking data into a semantic hand target.

    This is where device-specific geometry ends. A retargeter validates the
    sample, maps the device's joint layout into the solver's expected layout,
    estimates the wrist frame, solves, and projects the result onto the
    calibration's named semantic joints. Everything downstream sees only a
    `TeleopHandCandidate` and never learns which device produced it.

    Implementations are stateful: they carry a low-pass filter and a solver warm
    start across calls, which is why `reset` exists and must be called on
    session start and after tracking loss.
    """

    def retarget(
        self,
        sample: TimestampedSample,
        *,
        control_session_id: str,
        control_epoch: int,
        task_id: str | None,
        task_version: str | None,
    ) -> TeleopHandCandidate:
        """Solve one sample into a hand candidate.

        Raises rather than returning a degraded result: an unhealthy sample, a
        wrong payload type, a handedness mismatch, or a solver failure is an
        exception, because emitting a plausible-but-wrong hand pose is worse
        than emitting nothing.
        """
        ...

    def reset(self) -> None:
        """Drop filter state and solver warm start.

        Call on session start and whenever tracking has been lost, so a stale
        pose cannot be blended into the first post-recovery target.
        """
        ...

    def status(self) -> RetargeterStatus:
        """Report profile identity, reset count, and last solver timing/error."""
        ...
