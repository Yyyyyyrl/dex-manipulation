"""Deterministic contract fake for supervisor tests; never opens hardware."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from dex_contracts import (
    AcknowledgementLevel,
    EffectiveHandTarget,
    GatewayAcknowledgement,
    HandCommandAcknowledgement,
    OwnershipState,
    ResourceId,
)


@dataclass(frozen=True)
class FakeOwnershipPreparation:
    nonce: str
    ownership: OwnershipState


class _ResolvedTicket:
    def __init__(self, acknowledgement: HandCommandAcknowledgement) -> None:
        self.acknowledgement = acknowledgement

    def wait(self, timeout_s: float) -> HandCommandAcknowledgement:
        if timeout_s <= 0:
            raise TimeoutError("fake acknowledgement timeout must be positive")
        return self.acknowledgement


class FakeHandGateway:
    def __init__(
        self,
        control_session_id: str,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not control_session_id:
            raise ValueError("fake hand gateway needs a control-session ID")
        self.control_session_id = control_session_id
        self.clock_ns = clock_ns
        self.ownership: OwnershipState | None = None
        self._prepared: FakeOwnershipPreparation | None = None
        self.sent_commands = []
        self.last_effective_target: EffectiveHandTarget | None = None
        self.fail_send = False

    def prepare_ownership(self, ownership: OwnershipState) -> FakeOwnershipPreparation:
        now_ns = self.clock_ns()
        if ownership.control_session_id != self.control_session_id:
            raise ValueError("fake gateway ownership session mismatch")
        if ownership.resource_id is not ResourceId.HAND or not ownership.valid_at(now_ns):
            raise ValueError("fake gateway ownership is invalid")
        current_epoch = -1 if self.ownership is None else self.ownership.control_epoch
        if ownership.control_epoch <= current_epoch:
            raise ValueError("fake gateway ownership epoch must increase")
        self._prepared = FakeOwnershipPreparation(uuid.uuid4().hex, ownership)
        return self._prepared

    def commit_ownership(self, preparation: FakeOwnershipPreparation) -> None:
        if preparation != self._prepared:
            raise ValueError("fake gateway preparation is stale")
        self.ownership = preparation.ownership
        self._prepared = None

    def submit(self, command) -> _ResolvedTicket:
        now_ns = self.clock_ns()
        ownership = self.ownership
        if ownership is None or not ownership.valid_at(now_ns):
            raise ValueError("fake gateway has no valid ownership")
        if command.identity.control_session_id != self.control_session_id:
            raise ValueError("fake gateway command session mismatch")
        if command.identity.control_epoch != ownership.control_epoch:
            raise ValueError("fake gateway command epoch mismatch")
        if command.owner is not ownership.owner or command.command_mode is not ownership.command_mode:
            raise ValueError("fake gateway command owner or mode mismatch")
        if command.deadline_ns <= now_ns:
            raise ValueError("fake gateway command deadline expired")
        if self.fail_send:
            raise RuntimeError("injected fake hand send failure")
        self.sent_commands.append(command)
        effective = EffectiveHandTarget(
            semantic_position=command.semantic_position,
            command_id=command.command_id,
            evidence_level=AcknowledgementLevel.SENT_TO_BUS,
            evidence_time_ns=now_ns,
        )
        self.last_effective_target = effective
        acknowledgement = GatewayAcknowledgement(
            identity=command.identity,
            command_id=command.command_id,
            level=AcknowledgementLevel.SENT_TO_BUS,
            acknowledged_time_ns=now_ns,
            detail="deterministic fake accepted the semantic target",
        )
        return _ResolvedTicket(HandCommandAcknowledgement(acknowledgement, effective))
