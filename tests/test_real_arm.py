from __future__ import annotations

import json
import socket
import threading
import time

from dex_runtime.real_arm import RealArmGateway


class _ProtocolServer:
    def __init__(self, port: int = 0, *, malformed: bool = False) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", port))
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.session = None
        self.epoch = 0
        self.state = "TELEOP"
        self.heartbeats = 0
        self.anchor_generation = 0
        self.malformed = malformed
        self.requests: list[dict[str, object]] = []

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(1.0)
        self.socket.close()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                data, address = self.socket.recvfrom(8192)
            except socket.timeout:
                continue
            request = json.loads(data)
            self.requests.append(request)
            action = request["action"]
            ok = True
            reason = ""
            if action == "prepare":
                self.session = request["control_session_id"]
                self.epoch = request["control_epoch"]
                self.state = "PREPARED"
            elif action == "enter":
                self.state = "HOLDING"
            elif action == "heartbeat":
                self.heartbeats += 1
                if self.heartbeats >= 2:
                    self.state = "VERIFIED"
            elif action == "reanchor":
                self.anchor_generation += 1
                self.state = "REANCHOR_ACKED"
            elif action == "release":
                self.state = "TELEOP"
            elif action != "status":
                ok = False
                reason = "unknown-action"
            response = {
                "schema_version": 1,
                "kind": "response",
                "command_id": request["command_id"],
                "control_session_id": request["control_session_id"],
                "control_epoch": request["control_epoch"],
                "ok": ok,
                "reason": reason,
                "state": self.state,
                "prepared": self.state != "TELEOP",
                "active": self.state in {"HOLDING", "VERIFIED", "REANCHOR_ACKED"},
                "verified": self.state in {"VERIFIED", "REANCHOR_ACKED"},
                "anchor_generation": self.anchor_generation,
                "fault_reason": None,
                "position_error_mm": 0.2,
                "orientation_error_deg": 0.1,
                "server_monotonic_ns": request["sent_monotonic_ns"],
            }
            if self.malformed:
                response["active"] = "yes"
            self.socket.sendto(json.dumps(response).encode(), address)


def test_real_arm_gateway_requires_identity_bound_verified_hold_and_reanchor() -> None:
    server = _ProtocolServer()
    server.start()
    gateway = RealArmGateway(
        "runtime-session",
        port=server.port,
        request_timeout_s=0.2,
    )
    try:
        assert gateway.probe() is True
        gateway.prepare_hold()
        assert gateway.status.prepared is True
        assert gateway.status.control_epoch == 1
        gateway.enter_hold()
        assert gateway.verify_hold() is False
        assert gateway.verify_hold() is True
        assert gateway.status.position_error_mm == 0.2
        gateway.reanchor_teleop()
        assert gateway.status.anchor_generation == 1
        gateway.release_to_teleop()
        assert gateway.status.state == "TELEOP"
        assert gateway.status.active is False
    finally:
        gateway.close()
        server.close()

    assert [request["action"] for request in server.requests] == [
        "status",
        "prepare",
        "enter",
        "heartbeat",
        "heartbeat",
        "reanchor",
        "release",
    ]
    assert all(request["control_session_id"] == "runtime-session" for request in server.requests)
    assert all(request["deadline_monotonic_ns"] > request["sent_monotonic_ns"] for request in server.requests)


def test_real_arm_gateway_timeout_is_not_hold_verification() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]
    probe.close()
    gateway = RealArmGateway(
        "runtime-session",
        port=unused_port,
        request_timeout_s=0.03,
    )
    try:
        assert gateway.probe() is False
        assert gateway.status.connected is False
        assert gateway.verify_hold() is False
        server = _ProtocolServer(unused_port)
        server.start()
        try:
            assert gateway.probe() is True
            assert gateway.status.connected is True
        finally:
            server.close()
    finally:
        gateway.close()


def test_status_read_never_waits_for_an_inflight_probe_timeout() -> None:
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    silent.settimeout(1.0)
    gateway = RealArmGateway(
        "runtime-session",
        port=silent.getsockname()[1],
        request_timeout_s=0.25,
    )
    result: list[bool] = []
    probe_thread = threading.Thread(target=lambda: result.append(gateway.probe()))
    try:
        probe_thread.start()
        silent.recvfrom(8192)  # proves probe() is waiting while holding request serialization
        started = time.perf_counter()
        status = gateway.status
        elapsed = time.perf_counter() - started

        assert status.state == "DISCONNECTED"
        assert elapsed < 0.02
        assert probe_thread.is_alive()
        probe_thread.join(1.0)
        assert result == [False]
    finally:
        gateway.close()
        silent.close()


def test_real_arm_gateway_rejects_malformed_acknowledgement() -> None:
    server = _ProtocolServer(malformed=True)
    server.start()
    gateway = RealArmGateway(
        "runtime-session",
        port=server.port,
        request_timeout_s=0.1,
    )
    try:
        assert gateway.probe() is False
        assert gateway.status.connected is False
        assert gateway.status.fault_reason == "response-validation-failed"
    finally:
        gateway.close()
        server.close()
