from __future__ import annotations

import http.client
import json
from pathlib import Path
import re
import shutil
import threading
import time

import pytest

from dex_runtime.telemetry import TelemetryEnvelope, TelemetryHealth, TelemetryHub
from tools.control_console.server import ASSETS_DIR, make_console_server, verify_assets
from tools.control_console.telemetry import ConsoleTelemetryPump, SyntheticArmTelemetry
from tools.switch_web_demo import DemoController
from tools.switch_demo_backend import CONTROL_PERIOD_NS, _base_config
from policy_package_factory import write_test_package


class _Controller:
    def __init__(self) -> None:
        self.switch_count = 0
        self.stop_count = 0
        self.confirm_count = 0
        self.gateway = type(
            "Gateway",
            (),
            {"config": type("Config", (), {"state_stale_ns": 100_000_000})()},
        )()

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "RL_SHADOW",
            "session_id": "test-session",
            "hand_owner": "teleoperation",
            "control_epoch": 2,
            "history": "30/30",
            "readiness_ready": True,
            "readiness_providers": [
                {
                    "provider_id": "operator-confirmation-v1",
                    "result": "operator-confirmed",
                    "valid": True,
                    "reason_codes": [],
                },
                {
                    "provider_id": "gateway-health-v1",
                    "result": "pass",
                    "valid": True,
                    "reason_codes": [],
                },
            ],
            "readiness_blocking_reasons": [],
            "rejection_reason": None,
            "switchable": True,
            "switch_gate": "ready",
            "arm_hold_ready": True,
            "switch_block_reason": None,
            "connected": True,
            "confirmed": True,
            "stopped": False,
            "fault": None,
            "message": "Ready for a switch request.",
            "policy_name": "test-policy",
            "logs_path": "/tmp/test-logs",
        }

    def linker_snapshot(self) -> dict[str, object]:
        now_ns = time.monotonic_ns()
        return {
            "connected": True,
            "health": "healthy",
            "fault": None,
            "sample_monotonic_ns": now_ns,
            "received_monotonic_ns": now_ns,
            "rate_hz": 50.0,
            "state_sequence": 12,
            "owner": "teleoperation",
            "control_epoch": 2,
            "acknowledgement": "SENT_TO_BUS",
            "command_identity_match": True,
            "authorized_command_id": "command-12",
            "acknowledged_command_id": "command-12",
            "effective_command_id": "command-12",
            "maximum_error_rad": 0.01,
            "rms_error_rad": 0.01,
            "stale_after_ns": 250_000_000,
            "native_mapping": {
                "mapping_id": "mapping-test",
                "native_arc": [100.0] * 20,
                "saturated_joints": [],
            },
            "joints": [
                {
                    "index": index,
                    "name": f"joint_{index:02d}",
                    "measured": 0.2,
                    "requested_target": 0.22,
                    "authorized_target": 0.215,
                    "effective_target": 0.21,
                    "lower": -1.0,
                    "upper": 1.0,
                }
                for index in range(16)
            ],
        }

    def do_switch(self) -> dict[str, object]:
        self.switch_count += 1
        return {"ok": True, "message": "Switch request accepted."}

    def do_confirm(self) -> dict[str, object]:
        self.confirm_count += 1
        return {"ok": True, "message": "Operator confirmation refreshed."}

    def do_stop(self) -> dict[str, object]:
        self.stop_count += 1
        return {"ok": True, "message": "Stop request accepted."}


class _OpenXR:
    def snapshot(self) -> dict[str, object]:
        now_ns = time.monotonic_ns()
        return {
            "connected": True,
            "mode": "fake",
            "control_correlated": False,
            "side": "left",
            "source_sequence": 7,
            "sample_monotonic_ns": now_ns,
            "received_monotonic_ns": now_ns,
            "rate_hz": 30.0,
            "dropped_since_last": 0,
            "nodes": [
                {"id": 0, "parent": -1, "x": 0.0, "y": 0.0, "z": 0.0},
                {"id": 1, "parent": 0, "x": 0.01, "y": 0.0, "z": 0.03},
            ],
        }


