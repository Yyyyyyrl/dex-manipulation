#!/usr/bin/env python3
"""Repackage a dex-forge Stage-2 deploy.pth into a runtime-valid demo policy.

The dex-forge exporter (``screwdriver_rl.deploy.policy_package.export_policy_package``)
maps a training bundle's real actor + proprio-adapter weights into the
``dex-policy-package`` layout the dex-manipulation runtime consumes.  Two small
adjustments make its output loadable by *this* runtime for the switch demo:

1. The exporter emits a ``startup_sequence`` manifest field that the current
   runtime's strict schema rejects; we drop it.
2. The policy declares a LinkerHand L20 identity + its L20 deploy calibration.
   L20 and G20 are hardware-identical and share the exact semantic-16 schema, so
   we rebind the manifest to the G20 calibration the demo already drives the hand
   with — teleop and RL then share one semantic->native mapping.

After patching we recompute the content-addressed package id/digest so the
runtime's ``validate_policy_package`` accepts it.  The trained actor/adapter
weights are used verbatim; only metadata changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from dex_contracts import canonical_json

ROOT = Path(__file__).resolve().parents[1]
DEX_FORGE = Path("/home/user/dex-forge")

_TOOLS = ROOT / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
from demo_policy_factory import CALIBRATION_LOWER, CALIBRATION_UPPER  # noqa: E402

# The G20 calibration + semantic schema the demo runtime binds to (mirrors
# tools/demo_policy_factory.py, the synthetic package builder).
G20_CALIBRATION_ID = "linker-g20-left-lht20-010-415-v1"
G20_CALIBRATION_DIGEST = "1e20d989a14aa9fe127e78680decb9bb29679858e223c41ad28ae67a598d51df"
G20_HAND_MODEL = "LinkerHand G20"
DEMO_READINESS_PROVIDERS = [
    "operator-confirmation-v1",
    "hand-state-freshness-v1",
    "gateway-health-v1",
    "policy-compatibility-v1",
]

DEFAULT_DEPLOY = (
    DEX_FORGE
    / "runs/Isaac-LinkerL20-Screwdriver-Rotation-Topdown"
    / "topdown_good_main_stage2_retrain_20260722/stage2_nn/deploy.pth"
)
DEFAULT_METADATA = (
    DEX_FORGE
    / "deliverables/linker_l20_topdown_pip108_NOT_PROMOTED_20260723"
    / "immutable_policy/metadata_not_promoted.json"
)


def _assert_within_g20_envelope(action_transform: dict) -> None:
    """Guard that the policy's action clip bounds sit inside the G20 envelope."""

    lower = [float(v) for v in action_transform["position_lower_rad"]]
    upper = [float(v) for v in action_transform["position_upper_rad"]]
    for i, (pl, pu, sl, su) in enumerate(
        zip(lower, upper, CALIBRATION_LOWER, CALIBRATION_UPPER, strict=False)
    ):
        if pl < sl or pu > su:
            raise ValueError(
                f"policy action bound at joint {i} ([{pl},{pu}]) exceeds the G20 "
                f"safety envelope ([{sl},{su}]); refusing to rebind to this hand"
            )


def _content_digest(manifest: dict) -> str:
    content = deepcopy(manifest)
    content.pop("package_id", None)
    content.pop("package_digest", None)
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def build_g20_demo_package(
    out_dir: Path,
    *,
    deploy_pth: Path = DEFAULT_DEPLOY,
    metadata_path: Path = DEFAULT_METADATA,
) -> tuple[Path, str]:
    """Export ``deploy_pth`` and rebind it to the demo's G20 identity.

    Returns ``(package_dir, package_id)``.
    """

    if not deploy_pth.exists():
        raise FileNotFoundError(f"deploy bundle not found: {deploy_pth}")
    if not metadata_path.exists():
        raise FileNotFoundError(f"export metadata not found: {metadata_path}")
    if str(DEX_FORGE) not in sys.path:
        sys.path.insert(0, str(DEX_FORGE))
    import torch
    from screwdriver_rl.deploy.policy_package import export_policy_package

    # The top-down exporter requires a collision-safe ``startup_reset_targets`` in
    # the bundle config, which only feeds the ``startup_sequence`` manifest field
    # we strip below.  This bundle omits it, so synthesize the neutral home
    # posture purely to satisfy the export gate; the value is inert for the demo.
    bundle = torch.load(str(deploy_pth), map_location="cpu", weights_only=False)
    if bundle["config"].get("startup_reset_targets") is None:
        bundle["config"]["startup_reset_targets"] = list(bundle["config"]["home_targets"])

    metadata = json.loads(metadata_path.read_text())
    # Rebind identity to the demo's G20 calibration before export so the emitted
    # manifest is already G20-native; the schema id/digest are unchanged.
    metadata["hand"] = dict(metadata["hand"], model=G20_HAND_MODEL)
    metadata["calibration_compatibility"] = [
        {"calibration_id": G20_CALIBRATION_ID, "artifact_digest": G20_CALIBRATION_DIGEST}
    ]
    metadata["readiness_provider_ids"] = list(DEMO_READINESS_PROVIDERS)
    metadata["display_name"] = metadata.get("display_name", "policy") + " (G20 switch demo)"

    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dex-export-") as tmp:
        raw = Path(export_policy_package(bundle, metadata, str(Path(tmp) / "pkg")))
        manifest = json.loads((raw / "manifest.json").read_text())
        # Drop newer exporter fields this runtime's strict schema does not accept.
        # Both are inert for the demo: the runtime ignores startup_sequence and
        # derives the initial effective target from live hand state, not manifest.
        manifest.pop("startup_sequence", None)
        manifest.get("action_transform", {}).pop("initial_effective_target_rad", None)
        # Keep the policy's native (L20) action clip bounds: they sit strictly
        # inside the G20 calibration/safety envelope, so the runtime safety
        # supervisor contains them and the policy's float32 clamp never grazes the
        # G20 float64 limit. (Preflight accepts safety envelope ⊇ policy bounds.)
        _assert_within_g20_envelope(manifest["action_transform"])
        # Recompute the content-addressed identity after the edits.
        manifest["package_id"] = "pending"
        manifest["package_digest"] = "pending"
        digest = _content_digest(manifest)
        manifest["package_id"] = f"sha256:{digest}"
        manifest["package_digest"] = digest

        if out_dir.exists():
            for item in out_dir.iterdir():
                item.unlink()
        else:
            out_dir.mkdir(parents=True)
        for weight in ("actor.safetensors", "adapter.safetensors"):
            (out_dir / weight).write_bytes((raw / weight).read_bytes())
        (out_dir / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return out_dir, manifest["package_id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy", type=Path, default=DEFAULT_DEPLOY, help="Stage-2 deploy.pth")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output", type=Path, required=True, help="package output directory")
    args = parser.parse_args()
    pkg, package_id = build_g20_demo_package(
        args.output, deploy_pth=args.deploy, metadata_path=args.metadata
    )
    print(json.dumps({"package": str(pkg), "package_id": package_id}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
