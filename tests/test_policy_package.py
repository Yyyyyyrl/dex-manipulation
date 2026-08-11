from __future__ import annotations

import json

import pytest

from dex_runtime.policy_package import (
    PolicyCompatibilityContext,
    PolicyPackageValidationError,
    PolicyRegistry,
    check_policy_compatibility,
    validate_policy_package,
)
from policy_package_factory import (
    CALIBRATION_DIGEST,
    CALIBRATION_ID,
    SCHEMA_DIGEST,
    SCHEMA_ID,
    rewrite_manifest,
    write_test_package,
)


def _context(**changes) -> PolicyCompatibilityContext:
    values = {
        "runtime_api_version": "1.0",
        "protocol_version": "1.0",
        "hand_model": "LinkerHand G20",
        "hand_side": "left",
        "semantic_schema_id": SCHEMA_ID,
        "semantic_schema_digest": SCHEMA_DIGEST,
        "calibration_id": CALIBRATION_ID,
        "calibration_digest": CALIBRATION_DIGEST,
        "control_period_ns": 100_000_000,
        "acknowledgement_levels": ("sent-to-bus",),
    }
    values.update(changes)
    return PolicyCompatibilityContext(**values)


def test_package_validation_requires_explicit_unsigned_local_trust(tmp_path) -> None:
    directory = write_test_package(tmp_path / "policy")
    with pytest.raises(PolicyPackageValidationError, match="unsigned-local"):
        validate_policy_package(directory, allow_unsigned_local=False)
    package = validate_policy_package(directory, allow_unsigned_local=True)
    assert package.descriptor.package_id.startswith("sha256:")
    assert package.codec_spec.control_period_ns == 100_000_000
    assert package.load_tensors()[0] and package.load_tensors()[1]


def test_incompatible_identity_rate_and_calibration_fail_before_weight_load(tmp_path) -> None:
    directory = write_test_package(tmp_path / "policy")
    package = validate_policy_package(directory, allow_unsigned_local=True)
    compatibility = check_policy_compatibility(
        package,
        _context(
            hand_side="right",
            calibration_digest="wrong",
            control_period_ns=50_000_000,
        ),
    )
    assert not compatibility.compatible
    assert compatibility.reason_codes == (
        "hand-side-mismatch",
        "calibration-mismatch",
        "control-period-mismatch",
    )


def test_tensor_tamper_and_missing_safety_field_are_rejected(tmp_path) -> None:
    tampered = write_test_package(tmp_path / "tampered")
    with (tampered / "actor.safetensors").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(PolicyPackageValidationError, match="actor tensor digest"):
        validate_policy_package(tampered, allow_unsigned_local=True)

    missing = write_test_package(tmp_path / "missing")
    manifest = json.loads((missing / "manifest.json").read_text())
    del manifest["state_requirements"]["maximum_state_age_ns"]
    rewrite_manifest(missing, manifest)
    with pytest.raises(PolicyPackageValidationError, match="state requirements"):
        validate_policy_package(missing, allow_unsigned_local=True)


def test_non_standard_json_numeric_constants_are_rejected(tmp_path) -> None:
    directory = write_test_package(tmp_path / "nonfinite")
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["action_transform"]["delta_scale_rad"] = float("nan")
    rewrite_manifest(directory, manifest)
    with pytest.raises(PolicyPackageValidationError, match="numeric constant"):
        validate_policy_package(directory, allow_unsigned_local=True)


def test_registry_scans_descriptors_and_reports_compatibility(tmp_path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    write_test_package(store / "mounted")
    write_test_package(store / "free", free_object=True)
    snapshot = PolicyRegistry((store,), allow_unsigned_local=True).scan(_context())
    assert not snapshot.errors
    assert len(snapshot.entries) == 2
    compatible = {
        entry.descriptor.task_id: entry.compatibility.compatible for entry in snapshot.entries
    }
    assert compatible == {
        "free-object-rotation": False,
        "mounted-screwdriver-rotation": True,
    }
