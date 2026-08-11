"""Minimal local terminal status display for the hand-only runtime."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class RuntimeStatus:
    state: str
    hand_owner: str
    arm_owner: str
    control_epoch: int
    hand_health: str
    manus_health: str
    gateway_health: str
    policy_name: str | None
    policy_compatible: bool | None
    history_count: int | None
    history_required: int | None
    blend_alpha: float | None
    readiness_ready: bool
    rejection_reason: str | None
    recording: bool


class TerminalStatusRenderer:
    def __init__(self, stream: TextIO = sys.stdout, *, use_ansi: bool = True) -> None:
        self.stream = stream
        self.use_ansi = use_ansi

    @staticmethod
    def format(status: RuntimeStatus) -> str:
        policy = status.policy_name or "none"
        compatible = (
            "n/a"
            if status.policy_compatible is None
            else ("yes" if status.policy_compatible else "NO")
        )
        history = (
            "n/a"
            if status.history_count is None or status.history_required is None
            else f"{status.history_count}/{status.history_required}"
        )
        blend = "-" if status.blend_alpha is None else f"{status.blend_alpha:.2f}"
        rejection = status.rejection_reason or "-"
        return (
            f"state={status.state} hand={status.hand_owner} arm={status.arm_owner} "
            f"epoch={status.control_epoch} | health hand={status.hand_health} "
            f"manus={status.manus_health} gateway={status.gateway_health} | "
            f"policy={policy} compatible={compatible} history={history} blend={blend} | "
            f"ready={'yes' if status.readiness_ready else 'NO'} "
            f"recording={'on' if status.recording else 'off'} rejection={rejection}"
        )

    def render(self, status: RuntimeStatus) -> None:
        line = self.format(status)
        if self.use_ansi:
            self.stream.write("\r\x1b[2K" + line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.use_ansi:
            self.stream.write("\n")
            self.stream.flush()
