"""Timestamped F12 operator switch sources; evdev access is lazy and exclusive."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class SwitchEdge(str, Enum):
    PRESS = "press"
    RELEASE = "release"


@dataclass(frozen=True)
class OperatorSwitchEvent:
    source_id: str
    key: str
    edge: SwitchEdge
    generated_time_ns: int | None
    received_time_ns: int
    sequence: int


class F12Debouncer:
    def __init__(
        self,
        *,
        source_id: str,
        debounce_ns: int,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not source_id or debounce_ns <= 0:
            raise ValueError("switch source ID and positive debounce are required")
        self.source_id = source_id
        self.debounce_ns = debounce_ns
        self.clock_ns = clock_ns
        self._last_edge: SwitchEdge | None = None
        self._last_accepted_ns: int | None = None
        self._sequence = 0

    def ingest(self, edge: SwitchEdge) -> OperatorSwitchEvent | None:
        now_ns = self.clock_ns()
        if edge is self._last_edge:
            return None
        if (
            self._last_accepted_ns is not None
            and now_ns - self._last_accepted_ns < self.debounce_ns
        ):
            return None
        event = OperatorSwitchEvent(
            source_id=self.source_id,
            key="F12",
            edge=edge,
            generated_time_ns=None,
            received_time_ns=now_ns,
            sequence=self._sequence,
        )
        self._sequence += 1
        self._last_edge = edge
        self._last_accepted_ns = now_ns
        return event


class EvdevF12SwitchSource:
    """PCsensor foot switch reader fixed to the user-confirmed F12 binding."""

    EXPECTED_VENDOR_ID = 0x3553
    EXPECTED_PRODUCT_ID = 0xB001

    def __init__(
        self,
        *,
        device_path: str,
        source_id: str,
        debounce_ns: int,
        require_pcsensor_identity: bool = True,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not device_path:
            raise ValueError("evdev F12 source requires a device path")
        self.device_path = device_path
        self.require_pcsensor_identity = require_pcsensor_identity
        self._debouncer = F12Debouncer(
            source_id=source_id,
            debounce_ns=debounce_ns,
            clock_ns=clock_ns,
        )
        self._callback: Callable[[OperatorSwitchEvent], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._device = None
        self._status = "not-started"

    @property
    def status(self) -> str:
        return self._status

    def ingest_key_value(self, key_name: str, value: int) -> OperatorSwitchEvent | None:
        if key_name != "KEY_F12" or value not in (0, 1):
            return None
        event = self._debouncer.ingest(SwitchEdge.PRESS if value == 1 else SwitchEdge.RELEASE)
        callback = self._callback
        if event is not None and callback is not None:
            callback(event)
        return event

    def start(self, callback: Callable[[OperatorSwitchEvent], None]) -> None:
        if self._thread is not None:
            raise RuntimeError("evdev F12 source is already running")
        self._callback = callback
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="operator-switch-f12",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        device = self._device
        if device is not None:
            try:
                device.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError("evdev F12 source did not stop")
        self._thread = None
        self._callback = None

    def _worker(self) -> None:
        try:
            from evdev import InputDevice, categorize, ecodes
        except ImportError as exc:
            self._status = f"evdev-capability-missing:{exc}"
            return
        device = None
        try:
            device = InputDevice(self.device_path)
            self._device = device
            if self.require_pcsensor_identity and (
                device.info.vendor != self.EXPECTED_VENDOR_ID
                or device.info.product != self.EXPECTED_PRODUCT_ID
            ):
                raise RuntimeError(
                    f"foot switch USB identity mismatch: "
                    f"{device.info.vendor:04x}:{device.info.product:04x}"
                )
            capabilities = device.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_F12 not in capabilities:
                raise RuntimeError("configured switch device does not advertise KEY_F12")
            device.grab()
            self._status = "healthy-exclusive-grab"
            for raw_event in device.read_loop():
                if self._stop.is_set():
                    break
                if raw_event.type != ecodes.EV_KEY:
                    continue
                key_event = categorize(raw_event)
                key_name = key_event.keycode
                if isinstance(key_name, list):
                    key_name = key_name[0]
                if key_event.keystate == key_event.key_hold:
                    continue
                value = 1 if key_event.keystate == key_event.key_down else 0
                self.ingest_key_value(str(key_name), value)
        except BaseException as exc:
            if not self._stop.is_set():
                self._status = f"switch-fault:{type(exc).__name__}:{exc}"
        finally:
            if device is not None:
                try:
                    device.ungrab()
                except OSError:
                    pass
                try:
                    device.close()
                except OSError:
                    pass
            self._device = None


def is_toggle_request(event: OperatorSwitchEvent) -> bool:
    return event.key == "F12" and event.edge is SwitchEdge.PRESS
