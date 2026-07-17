"""Non-actuating deployment preflight; no transport is opened here."""

from __future__ import annotations

from dataclasses import dataclass

from dex_hardware_linker import LinkerMapper
from dex_teleop_adapters import TeleopProfile

from .deployment import DeploymentBinding
from .policy_package import (
    PolicyCompatibility,
    PolicyCompatibilityContext,
    PolicyRegistry,
    ValidatedPolicyPackage,
    check_policy_compatibility,
    validate_policy_package,
)


@dataclass(frozen=True)
class PreflightReport:
    binding_id: str
    hand_identity: str
    calibration_id: str
    calibration_digest: str
    teleop_profile_id: str
    teleop_profile_digest: str
    policy_package_id: str
    policy_display_name: str
    policy_compatibility: PolicyCompatibility
    gateway_transport: str
    switch_binding: str
    arm_mode: str


@dataclass(frozen=True)
class PreflightResult:
    binding: DeploymentBinding
    mapper: LinkerMapper
    teleop_profile: TeleopProfile
    policy_package: ValidatedPolicyPackage
    report: PreflightReport


def preflight_deployment(config_path: str) -> PreflightResult:
    binding = DeploymentBinding.load(config_path)
    mapper = LinkerMapper.load(
        binding.calibration.artifact_path,
        binding.calibration.schema_path,
    )
    calibration = mapper.calibration
    expected = binding.calibration
    if calibration.calibration_id != expected.calibration_id:
        raise ValueError("deployment calibration ID mismatch")
    if calibration.artifact_digest != expected.artifact_digest:
        raise ValueError("deployment calibration digest mismatch")
    if (
        calibration.hand_model != binding.hand.model
        or calibration.hand_side != binding.hand.side
        or calibration.hand_joint != binding.hand.hand_joint
        or calibration.serial_number != binding.hand.serial_number
    ):
        raise ValueError("deployment hand identity does not match calibration")

    profile = TeleopProfile.load(
        binding.teleop.profile_path,
        binding.teleop.repository_root,
    )
    if (
        profile.hand_model != calibration.hand_model
        or profile.hand_side != calibration.hand_side
        or profile.semantic_schema_id != calibration.semantic_schema_id
        or profile.semantic_schema_digest != calibration.semantic_schema_digest
    ):
        raise ValueError("TeleopProfile identity does not match calibration")

    context = PolicyCompatibilityContext(
        runtime_api_version="1.0",
        protocol_version=binding.protocol_version,
        hand_model=calibration.hand_model,
        hand_side=calibration.hand_side,
        semantic_schema_id=calibration.semantic_schema_id,
        semantic_schema_digest=calibration.semantic_schema_digest,
        calibration_id=calibration.calibration_id,
        calibration_digest=calibration.artifact_digest,
        control_period_ns=binding.handoff.teleop_command_period_ns,
        acknowledgement_levels=("sent-to-bus",),
    )
    snapshot = PolicyRegistry(
        binding.policies.stores,
        allow_unsigned_local=binding.policies.allow_unsigned_local,
    ).scan(context)
    if snapshot.errors:
        detail = "; ".join(f"{path}: {error}" for path, error in snapshot.errors)
        raise ValueError(f"policy store validation failed: {detail}")
    selected = next(
        (
            entry
            for entry in snapshot.entries
            if entry.descriptor.package_id == binding.policies.selected_package_id
        ),
        None,
    )
    if selected is None:
        raise ValueError("selected content-addressed policy package was not found")
    package = validate_policy_package(
        selected.directory,
        allow_unsigned_local=binding.policies.allow_unsigned_local,
    )
    compatibility = check_policy_compatibility(package, context)
    if not compatibility.compatible:
        raise ValueError(
            "selected policy is incompatible: " + ",".join(compatibility.reason_codes)
        )
    package_rate_hz = 1_000_000_000 / package.codec_spec.control_period_ns
    if binding.gateway.gateway_hz < package_rate_hz:
        raise ValueError("gateway state rate is slower than the selected policy rate")

    if tuple(binding.safety.position_lower_rad) != tuple(
        float(value) for value in package.manifest["action_transform"]["position_lower_rad"]
    ) or tuple(binding.safety.position_upper_rad) != tuple(
        float(value) for value in package.manifest["action_transform"]["position_upper_rad"]
    ):
        raise ValueError("deployment and policy action limits differ")
    calibration_lower = tuple(joint.lower for joint in calibration.joints)
    calibration_upper = tuple(joint.upper for joint in calibration.joints)
    if any(
        safe_lower < mapped_lower or safe_upper > mapped_upper
        for safe_lower, safe_upper, mapped_lower, mapped_upper in zip(
            binding.safety.position_lower_rad,
            binding.safety.position_upper_rad,
            calibration_lower,
            calibration_upper,
        )
    ):
        raise ValueError("deployment action limits exceed calibrated mapping limits")
    report = PreflightReport(
        binding_id=binding.binding_id,
        hand_identity=(
            f"{binding.hand.model} {binding.hand.side} {binding.hand.serial_number}"
        ),
        calibration_id=calibration.calibration_id,
        calibration_digest=calibration.artifact_digest,
        teleop_profile_id=profile.profile_id,
        teleop_profile_digest=profile.profile_digest,
        policy_package_id=package.descriptor.package_id,
        policy_display_name=package.descriptor.display_name,
        policy_compatibility=compatibility,
        gateway_transport=binding.gateway.transport,
        switch_binding=f"{binding.switch.key}@{binding.switch.device_path}",
        arm_mode=binding.arm_mode,
    )
    return PreflightResult(binding, mapper, profile, package, report)
