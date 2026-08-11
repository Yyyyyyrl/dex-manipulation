from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
import re
import subprocess
import sys
import threading

import pytest


LAUNCHER = Path("tools/start_live_ui.sh")


def _snapshot_server(snapshot: dict) -> HTTPServer:
    body = json.dumps(snapshot).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path != "/api/snapshot":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return HTTPServer(("127.0.0.1", 0), Handler)


def _run_startup_gate(snapshot: dict, *, enable_rl_switch: str) -> int:
    """Run the launcher's own readiness gate against a canned UI snapshot."""

    match = re.search(
        r"^hitbot_startup_ready\(\) \{.*?^\}", LAUNCHER.read_text(), re.S | re.M
    )
    assert match is not None, "launcher no longer defines hitbot_startup_ready"
    server = _snapshot_server(snapshot)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = "\n".join(
            [
                "set -uo pipefail",
                match.group(0),
                f'UI_URL="http://127.0.0.1:{server.server_address[1]}"',
                f'UI_PYTHON="{sys.executable}"',
                f'ENABLE_RL_SWITCH="{enable_rl_switch}"',
                "hitbot_startup_ready",
            ]
        )
        return subprocess.run(["bash", "-c", script], check=False).returncode
    finally:
        server.shutdown()
        server.server_close()


def _snapshot(*, hitbot_payload: dict, runtime_payload: dict) -> dict:
    return {
        "sources": {
            "hitbot": {"health": "healthy", "payload": hitbot_payload},
            "runtime": {"payload": runtime_payload},
        }
    }


_HEALTHY_CYCLE = {
    "connected": True,
    "ik_ok": True,
    "servo_ok": True,
    "hold_state": "TELEOP",
    "source_reason": "",
}
_NOT_SWITCHABLE = {
    "switchable": False,
    "switch_gate": "waiting-arm-hold",
    "switch_block_reason": (
        "RL switch unavailable: verified Hitbot hold controller is not connected."
    ),
}
_SWITCHABLE = {"switchable": True, "switch_gate": "ready", "switch_block_reason": None}


@pytest.mark.parametrize(
    ("hitbot_payload", "runtime_payload", "enable_rl_switch", "ready"),
    [
        # The first SDK cycle can land before the console's 0.5s hold probe has
        # published `switchable`; startup must keep polling instead of aborting.
        (_HEALTHY_CYCLE, _NOT_SWITCHABLE, "1", False),
        (_HEALTHY_CYCLE, _SWITCHABLE, "1", True),
        (_HEALTHY_CYCLE, _NOT_SWITCHABLE, "0", True),
        # Heartbeats alone keep the source healthy but never prove a real cycle.
        ({"connected": True, "source_reason": "waiting-for-cycle"}, _SWITCHABLE, "1", False),
    ],
)
def test_live_ui_launcher_startup_gate_matches_final_assertion(
    hitbot_payload: dict, runtime_payload: dict, enable_rl_switch: str, ready: bool
) -> None:
    returncode = _run_startup_gate(
        _snapshot(hitbot_payload=hitbot_payload, runtime_payload=runtime_payload),
        enable_rl_switch=enable_rl_switch,
    )
    assert (returncode == 0) is ready


def test_live_ui_launcher_has_valid_shell_and_help() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        ["bash", str(LAUNCHER), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "RL_SHADOW" in help_result.stdout
    assert "UI hand-back/re-anchor -> Hitbot controller" in help_result.stdout


def test_live_ui_launcher_preserves_hardware_safety_boundaries() -> None:
    source = LAUNCHER.read_text()
    assert "Type CONFIRM to continue" in source
    assert "--policy synthetic" in source
    assert "/api/switch" not in source
    assert 'curl -fsS --max-time 2 -X POST "$UI_URL/api/stop"' in source
    assert "[v]r_hitbot_controller.py" in source
    assert "DEX_ARM_TELEMETRY_PORT" in source
    assert "DEX_ARM_HOLD_PORT" in source
    assert "--arm-hold-port" in source
    assert "--camera d435" in source
    assert '--camera-python "$CAMERA_PYTHON"' in source
    assert 'DEX_CAMERA_PYTHON:-/home/user/miniconda3/bin/python' in source
    assert "import cv2, numpy, pyrealsense2" in source
    assert "install_d435_udev.sh" in source
    assert "--enable-real-arm-hold-switch" in source
    assert "Type ENABLE RL to continue" in source
    assert "--transport hand" in source
    assert "tools/vr_hitbot_controller.py" in source
    assert "tools/openxr_hand_bridge.py" not in source
    assert "--vr real" in source
    assert 'DEX_FLATPAK_BIN:-flatpak' in source
    assert 'WIVRN_APP_ID="io.github.wivrn.wivrn"' in source
    assert 'WIVRN_REF="${WIVRN_APP_ID}//stable"' in source
    assert 'run "$WIVRN_REF"' in source
    assert "flatpak install flathub" in source
    assert "--no-wivrn" in source
    assert "main_new.py" in source
    assert "VRHandReader is missing" in source
    assert "Manus glove battery is critical" not in source
    assert "manus_ws/install/local_setup.bash" not in source
    assert "Hitbot reports Axis 1 drive fault" in source
    assert "fail_hitbot_startup" in source
    assert "refused the SDK connection" in source
    assert 'kill -INT -- "$target"' in source
    assert "request_ui_stop" in source
    assert "keep the arm owner alive long enough" in source
    assert "UI hand-back/stop did not complete within 10 seconds" in source
