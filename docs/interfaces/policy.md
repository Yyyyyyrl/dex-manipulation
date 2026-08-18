# Policy (RL) interface

*[English](policy.md) | [中文](../zh/interfaces/policy.md)*

How a trained policy is packaged, validated, and executed, and how to deploy one
you trained yourself.

This repository does not train. Training lives in `dex-forge`; policies arrive
here as **packages** — self-describing directories that carry enough metadata to
prove they belong on this hand, with this calibration, at this control rate.

## Why a package, not a checkpoint

A bare `.pth` says nothing about which hand it was trained for, what the joint
ordering is, what the control period was, or what action bounds it assumes.
Loading one onto the wrong calibration produces confident, wrong joint angles.

A package makes every one of those assumptions explicit and machine-checkable,
and it is content-addressed, so "the same package" is a claim that can be
verified rather than trusted.

```
package/
├── manifest.json        # every assumption, explicitly
├── actor.safetensors    # policy network weights
└── adapter.safetensors  # history-compression network weights
```

Validate one at any time — this opens no hardware:

```bash
dex-runtime verify-package PACKAGE --allow-unsigned-local
```

## Manifest

Validated by `_validate_manifest_structure()` in
[`policy_package.py`](../../src/dex_runtime/policy_package.py). Validation is
strict and total: unknown fields, missing fields, and inconsistent dimensions
are all load errors. There is no partial acceptance.

### Identity

| Field | Notes |
|---|---|
| `package_format` / `package_format_version` | `dex-policy-package` / `1` |
| `protocol_version` | Must match the runtime's `PROTOCOL_VERSION` |
| `package_id`, `package_digest` | `sha256:` over the canonical JSON, excluding the digest fields themselves. Recomputed and compared on load. |
| `display_name` | Human label shown to the operator on the arming prompt |
| `task.id`, `task.version` | Carried into every command's identity |
| `supported_runtime_api.min` / `.max` | Version window this package accepts |
| `trust.mode` | `unsigned-local`, which is why loading needs `--allow-unsigned-local` |

### Hand binding

| Field | Notes |
|---|---|
| `hand.model`, `hand.side` | Must equal the deployment's hand |
| `hand.semantic_schema_id`, `hand.semantic_schema_digest` | Binds to an exact joint schema **by content** |
| `calibration_compatibility[]` | List of `{calibration_id, artifact_digest}`. The running calibration must appear here, or the package is refused. Non-empty required. |
| `control_period_ns` | The cadence the policy was trained at. Observations must arrive at exactly this spacing. |

### Observation encoding

`proprio_codec` is a `ProprioCodecSpec` ([`codecs.py`](../../src/dex_runtime/codecs.py)):

| Field | Meaning |
|---|---|
| `codec_id` | e.g. `linker-g20-mounted-proprio-v1` |
| `joint_count` | 16 for the G20 |
| `frame_dim` | `2 × joint_count` — measured position and effective target, concatenated |
| `history_length` | Ring buffer depth, e.g. 30 |
| `actor_frame_count` | How many frames the actor consumes directly |
| `measured_position_scaling` | `identity-radians`, or `affine-limits-to-minus-one-one` |
| `measured_lower_rad` / `measured_upper_rad` | Affine bounds, when scaling is affine |

One observation frame is `[measured_position, effective_target]`. Measured
values are optionally normalised to `[-1, 1]` by
`2·(x − lower)/(upper − lower) − 1`; the effective target is passed through.

`actor_input_assembler` must be `latest-frames-flatten`, with a `frame_count`
and an `output_width` consistent with the codec.

### Action decoding

`action_transform` must be `bounded-delta-position`:

| Field | Meaning |
|---|---|
| `action_clip` | `[-1.0, 1.0]`; the raw action is clamped to this |
| `delta_scale_rad` | Radians per unit action |
| `position_lower_rad` / `position_upper_rad` | Per-joint bounds the final target is clamped to |
| `integration_semantics` | `acknowledged-effective-target-plus-delta` |

So: `target = clamp(effective_target + delta_scale_rad · clamp(action, −1, 1),
lower, upper)`.

Actions are **deltas on acknowledged state**, not absolute positions. If a
command is dropped, integrating from "what we sent" would compound the next
action onto a position the hand never reached; integrating from the
acknowledged `EffectiveHandTarget` degrades safely.

The package's bounds are not the safety envelope. `HandSafetyLimits` in the
deployment is checked independently on every command, and a package can only
ever be more conservative than the deployment, never less.

### Networks

| Field | Meaning |
|---|---|
| `network.actor` | `mlp_units`, `activation`, `obs_dim`, `proprio_dim`, `latent_dim`, `action_dim`, `normalize_input`, `clip_obs` |
| `network.adapter` | `architecture_id` (`proprio-adapt-tconv-v1`), `frame_dim`, `history_length`, `output_dim`, `frame_encoder_units`, `temporal_convolutions[]` |
| `weights.actor` / `weights.adapter` | `{path, format: safetensors, sha256}` — the digest is verified before loading |