def _runtime_envelope() -> TelemetryEnvelope:
    now_ns = time.monotonic_ns()
    return TelemetryEnvelope(
        source="runtime",
        sequence=0,
        sample_monotonic_ns=now_ns,
        received_monotonic_ns=now_ns,
        rate_hz=20.0,
        dropped_since_last=0,
        health=TelemetryHealth.HEALTHY,
        payload={"state": "RL_SHADOW", "switchable": True},
        stale_after_ns=250_000_000,
    )


def test_console_assets_are_english_only_offline_and_digest_verified() -> None:
    verify_assets()
    html = (ASSETS_DIR / "index.html").read_text()
    css = (ASSETS_DIR / "app.css").read_text()
    javascript = (ASSETS_DIR / "app.js").read_text()

    assert '<html lang="en">' in html
    assert "DEX CONTROL / VR LIVE" in html
    assert not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", html + css + javascript)
    assert "system-ui" not in css
    assert "PingFang" not in css
    assert "font-family: \"Dex UI\"" in css
    assert 'id="readiness-providers"' in html
    assert 'id="camera-rgb"' in html
    assert 'id="camera-depth"' in html
    assert 'id="switch-gate-status"' in html
    assert '"gateway-health-v1": "GATEWAY"' in javascript
    assert "RL SWITCH DISABLED" in javascript
    assert "WAITING FOR ARM HOLD" in javascript
    assert "ARM HOLD READY" in javascript
    assert 'readiness.textContent = "4 / 4"' not in javascript
    network_text = (html + css + javascript).replace(
        "http://www.w3.org/2000/svg",
        "",
    )
    assert "https://" not in network_text
    assert "http://" not in network_text


def test_live_demo_command_deadline_matches_one_control_period(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "package-root")
    config = _base_config(tmp_path, package)
    assert config["safety"]["command_deadline_ns"] == CONTROL_PERIOD_NS


def test_real_arm_monitoring_rejects_switch_in_backend() -> None:
    controller = DemoController.__new__(DemoController)
    controller.allow_switch = False
    controller.switch_block_reason = (
        "MONITORING ONLY: RL switch disabled because real Hitbot arm hold is not integrated."
    )
    controller._lock = threading.Lock()
    controller._message = ""

    result = controller.do_switch()

    assert result == {"ok": False, "message": controller.switch_block_reason}
    assert controller._switchable(type("Status", (), {"state": "RL_SHADOW"})()) is False
    assert controller._message == controller.switch_block_reason


def test_live_arm_switch_requires_a_fresh_hold_controller_probe() -> None:
    class _ArmGateway:
        def __init__(self) -> None:
            self.available = False

        def probe(self) -> bool:
            return self.available

    arm_gateway = _ArmGateway()
    controller = DemoController.__new__(DemoController)
    controller.allow_switch = True
    controller.switch_block_reason = None
    controller.require_arm_hold_controller = True
    controller._arm_available = False
    controller._last_arm_probe = 0.0
    controller.runtime = type("Runtime", (), {"arm_gateway": arm_gateway})()
    controller._lock = threading.Lock()
    controller._message = ""
    controller._status = type("Status", (), {"state": "RL_SHADOW"})()
    controller._pending_stop = False
    controller._confirmed = True

    rejected = controller.do_switch()
    assert rejected["ok"] is False
    assert "not connected" in rejected["message"]
    assert controller._switchable(controller._status) is False

    arm_gateway.available = True
    assert controller._maybe_probe_arm(force=True) is True
    assert controller._switchable(controller._status) is True


