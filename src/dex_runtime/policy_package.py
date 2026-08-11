"""Strict policy-package validation, compatibility checks, and registry scans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import torch
from safetensors.torch import load_file

from dex_contracts import PolicyCompatibility, PolicyDescriptor, canonical_json

from .codecs import COLLECT_FRESH_HISTORY, ProprioCodecSpec

PACKAGE_FORMAT = "dex-policy-package"
PACKAGE_FORMAT_VERSION = 1
RUNTIME_API_VERSION = "1.0"
MANIFEST_FILENAME = "manifest.json"

_TOP_LEVEL_FIELDS = {
    "package_format",
    "package_format_version",
    "protocol_version",
    "package_id",
    "package_digest",
    "display_name",
    "task",
    "hand",
    "calibration_compatibility",
    "control_period_ns",
    "weights",
    "network",
    "proprio_codec",
    "actor_input_assembler",
    "action_transform",
    "history",
    "state_requirements",
    "task_frame",
    "provenance",
    "evaluation",
    "readiness_provider_ids",
    "supported_runtime_api",
    "trust",
}


class PolicyPackageValidationError(ValueError):
    pass


def _reject_json_constant(value: str) -> object:
    raise PolicyPackageValidationError(
        f"non-standard JSON numeric constant is forbidden: {value}"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_digest(manifest: Mapping[str, object]) -> str:
    content = deepcopy(dict(manifest))
    content.pop("package_id", None)
    content.pop("package_digest", None)
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _exact(value: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise PolicyPackageValidationError(
            f"invalid {label} fields; missing={missing}, extra={extra}"
        )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PolicyPackageValidationError(f"{label} must be an object")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyPackageValidationError(f"{label} must be a non-empty string")
    return value


def _version(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise PolicyPackageValidationError(f"invalid API version {value!r}") from exc
    if not result:
        raise PolicyPackageValidationError(f"invalid API version {value!r}")
    return result


@dataclass(frozen=True)
class PolicyCompatibilityContext:
    runtime_api_version: str
    protocol_version: str
    hand_model: str
    hand_side: str
    semantic_schema_id: str
    semantic_schema_digest: str
    calibration_id: str
    calibration_digest: str
    control_period_ns: int | None = None
    acknowledgement_levels: tuple[str, ...] = ("sent-to-bus",)


@dataclass(frozen=True)
class ValidatedPolicyPackage:
    directory: Path
    manifest: Mapping[str, object]
    descriptor: PolicyDescriptor
    codec_spec: ProprioCodecSpec

    def load_tensors(self, device: str = "cpu") -> tuple[dict[str, torch.Tensor], dict[str, torch.Tensor]]:
        weights = _mapping(self.manifest["weights"], "weights")
        actor = _mapping(weights["actor"], "actor weight entry")
        adapter = _mapping(weights["adapter"], "adapter weight entry")
        return (
            load_file(str(self.directory / str(actor["path"])), device=device),
            load_file(str(self.directory / str(adapter["path"])), device=device),
        )


@dataclass(frozen=True)
class RegistryEntry:
    descriptor: PolicyDescriptor
    compatibility: PolicyCompatibility
    directory: Path


@dataclass(frozen=True)
class RegistrySnapshot:
    entries: tuple[RegistryEntry, ...]
    errors: tuple[tuple[str, str], ...]


def _validate_manifest_structure(manifest: Mapping[str, object]) -> ProprioCodecSpec:
    _exact(manifest, _TOP_LEVEL_FIELDS, "manifest")
    if manifest["package_format"] != PACKAGE_FORMAT or manifest["package_format_version"] != PACKAGE_FORMAT_VERSION:
        raise PolicyPackageValidationError("unsupported policy package format")
    if manifest["protocol_version"] != "1.0":
        raise PolicyPackageValidationError("unsupported package protocol")
    digest = _nonempty_string(manifest["package_digest"], "package_digest")
    if digest != _content_digest(manifest):
        raise PolicyPackageValidationError("package content digest mismatch")
    if manifest["package_id"] != f"sha256:{digest}":
        raise PolicyPackageValidationError("package ID is not its content digest")
    _nonempty_string(manifest["display_name"], "display_name")

    task = _mapping(manifest["task"], "task")
    _exact(task, {"id", "version"}, "task")
    _nonempty_string(task["id"], "task.id")
    _nonempty_string(task["version"], "task.version")
    hand = _mapping(manifest["hand"], "hand")
    _exact(hand, {"model", "side", "semantic_schema_id", "semantic_schema_digest"}, "hand")
    for field in ("model", "semantic_schema_id", "semantic_schema_digest"):
        _nonempty_string(hand[field], f"hand.{field}")
    if hand["side"] not in ("left", "right"):
        raise PolicyPackageValidationError("hand.side must be left or right")

    compatibility = manifest["calibration_compatibility"]
    if not isinstance(compatibility, list) or not compatibility:
        raise PolicyPackageValidationError("calibration compatibility must be non-empty")
    for item in compatibility:
        entry = _mapping(item, "calibration compatibility entry")
        _exact(entry, {"calibration_id", "artifact_digest"}, "calibration compatibility")
        _nonempty_string(entry["calibration_id"], "calibration_id")
        _nonempty_string(entry["artifact_digest"], "calibration artifact digest")

    codec_value = _mapping(manifest["proprio_codec"], "proprio_codec")
    codec = ProprioCodecSpec.from_dict(codec_value)
    if int(manifest["control_period_ns"]) != codec.control_period_ns:
        raise PolicyPackageValidationError("manifest and codec control periods differ")

    weights = _mapping(manifest["weights"], "weights")
    _exact(weights, {"actor", "adapter"}, "weights")
    for label in ("actor", "adapter"):
        entry = _mapping(weights[label], f"{label} weight entry")
        _exact(entry, {"path", "format", "sha256"}, f"{label} weight entry")
        if entry["path"] != f"{label}.safetensors" or entry["format"] != "safetensors":
            raise PolicyPackageValidationError(f"invalid {label} tensor file declaration")
        _nonempty_string(entry["sha256"], f"{label} tensor digest")

    network = _mapping(manifest["network"], "network")
    _exact(network, {"actor", "adapter"}, "network")
    actor = _mapping(network["actor"], "actor architecture")
    _exact(
        actor,
        {
            "mlp_units",
            "activation",
            "obs_dim",
            "proprio_dim",
            "latent_dim",
            "action_dim",
            "normalize_input",
            "clip_obs",
        },
        "actor architecture",
    )
    adapter = _mapping(network["adapter"], "adapter architecture")
    _exact(
        adapter,
        {
            "architecture_id",
            "frame_dim",
            "history_length",
            "output_dim",
            "frame_encoder_units",
            "temporal_convolutions",
            "activation",
        },
        "adapter architecture",
    )
    if adapter["architecture_id"] != "proprio-adapt-tconv-v1":
        raise PolicyPackageValidationError("unsupported adapter architecture")
    if int(adapter["frame_dim"]) != codec.frame_dim or int(adapter["history_length"]) != codec.history_length:
        raise PolicyPackageValidationError("adapter history shape differs from codec")

    assembler = _mapping(manifest["actor_input_assembler"], "actor input assembler")
    _exact(assembler, {"kind", "frame_count", "output_width"}, "actor input assembler")
    expected_width = codec.actor_frame_count * codec.frame_dim
    if (
        assembler["kind"] != "latest-frames-flatten"
        or int(assembler["frame_count"]) != codec.actor_frame_count
        or int(assembler["output_width"]) != expected_width
        or int(actor["proprio_dim"]) != expected_width
    ):
        raise PolicyPackageValidationError("actor input assembler is inconsistent")
    if int(actor["action_dim"]) != codec.joint_count:
        raise PolicyPackageValidationError("actor action width differs from codec joint count")

    action = _mapping(manifest["action_transform"], "action transform")
    _exact(
        action,
        {
            "kind",
            "action_clip",
            "delta_scale_rad",
            "position_lower_rad",
            "position_upper_rad",
            "integration_semantics",
        },
        "action transform",
    )
    if action["kind"] != "bounded-delta-position" or action["integration_semantics"] != "acknowledged-effective-target-plus-delta":
        raise PolicyPackageValidationError("unsupported action integration semantics")
    if action["action_clip"] != [-1.0, 1.0] or float(action["delta_scale_rad"]) <= 0:
        raise PolicyPackageValidationError("invalid action clipping or delta scale")
    lower = action["position_lower_rad"]
    upper = action["position_upper_rad"]
    if not isinstance(lower, list) or not isinstance(upper, list) or len(lower) != codec.joint_count or len(upper) != codec.joint_count:
        raise PolicyPackageValidationError("invalid action position-limit width")
    if any(float(high) <= float(low) for low, high in zip(lower, upper, strict=False)):
        raise PolicyPackageValidationError("invalid action position limits")

    history = _mapping(manifest["history"], "history")
    _exact(history, {"length", "reset_semantics", "activation_requires_full_history"}, "history")
    if (
        int(history["length"]) != codec.history_length
        or history["reset_semantics"] != COLLECT_FRESH_HISTORY
        or history["activation_requires_full_history"] is not True
    ):
        raise PolicyPackageValidationError("unsupported history/reset semantics")

    state = _mapping(manifest["state_requirements"], "state requirements")
    _exact(
        state,
        {"fields", "acknowledgement_level", "maximum_state_age_ns", "maximum_effective_target_age_ns"},
        "state requirements",
    )
    if not isinstance(state["fields"], list) or "semantic_position" not in state["fields"] or "last_effective_target" not in state["fields"]:
        raise PolicyPackageValidationError("required hand state fields are incomplete")
    if int(state["maximum_state_age_ns"]) <= 0 or int(state["maximum_effective_target_age_ns"]) <= 0:
        raise PolicyPackageValidationError("state freshness limits must be positive")
    _nonempty_string(state["acknowledgement_level"], "acknowledgement level")

    task_frame = _mapping(manifest["task_frame"], "task frame")
    _exact(
        task_frame,
        {
            "task_frame_id",
            "wrist_frame_id",
            "desired_task_from_wrist",
            "position_envelope_m",
            "orientation_envelope_rad",
            "maximum_wrist_twist_rad_s",
            "gravity_relative_orientation",
            "object_fixture_assumptions",
            "contact_target_gap_conditions",
        },
        "task frame",
    )
    for name in ("task_frame_id", "wrist_frame_id"):
        _nonempty_string(task_frame[name], f"task_frame.{name}")

    provenance = _mapping(manifest["provenance"], "provenance")
    _exact(
        provenance,
        {"training_commit", "training_dirty", "resolved_training_config_digest", "urdf_digest", "asset_digests"},
        "provenance",
    )
    for name in ("training_commit", "resolved_training_config_digest", "urdf_digest"):
        _nonempty_string(provenance[name], f"provenance.{name}")
    if not isinstance(provenance["training_dirty"], bool) or not isinstance(provenance["asset_digests"], Mapping):
        raise PolicyPackageValidationError("invalid provenance dirty-state or asset digests")

    evaluation = _mapping(manifest["evaluation"], "evaluation")
    _exact(evaluation, {"results", "promotion_status"}, "evaluation")
    if not isinstance(evaluation["results"], Mapping):
        raise PolicyPackageValidationError("evaluation.results must be an object")
    _nonempty_string(evaluation["promotion_status"], "promotion status")
    readiness = manifest["readiness_provider_ids"]
    if not isinstance(readiness, list) or any(not isinstance(item, str) or not item for item in readiness):
        raise PolicyPackageValidationError("readiness provider IDs must be strings")

    api = _mapping(manifest["supported_runtime_api"], "supported runtime API")
    _exact(api, {"min", "max"}, "supported runtime API")
    minimum = _version(_nonempty_string(api["min"], "runtime API minimum"))
    maximum = _version(_nonempty_string(api["max"], "runtime API maximum"))
    if minimum > maximum:
        raise PolicyPackageValidationError("runtime API range is reversed")

    trust = _mapping(manifest["trust"], "trust")
    _exact(trust, {"mode", "signature"}, "trust")
    if trust["mode"] != "unsigned-local" or trust["signature"] is not None:
        raise PolicyPackageValidationError("unsupported package trust mode")
    return codec


def _descriptor(manifest: Mapping[str, object], codec: ProprioCodecSpec) -> PolicyDescriptor:
    task = _mapping(manifest["task"], "task")
    hand = _mapping(manifest["hand"], "hand")
    calibration = manifest["calibration_compatibility"]
    evaluation = _mapping(manifest["evaluation"], "evaluation")
    api = _mapping(manifest["supported_runtime_api"], "supported runtime API")
    results = _mapping(evaluation["results"], "evaluation results")
    summary = tuple(
        (str(key), value)
        for key, value in sorted(results.items())
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
    )
    return PolicyDescriptor(
        package_id=str(manifest["package_id"]),
        package_digest=str(manifest["package_digest"]),
        display_name=str(manifest["display_name"]),
        task_id=str(task["id"]),
        task_version=str(task["version"]),
        hand_model=str(hand["model"]),
        hand_side=str(hand["side"]),
        semantic_schema_id=str(hand["semantic_schema_id"]),
        semantic_schema_digest=str(hand["semantic_schema_digest"]),
        calibration_compatibility=tuple(
            f"{entry['calibration_id']}@{entry['artifact_digest']}" for entry in calibration
        ),
        control_period_ns=codec.control_period_ns,
        codec_id=codec.codec_id,
        runtime_api_min=str(api["min"]),
        runtime_api_max=str(api["max"]),
        promotion_status=str(evaluation["promotion_status"]),
        evaluation_summary=summary,
        readiness_provider_ids=tuple(str(item) for item in manifest["readiness_provider_ids"]),
    )


def validate_policy_package(
    directory: str | Path,
    *,
    allow_unsigned_local: bool,
    verify_tensor_files: bool = True,
) -> ValidatedPolicyPackage:
    package_directory = Path(directory).resolve()
    manifest_path = package_directory / MANIFEST_FILENAME
    raw = manifest_path.read_text(encoding="utf-8")
    try:
        manifest = json.loads(raw, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise PolicyPackageValidationError(f"invalid package JSON: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise PolicyPackageValidationError("manifest root must be an object")
    if raw != canonical_json(manifest) + "\n":
        raise PolicyPackageValidationError("manifest is not canonical JSON")
    codec = _validate_manifest_structure(manifest)
    trust = _mapping(manifest["trust"], "trust")
    if trust["mode"] == "unsigned-local" and not allow_unsigned_local:
        raise PolicyPackageValidationError("unsigned-local package trust is not enabled")
    if verify_tensor_files:
        weights = _mapping(manifest["weights"], "weights")
        for label in ("actor", "adapter"):
            entry = _mapping(weights[label], f"{label} weight entry")
            tensor_path = package_directory / str(entry["path"])
            if not tensor_path.is_file():
                raise PolicyPackageValidationError(f"missing {label} tensor file")
            if _sha256_file(tensor_path) != entry["sha256"]:
                raise PolicyPackageValidationError(f"{label} tensor digest mismatch")
    return ValidatedPolicyPackage(package_directory, manifest, _descriptor(manifest, codec), codec)


def check_policy_compatibility(
    package: ValidatedPolicyPackage,
    context: PolicyCompatibilityContext,
) -> PolicyCompatibility:
    manifest = package.manifest
    hand = _mapping(manifest["hand"], "hand")
    api = _mapping(manifest["supported_runtime_api"], "supported runtime API")
    state = _mapping(manifest["state_requirements"], "state requirements")
    reasons: list[str] = []
    if context.protocol_version != manifest["protocol_version"]:
        reasons.append("protocol-mismatch")
    runtime = _version(context.runtime_api_version)
    if runtime < _version(str(api["min"])) or runtime > _version(str(api["max"])):
        reasons.append("runtime-api-mismatch")
    for field, expected, actual in (
        ("hand-model", hand["model"], context.hand_model),
        ("hand-side", hand["side"], context.hand_side),
        ("semantic-schema-id", hand["semantic_schema_id"], context.semantic_schema_id),
        ("semantic-schema-digest", hand["semantic_schema_digest"], context.semantic_schema_digest),
    ):
        if expected != actual:
            reasons.append(f"{field}-mismatch")
    accepted_calibrations = {
        (str(item["calibration_id"]), str(item["artifact_digest"]))
        for item in manifest["calibration_compatibility"]
    }
    if (context.calibration_id, context.calibration_digest) not in accepted_calibrations:
        reasons.append("calibration-mismatch")
    if context.control_period_ns is not None and context.control_period_ns != package.codec_spec.control_period_ns:
        reasons.append("control-period-mismatch")
    if state["acknowledgement_level"] not in context.acknowledgement_levels:
        reasons.append("acknowledgement-level-unsupported")
    return PolicyCompatibility(not reasons, tuple(reasons))


class PolicyRegistry:
    def __init__(self, stores: tuple[str | Path, ...], *, allow_unsigned_local: bool) -> None:
        if not stores:
            raise ValueError("at least one policy store is required")
        self.stores = tuple(Path(store).resolve() for store in stores)
        self.allow_unsigned_local = allow_unsigned_local

    def scan(self, context: PolicyCompatibilityContext) -> RegistrySnapshot:
        entries: list[RegistryEntry] = []
        errors: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        candidates: list[Path] = []
        for store in self.stores:
            if (store / MANIFEST_FILENAME).is_file():
                candidates.append(store)
            elif store.is_dir():
                candidates.extend(
                    child for child in sorted(store.iterdir()) if (child / MANIFEST_FILENAME).is_file()
                )
        for directory in candidates:
            try:
                package = validate_policy_package(
                    directory,
                    allow_unsigned_local=self.allow_unsigned_local,
                    verify_tensor_files=True,
                )
                if package.descriptor.package_id in seen_ids:
                    raise PolicyPackageValidationError("duplicate content-addressed package ID")
                seen_ids.add(package.descriptor.package_id)
                entries.append(
                    RegistryEntry(
                        package.descriptor,
                        check_policy_compatibility(package, context),
                        directory,
                    )
                )
            except (OSError, ValueError) as exc:
                errors.append((str(directory), str(exc)))
        return RegistrySnapshot(tuple(entries), tuple(errors))
