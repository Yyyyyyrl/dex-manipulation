"""Loopback client for the single-owner dex_teleop Hitbot hold controller."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import socket
import threading
import time
import uuid
from typing import Callable


ARM_HOLD_SCHEMA_VERSION = 1
MAX_DATAGRAM_BYTES = 8192
RESPONSE_FIELDS = {
    "schema_version",
    "kind",
    "command_id",
    "control_session_id",
    "control_epoch",
    "ok",
    "reason",
    "state",
    "prepared",
    "active",
    "verified",
    "anchor_generation",
    "fault_reason",
    "position_error_mm",
    "orientation_error_deg",
    "server_monotonic_ns",
}
ARM_STATES = {
    "TELEOP",
    "PREPARED",
    "HOLDING",
    "VERIFIED",
    "REANCHOR_ACKED",
    "FAULT_HOLD",
}


@dataclass(frozen=True)
class RealArmHoldStatus:
    connected: bool
    prepared: bool
    active: bool
    verified: bool
    anchor_generation: int
    fault_reason: str | None
    state: str
    control_epoch: int
    position_error_mm: float | None
    orientation_error_deg: float | None
    last_response_monotonic_ns: int | None


class RealArmGateway:
    """Synchronous, identity-bound gateway; it never imports a robot SDK."""

    def __init__(
        self,
        control_session_id: str,
        host: str = "127.0.0.1",
        port: int = 8781,
        *,
        request_timeout_s: float = 0.35,
        command_ttl_ns: int = 500_000_000,
        hold_lease_ns: int = 1_000_000_000,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not control_session_id:
            raise ValueError("arm hold control-session ID is required")
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("arm hold control must use loopback")
        if not 1 <= port <= 65535:
            raise ValueError("arm hold control port must be within 1..65535")
        if request_timeout_s <= 0 or command_ttl_ns <= 0 or hold_lease_ns <= 0:
            raise ValueError("arm hold request timeout, command TTL, and lease must be positive")
        self.control_session_id = control_session_id
        self.destination = (host, port)
        self.request_timeout_s = request_timeout_s
        self.command_ttl_ns = command_ttl_ns
        self.hold_lease_ns = hold_lease_ns
        self._clock_ns = clock_ns
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.connect(self.destination)
        # Network requests are serialized, but their bounded receive timeout
        # must never block read-only status consumers in the hand-control loop.
        self._request_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._control_epoch = 0
        self._closed = False
        self._status = RealArmHoldStatus(
            False, False, False, False, 0, None, "DISCONNECTED", 0, None, None, None
        )

    @property
    def status(self) -> RealArmHoldStatus:
        with self._status_lock:
            return self._status

    def _set_disconnected(self, reason: str) -> None:
        with self._status_lock:
            previous = self._status
            self._status = RealArmHoldStatus(
                False,
                previous.prepared,
                previous.active,
                False,
                previous.anchor_generation,
                reason,
                "DISCONNECTED",
                previous.control_epoch,
                previous.position_error_mm,
                previous.orientation_error_deg,
                previous.last_response_monotonic_ns,
            )

    def _request(self, action: str, *, require_ok: bool = True) -> dict[str, object]:
        with self._request_lock:
            if self._closed:
                raise RuntimeError("arm hold gateway is closed")
            command_id = str(uuid.uuid4())
            now_ns = self._clock_ns()
            request = {
                "schema_version": ARM_HOLD_SCHEMA_VERSION,
                "kind": "request",
                "command_id": command_id,
                "control_session_id": self.control_session_id,
                "control_epoch": self._control_epoch,
                "action": action,
                "sent_monotonic_ns": now_ns,
                "deadline_monotonic_ns": now_ns + self.command_ttl_ns,
                "hold_lease_ns": self.hold_lease_ns,
            }
            encoded = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()
            deadline = time.monotonic() + self.request_timeout_s
            self._sock.settimeout(min(0.1, self.request_timeout_s))
            while time.monotonic() < deadline:
                try:
                    self._sock.send(encoded)
                    data = self._sock.recv(MAX_DATAGRAM_BYTES)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self._set_disconnected(f"transport:{type(exc).__name__}")
                    raise ConnectionError("arm hold controller transport failed") from exc
                try:
                    response = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(response, dict) or response.get("command_id") != command_id:
                    continue
                if (
                    response.get("schema_version") != ARM_HOLD_SCHEMA_VERSION
                    or response.get("kind") != "response"
                    or response.get("control_session_id") != self.control_session_id
                    or response.get("control_epoch") != self._control_epoch
                ):
                    self._set_disconnected("response-identity-mismatch")
                    raise RuntimeError("arm hold response identity mismatch")
                try:
                    self._validate_response(response)
                except RuntimeError:
                    self._set_disconnected("response-validation-failed")
                    raise
                self._update_status(response)
                if require_ok and response.get("ok") is not True:
                    raise RuntimeError(
                        "arm hold request rejected: "
                        + str(response.get("reason") or "unspecified")
                    )
                return response
            self._set_disconnected("response-timeout")
            raise TimeoutError(f"arm hold {action} acknowledgement timed out")

    @staticmethod
    def _validate_response(response: dict[str, object]) -> None:
        if set(response) != RESPONSE_FIELDS:
            raise RuntimeError("arm hold response fields are invalid")
        for name in ("ok", "prepared", "active", "verified"):
            if not isinstance(response.get(name), bool):
                raise RuntimeError(f"arm hold response {name} is not boolean")
        if response.get("state") not in ARM_STATES:
            raise RuntimeError("arm hold response state is invalid")
        if not isinstance(response.get("reason"), str):
            raise RuntimeError("arm hold response reason is invalid")
        fault = response.get("fault_reason")
        if fault is not None and not isinstance(fault, str):
            raise RuntimeError("arm hold response fault is invalid")
        generation = response.get("anchor_generation")
        server_ns = response.get("server_monotonic_ns")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or isinstance(server_ns, bool)
            or not isinstance(server_ns, int)
            or server_ns < 0
        ):
            raise RuntimeError("arm hold response counters are invalid")
        for name in ("position_error_mm", "orientation_error_deg"):
            value = response.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise RuntimeError(f"arm hold response {name} is invalid")
        if response["verified"] and not response["active"]:
            raise RuntimeError("verified arm hold response is not active")

    def _update_status(self, response: dict[str, object]) -> None:
        def optional_number(name: str) -> float | None:
            value = response.get(name)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        with self._status_lock:
            self._status = RealArmHoldStatus(
                True,
                bool(response.get("prepared")),
                bool(response.get("active")),
                bool(response.get("verified")),
                int(response.get("anchor_generation") or 0),
                None
                if response.get("fault_reason") is None
                else str(response["fault_reason"]),
                str(response.get("state") or "UNKNOWN"),
                self._control_epoch,
                optional_number("position_error_mm"),
                optional_number("orientation_error_deg"),
                self._clock_ns(),
            )

    def probe(self) -> bool:
        try:
            response = self._request("status", require_ok=False)
        except (ConnectionError, RuntimeError, TimeoutError):
            return False
        return response.get("ok") is True and response.get("state") == "TELEOP"

    def prepare_hold(self) -> None:
        with self._request_lock:
            self._control_epoch += 1
        self._request("prepare")

    def enter_hold(self) -> None:
        self._request("enter")

    def verify_hold(self) -> bool:
        try:
            response = self._request("heartbeat", require_ok=False)
        except (ConnectionError, RuntimeError, TimeoutError):
            return False
        return (
            response.get("ok") is True
            and response.get("active") is True
            and response.get("verified") is True
            and response.get("fault_reason") is None
        )

    def reanchor_teleop(self) -> None:
        before = self.status.anchor_generation
        response = self._request("reanchor")
        if int(response.get("anchor_generation") or 0) <= before:
            raise RuntimeError("arm teleoperation re-anchor was not acknowledged")

    def release_to_teleop(self) -> None:
        response = self._request("release")
        if response.get("state") != "TELEOP" or response.get("active") is not False:
            raise RuntimeError("arm hold release was not acknowledged")

    def close(self) -> None:
        with self._request_lock:
            if self._closed:
                return
            self._closed = True
            self._sock.close()
