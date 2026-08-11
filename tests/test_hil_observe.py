from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools.control_console.hil_observe import snapshot_url, validate_snapshot


def _live_snapshot() -> dict[str, object]:
    providers = [
        {
            "provider_id": "operator-confirmation-v1",
            "result": "operator-confirmed",
            "valid": True,
        },
        {"provider_id": "gateway-health-v1", "result": "pass", "valid": True},
    ]
    return {
        "revision": 12,
        "sources": {
            "runtime": {
                "health": "healthy",
                "age_ms": 2.0,
                "payload": {
                    "state": "RL_SHADOW",
                    "readiness_ready": True,
                    "readiness_providers": providers,
                },
            },
            "openxr": {
                "health": "healthy",
                "age_ms": 4.0,
                "payload": {
                    "mode": "real",
                    "layout": "openxr-hand-26-v1",
                    "side": "left",
                    "nodes": [{} for _ in range(26)],
                    "valid_joint_count": 26,
                    "session_focused": True,
                    "control_correlated": True,
                    "source_sequence": 91,
                    "candidate_source_sequence": 91,
                },
            },
            "linker": {
                "health": "healthy",
                "age_ms": 8.0,
                "payload": {
                    "joints": [{} for _ in range(16)],
                    "epoch_match": True,
                    "acknowledgement_missing": False,
                    "command_identity_match": True,
                    "gateway_rate_hz": 50.0,
                    "control_sample_sequence": 91,
                    "candidate_source_sequence": 91,
                },
            },
            "hitbot": {
                "health": "healthy",
                "age_ms": 10.0,
                "payload": {
                    "mode": "live",
                    "connected": True,
                    "cycle_success": True,
                    "tracker_pose": [0.0] * 7,
                    "tcp_actual": [0.0] * 6,
                    "tcp_target": [0.0] * 6,
                    "ik_result": [0.0] * 6,
                },
            },
        },
    }


def test_hil_observer_accepts_only_loopback_server_base_urls() -> None:
    assert snapshot_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765/api/snapshot"
    assert snapshot_url("http://localhost/") == "http://localhost:80/api/snapshot"
    for url in (
        "https://127.0.0.1:8765",
        "http://192.168.1.20:8765",
        "http://user@localhost:8765",
        "http://localhost:8765/api/status",
    ):
        with pytest.raises(ValueError):
            snapshot_url(url)


def test_live_snapshot_requires_real_modes_and_exact_correlations() -> None:
    snapshot = _live_snapshot()
    assert validate_snapshot(snapshot, ("openxr", "linker", "hitbot")) == []

    invalid = deepcopy(snapshot)
    invalid["sources"]["openxr"]["payload"]["mode"] = "fake"
    invalid["sources"]["linker"]["payload"]["command_identity_match"] = False
    invalid["sources"]["hitbot"]["payload"]["mode"] = "synthetic"
    issues = validate_snapshot(invalid, ("openxr", "linker", "hitbot"))
    assert "openxr-not-real:fake" in issues
    assert "linker-command-identity-mismatch" in issues
    assert "hitbot-not-live:synthetic" in issues


def test_hil_observer_source_has_no_action_or_hardware_path() -> None:
    source = Path("tools/control_console/hil_observe.py").read_text()
    assert "method=\"GET\"" in source
    assert "do_POST" not in source
    assert "HitBotInterface" not in source
    assert "HitbotSixAxiscall" not in source
    assert "python-can" not in source
    assert "linker_hand" not in source
