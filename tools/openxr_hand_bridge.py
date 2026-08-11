#!/usr/bin/env python3
"""Publish one Quest/WiVRn OpenXR hand stream to hand and arm consumers.

The bridge is the only process that opens ``VRHandReader``.  It fans each
immutable 26-joint frame out over two loopback UDP destinations: the hand
runtime/UI and the single Hitbot owner.  Network and JSON work run off the
OpenXR polling thread so a slow consumer never backpressures tracking.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1
LAYOUT_ID = "openxr-hand-26-v1"
JOINT_NAMES = (
    "palm", "wrist",
    "thumb_metacarpal", "thumb_proximal", "thumb_distal", "thumb_tip",
    "index_metacarpal", "index_proximal", "index_intermediate", "index_distal", "index_tip",
    "middle_metacarpal", "middle_proximal", "middle_intermediate", "middle_distal", "middle_tip",
    "ring_metacarpal", "ring_proximal", "ring_intermediate", "ring_distal", "ring_tip",
    "little_metacarpal", "little_proximal", "little_intermediate", "little_distal", "little_tip",
)
PARENT_IDS = (
    1, -1,
    1, 2, 3, 4,
    1, 6, 7, 8, 9,
    1, 11, 12, 13, 14,
    1, 16, 17, 18, 19,
    1, 21, 22, 23, 24,
)


class LatestDatagramFanout:
    def __init__(self, host: str, ports: tuple[int, ...]) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("OpenXR telemetry destinations must be loopback")
        if not ports or any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("OpenXR telemetry ports must be within 1..65535")
        self._destinations = tuple((host, port) for port in dict.fromkeys(ports))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._lock = threading.Lock()
        self._latest: dict[str, object] | None = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self.replaced = 0
        self.send_errors = 0
        self._thread = threading.Thread(
            target=self._worker,
            name="openxr-telemetry-fanout",
            daemon=True,
        )
        self._thread.start()

    def offer(self, frame: dict[str, object]) -> None:
        with self._lock:
            if self._latest is not None:
                self.replaced += 1
            self._latest = frame
        self._event.set()

    def _take(self) -> dict[str, object] | None:
        with self._lock:
            frame = self._latest
            self._latest = None
            self._event.clear()
            return frame

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._event.wait(0.25)
            frame = self._take()
            if frame is None:
                continue
            try:
                encoded = json.dumps(
                    frame,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > 48_000:
                    raise ValueError("OpenXR frame exceeds 48000 bytes")
                for destination in self._destinations:
                    try:
                        self._sock.sendto(encoded, destination)
                    except (BlockingIOError, OSError):
                        self.send_errors += 1
            except (TypeError, ValueError):
                self.send_errors += 1

    def close(self, timeout_s: float = 1.0) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("OpenXR telemetry fanout did not stop")
        self._sock.close()


def _joint_records(
    positions: np.ndarray,
    orientations: np.ndarray,
    radii: np.ndarray,
    valid: np.ndarray,
) -> list[dict[str, object]]:
    records = []
    for index, (name, parent) in enumerate(zip(JOINT_NAMES, PARENT_IDS, strict=False)):
        position = positions[index] if bool(valid[index]) else np.zeros(3)
        orientation = orientations[index] if bool(valid[index]) else np.array([0.0, 0.0, 0.0, 1.0])
        radius = float(radii[index]) if math.isfinite(float(radii[index])) else 0.0
        records.append(
            {
                "id": index,
                "name": name,
                "parent": parent,
                "x": float(position[0]),
                "y": float(position[1]),
                "z": float(position[2]),
                "qx": float(orientation[0]),
                "qy": float(orientation[1]),
                "qz": float(orientation[2]),
                "qw": float(orientation[3]),
                "radius_m": max(0.0, radius),
            }
        )
    return records


def _frame_from_hand(data: dict[str, object], sequence: int, focused: bool) -> dict[str, object]:
    joints = data["joints"]
    positions = np.asarray(joints["position"], dtype=float)
    orientations = np.asarray(joints["orientation"], dtype=float)
    radii = np.asarray(joints["radius"], dtype=float)
    valid = np.asarray(joints["valid"], dtype=bool)
    pinch = data.get("pinch")
    pinch_m = float(pinch) if isinstance(pinch, (int, float)) and math.isfinite(float(pinch)) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "dex-teleop-openxr-hand",
        "mode": "real",
        "device": "Quest 3S",
        "runtime": "WiVRn",
        "session_running": True,
        "session_focused": bool(focused),
        "side": "left",
        "layout": LAYOUT_ID,
        "joint_count": len(JOINT_NAMES),
        "source_sequence": sequence,
        "sample_monotonic_ns": time.monotonic_ns(),
        "valid_mask": [bool(value) for value in valid],
        "pinch_m": pinch_m,
        "joints": _joint_records(positions, orientations, radii, valid),
    }


def run_real(args: argparse.Namespace) -> None:
    teleop_root = Path(args.teleop_root).resolve()
    if not (teleop_root / "main_new.py").is_file():
        raise FileNotFoundError(f"dex_teleop main_new.py is missing: {teleop_root}")
    sys.path.insert(0, str(teleop_root.parent))
    from dex_teleop.vr_utils.vr_hand_reader import VRHandReader

    fanout = LatestDatagramFanout(args.host, (args.hand_port, args.arm_port))
    reader = VRHandReader()
    reader.init()
    sequence = 0
    tracking = False
    try:
        while True:
            reader.poll_events()
            if reader.session_running:
                hands = reader.read_poses()
                left = hands.get("left") if hands else None
                if left is not None and left.get("wrist") is not None:
                    if not tracking:
                        print("[openxr] LEFT hand tracking active", flush=True)
                        tracking = True
                    fanout.offer(_frame_from_hand(left, sequence, reader.session_focused))
                    sequence += 1
                elif tracking:
                    print("[openxr] LEFT hand tracking lost", flush=True)
                    tracking = False
            elif tracking:
                print("[openxr] session stopped", flush=True)
                tracking = False
            time.sleep(0.005)
    finally:
        reader.shutdown()
        fanout.close()


def _fake_pose(phase: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    positions = np.zeros((26, 3), dtype=float)
    orientations = np.tile(np.array([0.0, 0.0, 0.0, 1.0]), (26, 1))
    radii = np.full(26, 0.008, dtype=float)
    valid = np.ones(26, dtype=bool)
    positions[1] = (0.05 * math.sin(phase * 0.25), 1.25, -0.45)
    positions[0] = positions[1] + (0.0, 0.055, -0.005)
    finger_roots = (2, 6, 11, 16, 21)
    finger_lengths = (4, 5, 5, 5, 5)
    x_offsets = (-0.045, -0.025, 0.0, 0.024, 0.046)
    curl = 0.22 + 0.18 * (0.5 + 0.5 * math.sin(phase))
    for root, count, x_offset in zip(finger_roots, finger_lengths, x_offsets, strict=False):
        base = positions[0] + (x_offset, 0.0, 0.0)
        for offset in range(count):
            distance = 0.025 * (offset + 1)
            positions[root + offset] = base + (
                0.0,
                distance * math.cos(curl * (offset + 1)),
                -distance * math.sin(curl * (offset + 1)),
            )
    return positions, orientations, radii, valid


def run_fake(args: argparse.Namespace) -> None:
    fanout = LatestDatagramFanout(args.host, (args.hand_port, args.arm_port))
    sequence = 0
    start = time.monotonic()
    period = 1.0 / args.fake_hz
    try:
        while True:
            phase = (time.monotonic() - start) * 1.4
            positions, orientations, radii, valid = _fake_pose(phase)
            pinch = float(np.linalg.norm(positions[5] - positions[10]))
            fanout.offer(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": "dex-teleop-openxr-hand",
                    "mode": "fake",
                    "device": "SYNTHETIC QUEST 3S",
                    "runtime": "SYNTHETIC WIVRN",
                    "session_running": True,
                    "session_focused": True,
                    "side": "left",
                    "layout": LAYOUT_ID,
                    "joint_count": 26,
                    "source_sequence": sequence,
                    "sample_monotonic_ns": time.monotonic_ns(),
                    "valid_mask": [True] * 26,
                    "pinch_m": pinch,
                    "joints": _joint_records(positions, orientations, radii, valid),
                }
            )
            sequence += 1
            time.sleep(period)
    finally:
        fanout.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--hand-port", type=int, default=8770)
    parser.add_argument("--arm-port", type=int, default=8771)
    parser.add_argument("--teleop-root", default="/home/user/dex_teleop")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--fake-hz", type=float, default=72.0)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("--host must be loopback")
    if not 1 <= args.hand_port <= 65535 or not 1 <= args.arm_port <= 65535:
        parser.error("UDP ports must be within 1..65535")
    if not 10.0 <= args.fake_hz <= 120.0:
        parser.error("--fake-hz must be within 10..120")
    return args


def main() -> int:
    args = parse_args()
    try:
        run_fake(args) if args.fake else run_real(args)
    except KeyboardInterrupt:
        print("\n[openxr] bridge stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
