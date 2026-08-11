#!/usr/bin/env python3
"""Forward the Manus glove skeleton to the switch web demo over local UDP.

The web demo runs in the dex-manipulation venv, which cannot import ROS 2
(jazzy uses the system Python plus the ``manus_ws`` overlay).  This bridge runs
in the ROS 2 environment, subscribes to ``/manus_glove_{id}`` (``ManusGlove``),
and datagrams the latest 3-D node skeleton to ``127.0.0.1:<udp_port>`` as JSON,
which the demo renders as a live hand skeleton (a port of ``manus_data_viz.py``).

Real glove (run in the ROS 2 env):
    source /opt/ros/jazzy/setup.bash
    source /home/user/dex_teleop/dex_teleop/manus_ws/install/setup.bash
    python tools/manus_glove_bridge.py --glove-id 0

No glove (verify the demo's rendering path):
    python tools/manus_glove_bridge.py --fake

Payload schema version 1 includes native Manus coordinates, monotonic time,
source sequence, side, mode, validity, and the exact 25-node layout identity.
The bridge itself marks frames ``control_correlated=false``; the runtime changes
that presentation state only after the same validated sample produced a command.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import socket
import threading
import time


@dataclass(frozen=True)
class _ManusBridgeFrame:
    sample_monotonic_ns: int
    glove_id: int
    nodes: tuple[tuple[int, int, float, float, float], ...]
    sequence: int
    mode: str
    side: str


def _ros_node_record(node: object) -> dict[str, int | float]:
    """Normalize MANUS Core's self-parented root to our tree contract."""

    node_id = int(getattr(node, "node_id"))
    parent = int(getattr(node, "parent_node_id"))
    if node_id == 0:
        parent = -1
    pos = getattr(getattr(node, "pose"), "position")
    return {
        "id": node_id,
        "parent": parent,
        "x": float(getattr(pos, "x")),
        "y": float(getattr(pos, "y")),
        "z": float(getattr(pos, "z")),
    }


def _encode_frame(frame: _ManusBridgeFrame) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source": "manus-ros2-bridge",
            "mode": frame.mode,
            "control_correlated": False,
            "glove_id": frame.glove_id,
            "side": frame.side,
            "layout": "manus-raw-25-v1",
            "node_count": len(frame.nodes),
            "source_sequence": frame.sequence,
            "sample_monotonic_ns": frame.sample_monotonic_ns,
            "valid_mask": [True] * len(frame.nodes),
            "nodes": [
                {"id": node_id, "parent": parent, "x": x, "y": y, "z": z}
                for node_id, parent, x, y, z in frame.nodes
            ],
        },
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


class ManusDatagramPublisher:
    """Single-slot bridge that never serializes or sends on the ROS callback."""

    def __init__(self, sock: socket.socket, host: str, port: int) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("Manus telemetry must use a loopback destination")
        if not 1 <= port <= 65535:
            raise ValueError("Manus telemetry UDP port must be within 1..65535")
        self._sock = sock
        self._sock.setblocking(False)
        self._destination = (host, port)
        self._lock = threading.Lock()
        self._latest: _ManusBridgeFrame | None = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._replaced = 0
        self._contention_drops = 0
        self._send_errors = 0
        self._thread = threading.Thread(
            target=self._worker,
            name="manus-telemetry-publisher",
            daemon=True,
        )
        self._thread.start()

    @property
    def statistics(self) -> dict[str, int]:
        return {
            "replaced": self._replaced,
            "contention_drops": self._contention_drops,
            "send_errors": self._send_errors,
        }

    def offer(
        self,
        glove_id: int,
        nodes: list[dict],
        *,
        sequence: int,
        mode: str,
        side: str,
    ) -> bool:
        frame = _ManusBridgeFrame(
            sample_monotonic_ns=time.monotonic_ns(),
            glove_id=glove_id,
            nodes=tuple(
                (
                    int(node["id"]),
                    int(node["parent"]),
                    float(node["x"]),
                    float(node["y"]),
                    float(node["z"]),
                )
                for node in nodes
            ),
            sequence=sequence,
            mode=mode,
            side=side,
        )
        if self._stop.is_set() or not self._lock.acquire(blocking=False):
            self._contention_drops += 1
            return False
        try:
            if self._latest is not None:
                self._replaced += 1
            self._latest = frame
        finally:
            self._lock.release()
        self._event.set()
        return True

    def _take_latest(self) -> _ManusBridgeFrame | None:
        with self._lock:
            frame = self._latest
            self._latest = None
            self._event.clear()
            return frame

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._event.wait(0.25)
            frame = self._take_latest()
            if frame is None:
                continue
            try:
                encoded = _encode_frame(frame)
                if len(encoded) > 16_384:
                    raise ValueError("Manus telemetry datagram exceeds 16384 bytes")
                self._sock.sendto(encoded, self._destination)
            except (BlockingIOError, OSError, TypeError, ValueError):
                self._send_errors += 1

    def close(self, timeout_s: float = 1.0) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("Manus telemetry publisher did not stop")


