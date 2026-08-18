from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import torch
from safetensors.torch import save_file

from dex_contracts import canonical_json
from dex_runtime.codecs import (
    inhand_linker_g20_codec_spec,
    mounted_linker_g20_codec_spec,
)
from dex_runtime.policy_session import RuntimeActor, RuntimeAdapter

CALIBRATION_ID = "linker-g20-left-lht20-010-415-v1"
CALIBRATION_DIGEST = "1e20d989a14aa9fe127e78680decb9bb29679858e223c41ad28ae67a598d51df"
SCHEMA_ID = "linker-g20-left-semantic-16-v1"
SCHEMA_DIGEST = "ce53ccafeb70a7bd9ba203576f7e54f330e106c0676ef90f8262d2a9ffa34ba7"
CALIBRATION_LOWER = (
    -0.17,
    0.0,
    0.0,
    -0.17,
    0.0,
    0.0,
    -0.17,
    0.0,
    0.0,
    -0.17,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
)
CALIBRATION_UPPER = (
    0.17,
    1.4,
    1.57,
    0.17,
    1.4,
    1.57,
    0.17,
    1.4,
    1.57,
    0.17,
    1.4,
    1.57,
    1.4,
    1.22,
    0.79,
    1.05,
)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_digest(manifest: dict) -> str:
    content = deepcopy(manifest)
    content.pop("package_id", None)
    content.pop("package_digest", None)
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def rewrite_manifest(directory: Path, manifest: dict) -> None:
    manifest["package_id"] = "pending"
    manifest["package_digest"] = "pending"
    digest = _content_digest(manifest)
    manifest["package_id"] = f"sha256:{digest}"
    manifest["package_digest"] = digest
    (directory / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")


def write_demo_package(root: Path, *, free_object: bool = False) -> Path:
    root.mkdir(parents=True)
    torch.manual_seed(7)
    lower = list(CALIBRATION_LOWER)
    upper = list(CALIBRATION_UPPER)
    codec = (
        inhand_linker_g20_codec_spec(lower, upper, 30)
        if free_object
        else mounted_linker_g20_codec_spec(30)
    )
    actor_width = codec.actor_frame_count * codec.frame_dim
    actor_arch = {
        "mlp_units": [32, 32],
        "activation": "elu",
        "obs_dim": actor_width + 9,
        "proprio_dim": actor_width,
        "latent_dim": 8,
        "action_dim": 16,
        "normalize_input": True,
        "clip_obs": 5.0,
    }
    actor = RuntimeActor(actor_arch)
    adapter = RuntimeAdapter(codec.frame_dim, codec.history_length, 8)
    actor_path = root / "actor.safetensors"
    adapter_path = root / "adapter.safetensors"
    save_file(
        {key: value.detach().contiguous() for key, value in actor.state_dict().items()},
        str(actor_path),
    )
    save_file(
        {key: value.detach().contiguous() for key, value in adapter.state_dict().items()},
        str(adapter_path),
    )
    manifest = {
        "package_format": "dex-policy-package",
        "package_format_version": 1,
        "protocol_version": "1.0",
        "package_id": "pending",
        "package_digest": "pending",
        "display_name": "Free object test" if free_object else "Mounted test",
        "task": {
            "id": "free-object-rotation" if free_object else "mounted-screwdriver-rotation",
            "version": "1.0",
        },
        "hand": {
            "model": "LinkerHand G20",
            "side": "left",
            "semantic_schema_id": SCHEMA_ID,
            "semantic_schema_digest": SCHEMA_DIGEST,
        },
        "calibration_compatibility": [
            {"calibration_id": CALIBRATION_ID, "artifact_digest": CALIBRATION_DIGEST}
        ],
        "control_period_ns": codec.control_period_ns,
        "weights": {
            "actor": {
                "path": "actor.safetensors",
                "format": "safetensors",
                "sha256": _file_digest(actor_path),
            },
            "adapter": {
                "path": "adapter.safetensors",
                "format": "safetensors",
                "sha256": _file_digest(adapter_path),
            },
        },
        "network": {
            "actor": actor_arch,
            "adapter": {
                "architecture_id": "proprio-adapt-tconv-v1",
                "frame_dim": codec.frame_dim,
                "history_length": codec.history_length,
                "output_dim": 8,
                "frame_encoder_units": [32, 32],
                "temporal_convolutions": [
                    {"channels": 32, "kernel": 9, "stride": 2},
                    {"channels": 32, "kernel": 5, "stride": 1},
                    {"channels": 32, "kernel": 5, "stride": 1},
                ],
                "activation": "elu",
            },
        },
        "proprio_codec": codec.as_dict(),
        "actor_input_assembler": {
            "kind": "latest-frames-flatten",
            "frame_count": codec.actor_frame_count,
            "output_width": actor_width,
        },
        "action_transform": {
            "kind": "bounded-delta-position",
            "action_clip": [-1.0, 1.0],
            "delta_scale_rad": 0.05,
            "position_lower_rad": lower,
            "position_upper_rad": upper,
            "integration_semantics": "acknowledged-effective-target-plus-delta",
        },
        "history": {
            "length": codec.history_length,
            "reset_semantics": "collect-fresh-effective-targets",
            "activation_requires_full_history": True,
        },
        "state_requirements": {
            "fields": ["semantic_position", "last_effective_target"],
            "acknowledgement_level": "sent-to-bus",
            "maximum_state_age_ns": 100_000_000,
            "maximum_effective_target_age_ns": 100_000_000,
        },
        "task_frame": {
            "task_frame_id": "fixture-axis",
            "wrist_frame_id": "hand-base",
            "desired_task_from_wrist": [0, 0, 0, 1, 0, 0, 0],
            "position_envelope_m": [0.01, 0.01, 0.01],
            "orientation_envelope_rad": 0.1,
            "maximum_wrist_twist_rad_s": 0.05,
            "gravity_relative_orientation": "fixture-axis-parallel-gravity",
            "object_fixture_assumptions": ["fixture-secured"],
            "contact_target_gap_conditions": ["operator-confirmed"],
        },
        "provenance": {
            "training_commit": "0123456789abcdef0123456789abcdef01234567",
            "training_dirty": False,
            "resolved_training_config_digest": "a" * 64,
            "urdf_digest": "b" * 64,
            "asset_digests": {"object": "c" * 64},
        },
        "evaluation": {
            "results": {"success_rate": 0.9, "episodes": 100},
            "promotion_status": "commissioning",
        },
        "readiness_provider_ids": [
            "operator-confirmation-v1",
            "hand-state-freshness-v1",
            "gateway-health-v1",
        ],
        "supported_runtime_api": {"min": "1.0", "max": "1.0"},
        "trust": {"mode": "unsigned-local", "signature": None},
    }
    rewrite_manifest(root, manifest)
    return root
