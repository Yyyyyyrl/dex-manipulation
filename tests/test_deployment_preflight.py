from __future__ import annotations

import json
from pathlib import Path

import pytest

from dex_runtime.cli import main
from dex_runtime.deployment import DeploymentBinding, DeploymentBindingError
from dex_runtime.preflight import preflight_deployment
from policy_package_factory import (
    CALIBRATION_DIGEST,
    CALIBRATION_ID,
    CALIBRATION_LOWER,
    CALIBRATION_UPPER,
    write_test_package,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, package: Path) -> Path:
    package_id = json.loads((package / "manifest.json").read_text())["package_id"]
    config = {
        "format_version": 1,
        "binding_id": "linker-g20-left-hand-only-test-v1",
        "protocol_version": "1.0",
        "control_session_id": "preflight-session",
        "hand": {
            "model": "LinkerHand G20",
            "side": "left",
            "serial_number": "LHT20-010-415-L-B-1-D",
            "hand_joint": "G20",
        },
        "calibration": {
            "artifact_path": str(
                ROOT
                / "src/dex_hardware_linker/assets/calibrations/linker_g20_left_lht20_010_415_v1.json"
            ),
            "schema_path": str(
                ROOT
                / "src/dex_hardware_linker/assets/calibrations/linker_g20_left_semantic_schema_v1.json"
            ),
            "calibration_id": CALIBRATION_ID,
            "artifact_digest": CALIBRATION_DIGEST,
        },
        "teleop": {
            "repository_root": str(ROOT),
            "profile_path": str(
                ROOT / "configs/teleop/linker_g20_left_manus_dexpilot_v1.json"
            ),
            "retargeting_model_directory": str(
                ROOT / "src/dex_hardware_linker/assets/model"
            ),
            "manus": {
                "source_id": "manus-left",
                "topic": "manus_glove_0",
                "stale_after_ns": 100_000_000,
                "candidate_ttl_ns": 100_000_000,
            },
        },
        "gateway": {
            "transport": "fake",
            "gateway_id": "linker-g20-left",
            "gateway_hz": 200.0,
            "state_stale_ns": 100_000_000,
            "command_watchdog_ns": 1_000_000_000,
            "maximum_round_trip_error_rad": 0.01,
            "linker_sdk": None,
        },
        "policies": {
            "stores": [str(package.parent)],
            "selected_package_id": package_id,
            "allow_unsigned_local": True,
        },
        "safety": {
            "position_lower_rad": list(CALIBRATION_LOWER),
            "position_upper_rad": list(CALIBRATION_UPPER),
            "maximum_delta_per_tick_rad": 0.1,
            "maximum_target_rate_rad_s": 1.0,
            "maximum_following_error_rad": 0.5,
            "maximum_state_age_ns": 100_000_000,
            "command_deadline_ns": 50_000_000,
        },
        "readiness": {
            "required_provider_ids": [
                "operator-confirmation-v1",
                "hand-state-freshness-v1",
                "gateway-health-v1",
                "policy-compatibility-v1",
            ],
            "evidence_validity_ns": 200_000_000,
            "operator_confirmation_validity_ns": 30_000_000_000,
        },
        "handoff": {
            "ownership_lease_ns": 60_000_000_000,
            "gateway_ack_timeout_s": 0.5,
            "teleop_command_period_ns": 100_000_000,
            "policy_blend_ticks": 10,
            "handback_blend_ticks": 10,
        },
        "switch": {
            "kind": "evdev-f12",
            "device_path": "/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd",
            "source_id": "footswitch",
            "key": "F12",
            "debounce_ns": 50_000_000,
            "require_pcsensor_identity": True,
        },
        "logging": {
            "events_path": str(tmp_path / "events.jsonl"),
            "trace_path": str(tmp_path / "trace.jsonl"),
            "trace_minimum_period_ns": 50_000_000,
        },
        "status": {"period_ns": 100_000_000, "use_ansi": False},
        "arm": {"mode": "fake-hold"},
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def test_strict_preflight_validates_all_identities_without_opening_transport(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    binding = DeploymentBinding.load(config)
    assert binding.switch.key == "F12"
    assert binding.gateway.transport == "fake"
    result = preflight_deployment(str(config))
    assert result.report.policy_compatibility.compatible
    assert result.report.calibration_id == CALIBRATION_ID
    assert result.report.switch_binding.startswith("F12@")


def test_binding_rejects_unconfirmed_switch_and_missing_fields(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    raw = json.loads(config.read_text())
    raw["switch"]["key"] = "F13"
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="F12"):
        DeploymentBinding.load(config)
    raw["switch"]["key"] = "F12"
    del raw["gateway"]["command_watchdog_ns"]
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="command_watchdog_ns"):
        DeploymentBinding.load(config)


def test_binding_accepts_only_loopback_identity_bound_hitbot_hold(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    raw = json.loads(config.read_text())
    raw["arm"] = {
        "mode": "hitbot-hold-v1",
        "control_host": "127.0.0.1",
        "control_port": 8781,
        "request_timeout_s": 0.35,
        "command_ttl_ns": 500_000_000,
        "hold_lease_ns": 1_000_000_000,
    }
    config.write_text(json.dumps(raw))
    binding = DeploymentBinding.load(config)
    assert binding.arm_mode == "hitbot-hold-v1"
    assert binding.arm.control_port == 8781

    raw["arm"]["control_host"] = "0.0.0.0"
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="loopback"):
        DeploymentBinding.load(config)


def test_cli_preflight_and_package_trust_are_explicit(tmp_path, capsys) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    with pytest.raises(ValueError, match="unsigned-local"):
        main(["verify-package", str(package)])
    assert main(["verify-package", str(package), "--allow-unsigned-local"]) == 0
    assert "package_id" in capsys.readouterr().out
    assert main(["preflight", str(config)]) == 0
    output = capsys.readouterr().out
    assert "linker-g20-left-hand-only-test-v1" in output
    assert "F12@" in output

def test_preflight_rejects_rate_mismatch_before_hardware_access(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    raw = json.loads(config.read_text())
    raw["handoff"]["teleop_command_period_ns"] = 50_000_000
    config.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="control-period-mismatch"):
        preflight_deployment(str(config))

    raw["handoff"]["teleop_command_period_ns"] = 100_000_000
    raw["gateway"]["gateway_hz"] = 5.0
    config.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="gateway state rate"):
        preflight_deployment(str(config))

def test_binding_rejects_nonfinite_duplicate_and_disabled_identity(tmp_path) -> None:
    package = write_test_package(tmp_path / "store" / "policy")
    config = _write_config(tmp_path, package)
    raw = json.loads(config.read_text())

    raw["switch"]["require_pcsensor_identity"] = False
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="PCsensor"):
        DeploymentBinding.load(config)

    raw["switch"]["require_pcsensor_identity"] = True
    raw["gateway"]["gateway_hz"] = float("nan")
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="finite"):
        DeploymentBinding.load(config)

    raw["gateway"]["gateway_hz"] = 200.0
    raw["readiness"]["required_provider_ids"].append("gateway-health-v1")
    config.write_text(json.dumps(raw))
    with pytest.raises(DeploymentBindingError, match="unique"):
        DeploymentBinding.load(config)
