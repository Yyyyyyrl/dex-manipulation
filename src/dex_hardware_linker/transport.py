"""Linker transport boundary. Only the gateway thread calls these objects."""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dex_contracts import AcknowledgementLevel

PINNED_G20_DRIVER_SHA256 = "513be964dee481773ad4d346559e59912b3b70c79c920528783648631f9e10b9"


@dataclass(frozen=True)
class NativeHandState:
    native_range: tuple[float, ...]
    acquisition_time_ns: int
    faults: tuple[str, ...] = ()
    temperatures_c: tuple[float, ...] | None = None
    raw_reference: str | None = None


class LinkerTransport(Protocol):
    acknowledgement_level: AcknowledgementLevel

    def open(self) -> None: ...

    def read_state(self) -> NativeHandState | None: ...

    def send(self, native_range: Sequence[int]) -> None: ...

    def close(self) -> None: ...


class FakeLinkerTransport:
    """Perfect-tracking transport for contract and supervisor tests."""

    acknowledgement_level = AcknowledgementLevel.SENT_TO_BUS

    def __init__(self, initial_state: Sequence[float]) -> None:
        if len(initial_state) != 20:
            raise ValueError("fake Linker transport needs 20 native slots")
        self._state = tuple(float(value) for value in initial_state)
        self._open = False
        self._owner_thread_id: int | None = None
        self.sent_commands: list[tuple[int, ...]] = []
        self.fail_reads = False
        self.fail_sends = False

    def _claim_or_check_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
        elif self._owner_thread_id != current:
            raise RuntimeError("transport accessed outside its exclusive gateway thread")

    def open(self) -> None:
        self._claim_or_check_thread()
        self._open = True

    def read_state(self) -> NativeHandState | None:
        self._claim_or_check_thread()
        if not self._open:
            raise RuntimeError("transport is not open")
        if self.fail_reads:
            return None
        return NativeHandState(self._state, time.monotonic_ns())

    def send(self, native_range: Sequence[int]) -> None:
        self._claim_or_check_thread()
        if not self._open:
            raise RuntimeError("transport is not open")
        if self.fail_sends:
            raise RuntimeError("injected fake transport send failure")
        command = tuple(int(value) for value in native_range)
        if len(command) != 20 or any(value < 0 or value > 255 for value in command):
            raise ValueError("native command must contain 20 values within 0..255")
        self.sent_commands.append(command)
        self._state = tuple(float(value) for value in command)

    def close(self) -> None:
        self._claim_or_check_thread()
        self._open = False


class LinkerSdkTransport:
    """Lazy direct-CAN transport around the pinned LinkerHand SDK.

    No SDK module is imported and no CAN interface is opened until ``open`` is
    invoked by the exclusive gateway thread.
    """

    acknowledgement_level = AcknowledgementLevel.SENT_TO_BUS

    def __init__(
        self,
        sdk_root: str | Path,
        *,
        side: str,
        hand_joint: str,
        can_channel: str,
        speed: Sequence[int],
        torque: Sequence[int],
    ) -> None:
        self.sdk_root = Path(sdk_root).resolve()
        self.side = side
        self.hand_joint = hand_joint
        self.can_channel = can_channel
        self.speed = tuple(int(value) for value in speed)
        self.torque = tuple(int(value) for value in torque)
        self._api = None
        self._owner_thread_id: int | None = None
        if side != "left" or hand_joint != "G20":
            raise ValueError("the initial verified transport is left LinkerHand G20 only")
        if len(self.speed) != 5 or len(self.torque) != 5:
            raise ValueError("speed and torque must each contain five values")

    def _check_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
        elif self._owner_thread_id != current:
            raise RuntimeError("SDK transport accessed outside its exclusive gateway thread")

    def _verify_driver(self) -> Path:
        scripts = self.sdk_root / "linker_hand_sdk_ros" / "scripts"
        driver = scripts / "LinkerHand" / "core" / "can" / "linker_hand_g20_can.py"
        if not driver.is_file():
            raise RuntimeError(f"pinned G20 driver not found at {driver}")
        digest = hashlib.sha256(driver.read_bytes()).hexdigest()
        if digest != PINNED_G20_DRIVER_SHA256:
            raise RuntimeError(
                f"G20 driver digest mismatch: expected {PINNED_G20_DRIVER_SHA256}, got {digest}"
            )
        return scripts

    def open(self) -> None:
        self._check_thread()
        scripts = self._verify_driver()
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from LinkerHand.linker_hand_api import LinkerHandApi  # type: ignore

        self._api = LinkerHandApi(
            hand_type=self.side,
            hand_joint=self.hand_joint,
            can=self.can_channel,
        )
        self._api.set_speed(speed=list(self.speed))
        self._api.set_torque(torque=list(self.torque))

    def read_state(self) -> NativeHandState | None:
        self._check_thread()
        if self._api is None:
            raise RuntimeError("SDK transport is not open")
        raw = self._api.get_state()
        if not isinstance(raw, (list, tuple)) or len(raw) != 20:
            return None
        try:
            values = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            return None
        if any(value < 0.0 or value > 255.0 for value in values):
            return None
        return NativeHandState(values, time.monotonic_ns())

    def send(self, native_range: Sequence[int]) -> None:
        self._check_thread()
        if self._api is None:
            raise RuntimeError("SDK transport is not open")
        self._api.finger_move(pose=[int(value) for value in native_range])

    def close(self) -> None:
        self._check_thread()
        if self._api is not None:
            self._api.close_can()
            self._api = None