def run_fake(sock: socket.socket, host: str, port: int, glove_id: int, hz: float) -> None:
    """Emit a synthetic, gently flexing 25-node hand skeleton for UI testing."""

    # Wrist root + 5 fingers of 4 nodes; positions in metres, palm near origin.
    fingers = [
        {"base_x": -0.03, "curl": 0.9},  # thumb
        {"base_x": -0.015, "curl": 1.0},
        {"base_x": 0.0, "curl": 1.0},
        {"base_x": 0.015, "curl": 1.0},
        {"base_x": 0.03, "curl": 0.95},  # pinky
    ]
    period = 1.0 / hz
    start = time.monotonic()
    sequence = 0
    publisher = ManusDatagramPublisher(sock, host, port)
    try:
        while True:
            t = time.monotonic() - start
            wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * 0.2 * t)
            nodes = [{"id": 0, "parent": -1, "x": 0.0, "y": 0.0, "z": 0.0}]
            node_id = 1
            for finger_index, finger in enumerate(fingers):
                parent = 0
                reach = 0.0
                joint_count = 4 if finger_index == 0 else 5
                for joint in range(joint_count):
                    reach += 0.1 / joint_count
                    bend = wave * finger["curl"] * (joint + 1) / joint_count
                    x = finger["base_x"] + (0.01 if finger_index == 0 else 0.0) * joint
                    y = -math.sin(bend) * reach * 0.6
                    z = math.cos(bend) * reach
                    nodes.append({"id": node_id, "parent": parent, "x": x, "y": y, "z": z})
                    parent = node_id
                    node_id += 1
            publisher.offer(
                glove_id,
                nodes,
                sequence=sequence,
                mode="fake",
                side="left",
            )
            sequence += 1
            time.sleep(period)
    finally:
        publisher.close()


def run_real(sock: socket.socket, host: str, port: int, glove_id: int) -> None:
    import rclpy
    from rclpy.node import Node
    from manus_ros2_msgs.msg import ManusGlove

    publisher = ManusDatagramPublisher(sock, host, port)

    class _Bridge(Node):
        def __init__(self) -> None:
            super().__init__("manus_glove_udp_bridge")
            self._sequence = 0
            self.create_subscription(ManusGlove, f"/manus_glove_{glove_id}", self._on_glove, 20)

        def _on_glove(self, msg: "ManusGlove") -> None:
            # Keep Manus Core's native left-handed coordinates. The retargeter
            # and UI each apply their own explicit conversion.
            nodes = [_ros_node_record(node) for node in msg.raw_nodes]
            raw_side = getattr(msg, "side", "unknown")
            side = raw_side if isinstance(raw_side, str) else str(raw_side)
            publisher.offer(
                int(getattr(msg, "glove_id", glove_id)),
                nodes,
                sequence=self._sequence,
                mode="real",
                side=side,
            )
            self._sequence += 1

    rclpy.init()
    node = _Bridge()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        publisher.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake", action="store_true", help="emit a synthetic skeleton (no ROS 2)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=8770)
    parser.add_argument("--glove-id", type=int, default=0)
    parser.add_argument("--fake-hz", type=float, default=30.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = f"{args.host}:{args.udp_port}"
    try:
        if args.fake:
            print(f"[manus-bridge] FAKE skeleton -> {dest} (glove {args.glove_id})")
            run_fake(sock, args.host, args.udp_port, args.glove_id, args.fake_hz)
        else:
            print(f"[manus-bridge] ROS2 /manus_glove_{args.glove_id} -> {dest}")
            run_real(sock, args.host, args.udp_port, args.glove_id)
    except KeyboardInterrupt:
        print("\n[manus-bridge] stopped")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
