"""Read-only Intel RealSense D435 RGB/depth sources for the live console.

The camera worker owns only the RealSense pipeline and a bounded latest-frame
buffer.  It has no reference to the robot runtime or any command endpoint, so a
camera failure remains isolated from hand/arm control.
"""

from __future__ import annotations

import math
from multiprocessing.connection import Connection
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


class _LatestCameraSource:
    """Thread-safe metadata plus one latest JPEG per stream."""

    def __init__(self, *, mode: str, stale_after_ns: int = 1_000_000_000) -> None:
        self.mode = mode
        self.stale_after_ns = stale_after_ns
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: dict[str, tuple[int, bytes]] = {}
        self._sequence = 0
        self._sample_ns = 0
        self._received_ns = 0
        self._rate_hz = 0.0
        self._last_frame_ns = 0
        self._connected = False
        self._fault: str | None = None
        self._device_model = "Intel RealSense D435" if mode == "synthetic" else None
        self._serial: str | None = "SYNTHETIC" if mode == "synthetic" else None
        self._color_size: tuple[int, int] | None = None
        self._depth_size: tuple[int, int] | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("camera source already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"camera-{self.mode}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise TimeoutError("camera source did not stop")

    def _set_device(self, model: str, serial: str | None) -> None:
        with self._condition:
            self._device_model = model
            self._serial = serial

    def _set_fault(self, message: str) -> None:
        with self._condition:
            self._connected = False
            self._fault = message
            self._condition.notify_all()

    def _publish(
        self,
        rgb_jpeg: bytes,
        depth_jpeg: bytes,
        *,
        color_size: tuple[int, int],
        depth_size: tuple[int, int],
        sample_ns: int | None = None,
    ) -> None:
        received_ns = time.monotonic_ns()
        sample_ns = received_ns if sample_ns is None else sample_ns
        with self._condition:
            if self._last_frame_ns:
                interval_s = (received_ns - self._last_frame_ns) / 1_000_000_000
                if interval_s > 0:
                    instant_hz = 1.0 / interval_s
                    self._rate_hz = (
                        instant_hz
                        if self._rate_hz <= 0
                        else self._rate_hz * 0.85 + instant_hz * 0.15
                    )
            self._last_frame_ns = received_ns
            self._sequence += 1
            self._sample_ns = sample_ns
            self._received_ns = received_ns
            self._color_size = color_size
            self._depth_size = depth_size
            self._frames["rgb"] = (self._sequence, rgb_jpeg)
            self._frames["depth"] = (self._sequence, depth_jpeg)
            self._connected = True
            self._fault = None
            self._condition.notify_all()

    def wait_for_frame(
        self,
        kind: str,
        after_sequence: int,
        timeout_s: float,
    ) -> tuple[int, bytes] | None:
        if kind not in ("rgb", "depth"):
            raise ValueError("camera frame kind must be 'rgb' or 'depth'")
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while not self._stop.is_set():
                frame = self._frames.get(kind)
                if frame is not None and frame[0] > after_sequence:
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def snapshot(self) -> dict[str, object]:
        now_ns = time.monotonic_ns()
        with self._condition:
            connected = self._connected
            fault = self._fault
            sequence = self._sequence
            sample_ns = self._sample_ns
            received_ns = self._received_ns
            rate_hz = self._rate_hz
            model = self._device_model
            serial = self._serial
            color_size = self._color_size
            depth_size = self._depth_size
            rgb_available = "rgb" in self._frames
            depth_available = "depth" in self._frames
        stale = not received_ns or now_ns - received_ns > self.stale_after_ns
        if fault:
            health = "fault"
            reason = fault
        elif not connected or stale:
            health = "stale"
            reason = "waiting-for-camera-frame" if not received_ns else "camera-frame-stale"
        else:
            health = "healthy"
            reason = None
        return {
            "connected": connected and not stale and fault is None,
            "mode": self.mode,
            "device_model": model,
            "serial": serial,
            "source_sequence": sequence,
            "sample_monotonic_ns": sample_ns,
            "received_monotonic_ns": received_ns,
            "rate_hz": rate_hz,
            "stale_after_ns": self.stale_after_ns,
            "source_health": health,
            "source_reason": reason,
            "fault": fault,
            "rgb_available": rgb_available,
            "depth_available": depth_available,
            "color_width": None if color_size is None else color_size[0],
            "color_height": None if color_size is None else color_size[1],
            "depth_width": None if depth_size is None else depth_size[0],
            "depth_height": None if depth_size is None else depth_size[1],
        }

    def _run(self) -> None:
        raise NotImplementedError


class SyntheticD435Source(_LatestCameraSource):
    """Clearly labelled RGB/depth preview for offline UI verification."""

    def __init__(self, *, fps: int = 15) -> None:
        super().__init__(mode="synthetic")
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.fps = fps

    def _run(self) -> None:
        try:
            import cv2
            import numpy as np

            period_s = 1.0 / self.fps
            started = time.monotonic()
            while not self._stop.is_set():
                phase = time.monotonic() - started
                width, height = 960, 540
                image = np.zeros((height, width, 3), dtype=np.uint8)
                image[:] = (18, 25, 31)
                for x in range(0, width, 80):
                    cv2.line(image, (x, 0), (x, height), (35, 48, 57), 1)
                for y in range(0, height, 60):
                    cv2.line(image, (0, y), (width, y), (35, 48, 57), 1)
                cx = int(width * 0.55 + 75 * math.sin(phase * 0.7))
                cy = int(height * 0.52 + 35 * math.cos(phase * 0.5))
                cv2.rectangle(image, (cx - 145, cy - 80), (cx + 145, cy + 80), (50, 74, 91), -1)
                cv2.circle(image, (cx, cy), 58, (43, 125, 164), -1)
                cv2.line(image, (width // 2 - 22, height // 2), (width // 2 + 22, height // 2), (86, 212, 255), 2)
                cv2.line(image, (width // 2, height // 2 - 22), (width // 2, height // 2 + 22), (86, 212, 255), 2)
                cv2.putText(image, "SYNTHETIC D435 PREVIEW", (34, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.82, (86, 212, 255), 2, cv2.LINE_AA)
                cv2.putText(image, "NO PHYSICAL CAMERA", (34, 79), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (247, 185, 85), 1, cv2.LINE_AA)

                depth_width, depth_height = 480, 270
                xx = np.linspace(0, 255, depth_width, dtype=np.uint8)
                depth_gray = np.tile(xx, (depth_height, 1))
                depth_gray = np.roll(depth_gray, int(phase * 18) % depth_width, axis=1)
                depth = cv2.applyColorMap(depth_gray, cv2.COLORMAP_TURBO)
                cv2.circle(depth, (int(cx / 2), int(cy / 2)), 44, (255, 210, 38), -1)
                cv2.putText(depth, "DEPTH / SYNTHETIC", (14, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 1, cv2.LINE_AA)
                rgb_ok, rgb_encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
                depth_ok, depth_encoded = cv2.imencode(".jpg", depth, [cv2.IMWRITE_JPEG_QUALITY, 84])
                if not rgb_ok or not depth_ok:
                    raise RuntimeError("OpenCV failed to encode synthetic camera frame")
                self._publish(
                    rgb_encoded.tobytes(),
                    depth_encoded.tobytes(),
                    color_size=(width, height),
                    depth_size=(depth_width, depth_height),
                )
                self._stop.wait(period_s)
        except BaseException as exc:  # source-local fault surfaced in telemetry
            self._set_fault(f"{type(exc).__name__}: {exc}")


class RealSenseD435Source(_LatestCameraSource):
    """Live D435 source whose native SDK is isolated in an external process."""

    def __init__(
        self,
        *,
        serial: str | None = None,
        fps: int = 30,
        camera_python: str | None = None,
    ) -> None:
        super().__init__(mode="real")
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.requested_serial = serial
        self.fps = fps
        self.camera_python = camera_python or sys.executable
        self._worker_command_override: list[str] | None = None

    def snapshot(self) -> dict[str, object]:
        payload = super().snapshot()
        payload["capture_python"] = self.camera_python
        return payload

    def _worker_command(self) -> list[str]:
        if self._worker_command_override is not None:
            return self._worker_command_override
        worker_path = Path(__file__).with_name("realsense_worker.py")
        command = [
            self.camera_python,
            "-u",
            str(worker_path),
            "--width",
            "640",
            "--height",
            "480",
            "--fps",
            str(self.fps),
        ]
        if self.requested_serial:
            command.extend(("--serial", self.requested_serial))
        return command

    @staticmethod
    def _process_error(process: subprocess.Popen[bytes]) -> str:
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read()
            except OSError:
                pass
        detail = stderr.decode("utf-8", errors="replace").strip()
        if detail:
            return detail[-2000:]
        return f"camera capture process exited with code {process.returncode}"

    def _run(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(repo_root)
            if not existing_pythonpath
            else f"{repo_root}{os.pathsep}{existing_pythonpath}"
        )
        try:
            process = subprocess.Popen(
                self._worker_command(),
                cwd=repo_root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except BaseException as exc:
            self._set_fault(f"{type(exc).__name__}: {exc}")
            return
        assert process.stdout is not None
        connection = Connection(
            os.dup(process.stdout.fileno()),
            readable=True,
            writable=False,
        )
        try:
            while not self._stop.is_set():
                if not connection.poll(0.25):
                    if process.poll() is not None:
                        self._set_fault(self._process_error(process))
                        break
                    continue
                try:
                    message = connection.recv()
                except (EOFError, OSError):
                    process.wait(timeout=1.0)
                    self._set_fault(self._process_error(process))
                    break
                if message[0] == "fault":
                    self._set_fault(str(message[1]))
                    break
                if message[0] != "frame":
                    self._set_fault("camera capture process sent an invalid message")
                    break
                _, model, serial, rgb_jpeg, depth_jpeg, color_size, depth_size, sample_ns = message
                self._set_device(str(model), str(serial))
                self._publish(
                    rgb_jpeg,
                    depth_jpeg,
                    color_size=color_size,
                    depth_size=depth_size,
                    sample_ns=sample_ns,
                )
        except BaseException as exc:
            self._set_fault(f"{type(exc).__name__}: {exc}")
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
            connection.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
