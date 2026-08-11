from __future__ import annotations

from pathlib import Path

from tools.control_console.soak_verify import build_fake_command, summarize


def test_soak_command_is_hardware_forbidden() -> None:
    command = build_fake_command(
        "/venv/python",
        Path("/repo"),
        http_port=8000,
        openxr_port=8001,
        arm_port=8002,
    )
    pairs = dict(zip(command[2::2], command[3::2]))
    assert pairs["--transport"] == "fake"
    assert pairs["--policy"] == "synthetic"
    assert pairs["--vr"] == "fake"
    assert pairs["--vr-python"] == "/venv/python"
    assert pairs["--arm-telemetry"] == "fake"
    assert pairs["--camera"] == "fake"
    assert "hand" not in command
    assert "real" not in command
    assert "/api/switch" not in Path("tools/control_console/soak_verify.py").read_text()
    assert "/api/stop" not in Path("tools/control_console/soak_verify.py").read_text()


def test_timing_summary_is_deterministic() -> None:
    assert summarize([]) == {
        "count": 0,
        "mean_ms": None,
        "p95_ms": None,
        "max_ms": None,
    }
    assert summarize([0.1, 0.2, 0.3, 0.4]) == {
        "count": 4,
        "mean_ms": 0.25,
        "p95_ms": 0.4,
        "max_ms": 0.4,
    }
