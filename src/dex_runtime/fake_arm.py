"""Deterministic fake arm gateway and hold controller for hand-only M2."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FakeArmHoldStatus:
    connected: bool
    prepared: bool
    active: bool
    verified: bool
    anchor_generation: int
    fault_reason: str | None


class FakeArmGateway:
    def __init__(self) -> None:
        self.prepared = False
        self.hold_active = False
        self.hold_verified = False
        self.anchor_generation = 0
        self.fault_reason: str | None = None

    @property
    def status(self) -> FakeArmHoldStatus:
        return FakeArmHoldStatus(
            True,
            self.prepared,
            self.hold_active,
            self.hold_verified,
            self.anchor_generation,
            self.fault_reason,
        )

    def prepare_hold(self) -> None:
        if self.fault_reason is not None:
            raise RuntimeError(f"fake arm fault: {self.fault_reason}")
        self.prepared = True

    def enter_hold(self) -> None:
        if not self.prepared:
            raise RuntimeError("fake arm hold was not prepared")
        self.hold_active = True
        self.hold_verified = True

    def verify_hold(self) -> bool:
        return self.hold_active and self.hold_verified and self.fault_reason is None

    def reanchor_teleop(self) -> None:
        if not self.hold_active:
            raise RuntimeError("fake arm must remain held while re-anchoring")
        self.anchor_generation += 1

    def release_to_teleop(self) -> None:
        if self.anchor_generation <= 0:
            raise RuntimeError("fake arm teleoperation anchor was not prepared")
        self.prepared = False
        self.hold_active = False
        self.hold_verified = False

    def inject_fault(self, reason: str) -> None:
        self.fault_reason = reason
        self.hold_verified = False

    def probe(self) -> bool:
        return self.fault_reason is None

    def close(self) -> None:
        return None