@pytest.mark.parametrize(
    ("allow_switch", "arm_required", "arm_available", "state", "expected"),
    [
        (False, True, False, "RL_SHADOW", "disabled"),
        (True, True, False, "RL_SHADOW", "waiting-arm-hold"),
        (True, True, True, "RL_SHADOW", "ready"),
        (True, False, True, "RL_ACTIVE", "ready"),
        (True, True, True, "SAFE_HOLD", "state-unavailable"),
    ],
)
def test_rl_switch_gate_distinguishes_authorization_and_arm_hold(
    allow_switch: bool,
    arm_required: bool,
    arm_available: bool,
    state: str,
    expected: str,
) -> None:
    controller = DemoController.__new__(DemoController)
    controller.allow_switch = allow_switch
    controller.require_arm_hold_controller = arm_required
    controller._arm_available = arm_available
    status = type("Status", (), {"state": state})()

    assert controller._switch_gate(status, pending_stop=False, stopped=False) == expected


def test_console_asset_verifier_rejects_modified_font(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS_DIR, assets)
    font = assets / "fonts" / "ui-regular.woff2"
    font.write_bytes(font.read_bytes() + b"modified")
    with pytest.raises(RuntimeError, match="font digest mismatch"):
        verify_assets(assets)


def test_fake_telemetry_pump_publishes_all_five_sources() -> None:
    hub = TelemetryHub()
    controller = _Controller()
    pump = ConsoleTelemetryPump(
        hub,
        controller=controller,
        vr=_OpenXR(),
        arm=SyntheticArmTelemetry(),
    )
    pump.publish_once()

    snapshot = hub.snapshot()
    assert set(snapshot["sources"]) == {"runtime", "openxr", "linker", "hitbot", "d435"}
    assert len(snapshot["sources"]["linker"]["payload"]["joints"]) == 16
    first_joint = snapshot["sources"]["linker"]["payload"]["joints"][0]
    assert first_joint["requested_target"] == 0.22
    assert first_joint["authorized_target"] == 0.215
    assert first_joint["effective_target"] == 0.21
    assert first_joint["measured"] == 0.2
    assert len(snapshot["sources"]["linker"]["payload"]["native_mapping"]["native_arc"]) == 20
    assert snapshot["sources"]["openxr"]["payload"]["control_correlated"] is False
    assert snapshot["sources"]["hitbot"]["payload"]["mode"] == "synthetic"
    assert snapshot["sources"]["d435"]["payload"]["mode"] == "off"
    assert len(snapshot["sources"]["openxr"]["payload"]["latency_history_ms"]) == 1
    assert len(snapshot["sources"]["linker"]["payload"]["latency_history_ms"]) == 1
    assert len(snapshot["sources"]["hitbot"]["payload"]["latency_history_ms"]) == 1


def test_latency_history_is_bounded_and_reconstructable_from_one_snapshot() -> None:
    clock = [time.monotonic_ns()]
    hub = TelemetryHub(clock_ns=lambda: clock[0])
    pump = ConsoleTelemetryPump(
        hub,
        controller=_Controller(),
        vr=_OpenXR(),
        arm=SyntheticArmTelemetry(clock_ns=lambda: clock[0]),
        display_hz=2.0,
        clock_ns=lambda: clock[0],
    )
    for _ in range(25):
        pump.publish_once()
        clock[0] += 500_000_000

    snapshot = hub.snapshot(now_ns=clock[0])
    for source in ("openxr", "linker", "hitbot"):
        history = snapshot["sources"][source]["payload"]["latency_history_ms"]
        assert len(history) == 20
        assert all(isinstance(value, (int, float)) for value in history)


def test_command_identity_mismatch_degrades_only_the_linker_source() -> None:
    class _MismatchController(_Controller):
        def linker_snapshot(self) -> dict[str, object]:
            payload = super().linker_snapshot()
            payload["command_identity_match"] = False
            return payload

    hub = TelemetryHub()
    pump = ConsoleTelemetryPump(
        hub,
        controller=_MismatchController(),
        vr=_OpenXR(),
        arm=SyntheticArmTelemetry(),
    )
    pump.publish_once()

    sources = hub.snapshot()["sources"]
    assert sources["linker"]["health"] == "degraded"
    assert sources["runtime"]["health"] == "healthy"


