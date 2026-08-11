from __future__ import annotations

import http.client
import sys
import threading
import time

from dex_runtime.telemetry import TelemetryHub
from tools.control_console.camera_source import RealSenseD435Source, SyntheticD435Source
from tools.control_console.server import make_console_server
from tools.control_console.telemetry import ConsoleTelemetryPump


class _Controller:
    gateway = type(
        "Gateway",
        (),
        {"config": type("Config", (), {"state_stale_ns": 250_000_000})()},
    )()

    def snapshot(self) -> dict[str, object]:
        return {"connected": True, "stopped": False, "state": "RL_SHADOW"}

    def linker_snapshot(self) -> dict[str, object]:
        now_ns = time.monotonic_ns()
        return {
            "connected": True,
            "health": "healthy",
            "sample_monotonic_ns": now_ns,
            "received_monotonic_ns": now_ns,
            "joints": [],
        }

    def do_confirm(self) -> dict[str, object]:
        return {"ok": True}

    def do_switch(self) -> dict[str, object]:
        return {"ok": True}

    def do_stop(self) -> dict[str, object]:
        return {"ok": True}


def _started_camera() -> SyntheticD435Source:
    camera = SyntheticD435Source(fps=10)
    camera.start()
    frame = camera.wait_for_frame("rgb", -1, 3.0)
    assert frame is not None
    return camera


def test_synthetic_camera_has_bounded_rgb_depth_frames_and_truthful_metadata() -> None:
    camera = _started_camera()
    try:
        rgb = camera.wait_for_frame("rgb", -1, 1.0)
        depth = camera.wait_for_frame("depth", -1, 1.0)
        assert rgb is not None and rgb[1].startswith(b"\xff\xd8")
        assert depth is not None and depth[1].startswith(b"\xff\xd8")
        snapshot = camera.snapshot()
        assert snapshot["source_health"] == "healthy"
        assert snapshot["mode"] == "synthetic"
        assert snapshot["device_model"] == "Intel RealSense D435"
        assert snapshot["color_width"] == 960
        assert snapshot["depth_width"] == 480
    finally:
        camera.stop()


def test_camera_telemetry_is_a_fifth_source_and_mjpeg_is_streamed() -> None:
    camera = _started_camera()
    controller = _Controller()
    hub = TelemetryHub()
    pump = ConsoleTelemetryPump(hub, controller=controller, camera=camera)
    pump.publish_once()
    assert hub.snapshot()["sources"]["d435"]["health"] == "healthy"

    server = make_console_server(
        "127.0.0.1",
        0,
        controller=controller,
        hub=hub,
        camera=camera,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        connection = http.client.HTTPConnection(host, port, timeout=3)
        connection.request("GET", "/api/camera/rgb.mjpg")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("multipart/x-mixed-replace")
        assert response.readline().startswith(b"--dex-camera-frame")
        connection.close()
    finally:
        server.console_stop.set()
        server.shutdown()
        server.server_close()
        camera.stop()


def test_real_camera_sdk_fault_is_isolated_in_a_child_process() -> None:
    camera = RealSenseD435Source()
    camera._worker_command_override = [
        sys.executable,
        "-c",
        (
            "from multiprocessing.connection import Connection; import sys; "
            "connection=Connection(sys.stdout.fileno(), readable=False, writable=True); "
            "connection.send(('fault', 'isolated-test-fault'))"
        ),
    ]
    camera.start()
    try:
        deadline = time.monotonic() + 3.0
        while camera.snapshot()["source_health"] != "fault" and time.monotonic() < deadline:
            time.sleep(0.02)
        snapshot = camera.snapshot()
        assert snapshot["source_health"] == "fault"
        assert snapshot["fault"] == "isolated-test-fault"
    finally:
        camera.stop()


def test_blocked_real_camera_process_has_bounded_shutdown() -> None:
    camera = RealSenseD435Source()
    camera._worker_command_override = [
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    ]
    camera.start()
    time.sleep(0.2)
    started = time.monotonic()
    camera.stop()
    assert time.monotonic() - started < 2.75


def test_real_camera_worker_matches_verified_host_capture_contract() -> None:
    camera = RealSenseD435Source(
        serial="144223022813",
        camera_python="/known-good/python",
    )
    command = camera._worker_command()
    assert command[0] == "/known-good/python"
    assert command[1] == "-u"
    assert command[2].endswith("/tools/control_console/realsense_worker.py")
    assert command[command.index("--width") + 1] == "640"
    assert command[command.index("--height") + 1] == "480"
    assert command[command.index("--fps") + 1] == "30"
    assert command[command.index("--serial") + 1] == "144223022813"
    assert camera.snapshot()["capture_python"] == "/known-good/python"