The adapter compresses the history window to a small latent; the actor consumes
`[flattened proprio, latent]`. Dimensions must agree with the codec, and
disagreement is a load error rather than a runtime shape crash.

### Timing, history, provenance

| Field | Meaning |
|---|---|
| `history.length`, `.reset_semantics`, `.activation_requires_full_history` | `collect-fresh-effective-targets`; activation requires a full history |
| `state_requirements.fields` | Must include `semantic_position` and `last_effective_target` |
| `state_requirements.acknowledgement_level` | Minimum evidence strength the hardware must be able to supply |
| `state_requirements.maximum_state_age_ns`, `maximum_effective_target_age_ns` | Freshness the policy assumes |
| `task_frame` | Wrist/task frame, position and orientation envelopes, fixture assumptions |
| `provenance` | `training_commit`, `training_dirty`, resolved config digest, URDF and asset digests |
| `evaluation` | `results`, `promotion_status` (e.g. `commissioning`) |
| `readiness_provider_ids` | Evidence this policy requires before it may be armed |

`provenance.training_dirty` records whether the training tree had uncommitted
changes. It is not enforced, but it is the field to check first when a policy
behaves differently from its evaluation.

## Execution

`PolicySession` ([`policy_session.py`](../../src/dex_runtime/policy_session.py)):

```
LOADED --reset--> SHADOW --activate--> ACTIVE
                    ^                    |
                    |                deactivate
                    +---- reset ---- DEACTIVATED --close--> CLOSED
```

| Method | Valid in | Effect |
|---|---|---|
| `reset(measured, effective_target, …)` | LOADED, DEACTIVATED | Clears history, seeds the effective target, enters SHADOW |
| `observe(measured, effective_target, tick, scheduled_time_ns, state_sequence)` | SHADOW, ACTIVE | Appends one frame. Enforces consecutive ticks, exact cadence, increasing state sequence. |
| `preview()` | SHADOW, ACTIVE | Runs inference, returns a `PolicyHandCandidate`. Cached per tick. Commands nothing. |
| `activate(tick, control_epoch)` | SHADOW | Promotes to ACTIVE. Requires a preview for that same tick and a strictly increasing epoch. |
| `step(...)` | ACTIVE | `observe()` then `preview()` |
| `deactivate()` / `close()` | — | Hand back, or finish |

Two things are deliberate here.

**Inference happens in SHADOW.** The policy runs, fills its history, and has its
proposed targets checked by the safety supervisor and written to the trace,
while teleoperation still holds the hand. By the time it is activated its first
target continues from the target already in effect — that is what makes the
switch bumpless — and a policy that would have violated the envelope is visible
*before* it ever gets the hand.

**Activation reuses the previewed candidate.** `activate()` requires a preview
for the same tick and returns it, rather than running fresh inference. The
command that hands over control is exactly the one that was just evaluated.

Cadence violations are hard errors. The temporal convolution over the history
assumes a uniformly sampled window, so quietly accepting a skipped or repeated
tick would change what the policy sees with no signal that it happened.

## Deploying your own policy

1. **Export from training.** You need a `deploy.pth` and the metadata the
   exporter produces.

2. **Repackage.** `tools/repackage_stage2_policy.py::repackage_g20_policy` is the
   worked example. It calls the `dex-forge` exporter, then does the parts that
   are specific to landing on this runtime: strips fields the runtime rejects,
   rebinds calibration compatibility from the training hand to the deployment
   hand, checks the action bounds sit inside the safety envelope, and recomputes
   the content-addressed id and digest.

3. **Validate.** `dex-runtime verify-package PACKAGE --allow-unsigned-local`.
   Fix whatever it rejects; it will not accept a partially valid manifest.

4. **Register.** Put the directory in a store listed under `policies.stores` in
   the deployment config, then confirm the runtime sees it:
   `dex-runtime list-policies CONFIG`.

5. **Preflight.** `dex-runtime preflight CONFIG` proves compatibility without
   opening hardware.

6. **Shadow first.** Run with the policy in `RL_SHADOW` and inspect the trace.
   The policy is running and being safety-checked without commanding anything.
   This is the step worth spending time on.

7. **Then, and only under the authorized procedure, switch.** See
   [operator-runbook.md](../operator-runbook.md).

No code changes are needed for a new policy. If you find yourself editing
`dex_runtime` to load one, the manifest is wrong.

## Compatibility gates

`PolicyCompatibilityProvider` ([`readiness.py`](../../src/dex_runtime/readiness.py))
blocks activation when:

- the runtime API version is outside `supported_runtime_api`
- `hand.model` / `hand.side` disagree with the deployment
- the semantic schema digest does not match the running schema
- the running calibration is absent from `calibration_compatibility`
- `control_period_ns` disagrees with the configured control period
- the hardware cannot supply the required acknowledgement level

These are digest comparisons, not version-string comparisons: a version string
says two artifacts claim to be the same, a digest proves it.