@pytest.mark.parametrize(
    ("patch", "expected_health"),
    [
        ({"epoch_match": False, "health": "degraded"}, "degraded"),
        ({"connected": False, "health": "stale"}, "stale"),
        ({"fault": "gateway-fault", "health": "fault"}, "fault"),
    ],
)
def test_linker_epoch_stale_and_fault_states_are_source_local(
    patch: dict[str, object], expected_health: str
) -> None:
    class _CorrelationController(_Controller):
        def linker_snapshot(self) -> dict[str, object]:
            payload = super().linker_snapshot()
            payload.update(patch)
            return payload

    hub = TelemetryHub()
    pump = ConsoleTelemetryPump(
        hub,
        controller=_CorrelationController(),
        vr=_OpenXR(),
        arm=None,
    )
    pump.publish_once()

    sources = hub.snapshot()["sources"]
    assert sources["linker"]["health"] == expected_health
    assert sources["runtime"]["health"] == "healthy"


def test_exact_openxr_snapshot_preserves_degraded_validation_state() -> None:
    class _ExactController(_Controller):
        def vr_control_snapshot(self) -> dict[str, object]:
            now_ns = time.monotonic_ns()
            return {
                "connected": False,
                "mode": "real",
                "control_correlated": True,
                "drives_current_command": True,
                "source_sequence": 19,
                "candidate_source_sequence": 19,
                "sample_monotonic_ns": now_ns,
                "received_monotonic_ns": now_ns,
                "rate_hz": 60.0,
                "dropped_since_last": 0,
                "source_health": "degraded",
                "source_reason": "layout-mismatch",
                "nodes": [
                    {"id": 0, "parent": -1, "x": 0.0, "y": 0.0, "z": 0.0},
                    {"id": 1, "parent": 0, "x": 0.01, "y": 0.0, "z": 0.03},
                ],
            }

    hub = TelemetryHub()
    pump = ConsoleTelemetryPump(
        hub,
        controller=_ExactController(),
        vr=_OpenXR(),
        arm=None,
    )
    pump.publish_once()
    openxr = hub.snapshot()["sources"]["openxr"]
    assert openxr["health"] == "degraded"
    assert openxr["payload"]["control_correlated"] is True
    assert openxr["payload"]["source_reason"] == "layout-mismatch"


def test_http_server_serves_static_snapshot_sse_and_existing_actions() -> None:
    controller = _Controller()
    hub = TelemetryHub()
    hub.publish(_runtime_envelope())
    server = make_console_server("127.0.0.1", 0, controller=controller, hub=hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        assert response.status == 200
        assert response.getheader("Content-Security-Policy")
        assert "DEX CONTROL / VR LIVE" in html
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("POST", "/api/confirm", body=b"")
        response = connection.getresponse()
        result = json.loads(response.read())
        assert result["ok"] is True
        assert controller.confirm_count == 1
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/assets/fonts/ui-regular.woff2")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "font/woff2"
        assert "immutable" in response.getheader("Cache-Control")
        assert response.read()
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/snapshot")
        response = connection.getresponse()
        snapshot = json.loads(response.read())
        assert response.status == 200
        assert snapshot["sources"]["runtime"]["payload"]["state"] == "RL_SHADOW"
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/live")
        response = connection.getresponse()
        assert response.status == 200
        lines = [response.readline().decode().strip() for _ in range(3)]
        assert lines[0].startswith("id: ")
        assert lines[1] == "event: snapshot"
        assert lines[2].startswith("data: {")
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/live", headers={"Last-Event-ID": "0"})
        response = connection.getresponse()
        assert response.status == 200
        reconnect_lines = [response.readline().decode().strip() for _ in range(3)]
        assert reconnect_lines[1] == "event: snapshot"
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/vr")
        response = connection.getresponse()
        openxr = json.loads(response.read())
        assert response.status == 200
        assert openxr == {"connected": False, "nodes": []}
        connection.close()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request("POST", "/api/switch", body=b"")
        response = connection.getresponse()
        result = json.loads(response.read())
        assert result["ok"] is True
        assert controller.switch_count == 1
        connection.close()
    finally:
        server.console_stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2.0)

    assert not thread.is_alive()
