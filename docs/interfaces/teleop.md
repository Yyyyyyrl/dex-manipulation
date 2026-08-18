# Teleoperation interface

*[English](teleop.md) | [中文](../zh/interfaces/teleop.md)*

How operator input becomes a hand target, and how to add a device that isn't
already supported.

Two devices ship today: a Manus glove over ROS 2, and Quest 3S hand tracking
over OpenXR/WiVRn. They share everything downstream of retargeting.

## The two roles

Input is split into a **source** (owns a transport, emits validated samples) and
a **retargeter** (turns samples into hand targets). Both contracts are written
down in [`src/dex_teleop_adapters/protocols.py`](../../src/dex_teleop_adapters/protocols.py)
as `typing.Protocol` definitions. They are structural: nothing subclasses them,
nothing checks them at runtime, and the existing implementations satisfy them as
written.

```
device ──▶ TeleopSource ──TimestampedSample──▶ Retargeter ──TeleopHandCandidate──▶ runtime
           validates                            solves + projects
           layout/side/validity                 onto named semantic joints
```

The split matters because it is where device specifics stop. A retargeter's
output names joints; nothing above it knows whether a glove or a headset
produced them.

## What a source must emit

A `TimestampedSample` (`dex_contracts/identity.py`):

| Field | Meaning and obligations |
|---|---|
| `payload` | Your own frozen dataclass of keypoints, e.g. `ManusKeypoints`, `OpenXRKeypoints`. Positions in **metres**. |
| `generated_time_ns` | When the *device* produced the sample, if it says. `None` if unknown — do not substitute local time. |
| `received_time_ns` | Local monotonic time on arrival. Freshness is judged from this. |
| `sequence` | Strictly increasing per accepted sample. Gaps are how consumers detect loss. |
| `source_health` | `SourceHealth.HEALTHY` only while samples are arriving *and* passing validation. Must degrade on staleness, not just on transport failure. |
| `validity_mask` | Per-node booleans. Report partial tracking honestly; do not fill in guesses. |
| `coordinate_frame_id` | Names the frame the points are in, e.g. `manus-wrist-local-native`. |
| `units` | `"meter"`. |
| `diagnostics` | Optional key/value tuples, surfaced in telemetry. |

Validation is the source's job, not the retargeter's: handedness, joint layout,
and per-node validity are all rejected here. A source must never actuate.

## What a retargeter must do

`retarget()` takes a sample plus session identity and returns a
`TeleopHandCandidate`, or **raises**. It does not return a degraded result: an
unhealthy sample, wrong payload type, handedness mismatch, or solver failure is
an exception, because a plausible-but-wrong hand pose is worse than none.

The pipeline both implementations follow:

1. reject unless `source_health` is `HEALTHY` and the payload type matches
2. check handedness against the loaded profile
3. remap the device's joint layout to the solver's 21-joint MANO layout
4. translate to wrist origin, estimate the wrist frame, rotate into MANO
5. run the DexPilot solver
6. project solver outputs onto the calibration's **named** semantic joints
7. apply the profile's thumb bias and low-pass filter, then validate limits

Retargeters are stateful — filter state and solver warm start persist across
calls. That is why `reset()` exists, and why it must be called at session start
and after any tracking loss.

## Coordinate conventions

This is where mistakes are easy and silent. The target for both devices is the
same: 21 keypoints, wrist at the origin, rotated into the MANO canonical frame,
which is what `dex-retargeting` expects.

**Manus** (`manus_math.py::manus_to_joint_pos`, documented in Chinese in-file):

1. Manus Core emits a **left-handed** frame → negate X to get right-handed.
2. Remap 25 native nodes onto the 21-slot MediaPipe layout via `MANUS_TO_MP`.
3. Translate to wrist origin (node 0 is already the origin; kept as a guard
   against a publisher-side `HandMotion` setting change).
4. Estimate the wrist frame, then rotate by `OPERATOR2MANO_LEFT/RIGHT`.

**OpenXR** (`openxr.py::openxr_to_joint_pos`): 26 `XR_EXT_hand_tracking` joints
→ the same 21-joint layout, then the same wrist-frame and MANO rotation. The
input is already right-handed, so there is no negation step.

**Wrist frame estimation** (`hand_frame.py::estimate_frame_from_hand_points`)
uses only three MediaPipe points — wrist (0), index MCP (5), middle MCP (9). It
fits a plane by SVD, builds an orthonormal basis, and disambiguates the normal's
sign using the pinky direction. A new device only has to land its keypoints in
the 21-slot layout correctly; this step is shared.

## Teleop profiles

A profile pins everything about a device/hand pairing that must not drift. See
`configs/teleop/linker_g20_left_openxr_dexpilot_v1.json`.

| Field | Purpose |
|---|---|
| `profile_id`, `profile_version` | Identity |
| `hand_model`, `hand_side` | Must match the deployment and the calibration |
| `semantic_schema_id`, `semantic_schema_digest` | Binds to an exact schema, by content |
| `semantic_joint_names` | The 16 output joints, **in order**. This ordering is the system's canonical joint order. |
| `retargeting_config` | Path to the DexPilot solver YAML |
| `retargeting_config_sha256` | Digest of that YAML — editing it without updating this is a load error |
| `low_pass_alpha` | Output filter coefficient |
| `thumb_cmc_roll_bias_rad` | Explicit, profile-owned thumb correction (rather than a constant buried in code) |
| `source_coordinate_conversion` | Names the conversion applied, e.g. `manus-native-left-handed-negate-x-to-right-handed` |
| `filter_reset` | When `reset()` must be called: `session-start-and-tracking-recovery` |
| `digest_algorithm` | `sha256-canonical-json-excluding-profile-digest` |
| `profile_digest` | Digest over the canonical JSON with `profile_digest` itself excluded |

Everything is content-addressed on purpose: a silently edited solver config
changes where the hand goes, so it must be a load failure rather than a
surprise.

## Adding a new device

1. **Define a keypoint payload.** A frozen dataclass with positions in metres
   and a `layout_id` naming your layout. Model it on `OpenXRKeypoints`.

2. **Write the source.** Implement `TeleopSource`: `start(callback)`,
   `stop(timeout_s)`, `status(now_ns)`. Own your transport thread, validate
   handedness/layout/validity before publishing, and keep the callback cheap —
   the runtime's callback only stores into a `LatestValueBuffer`. Follow
   `manus.py` for a ROS source or the UDP sources in
   `tools/control_console/` for a datagram source.

3. **Map into the 21-joint layout.** The one genuinely new piece of work. Write
   a `<device>_to_joint_pos()` mirroring `manus_to_joint_pos`: fix handedness,
   remap indices, translate to wrist origin, then reuse
   `estimate_frame_from_hand_points` and the `OPERATOR2MANO_*` rotation. Do not
   write your own wrist-frame estimator.

4. **Write the retargeter.** Implement `Retargeter`. Most of it is shared
   structure; copy `openxr.py::OpenXRRetargeter` and replace step 3. Reuse
   `compute_ref_value` for the solver's reference frame.

5. **Add a teleop profile.** Copy an existing JSON, set your
   `source_coordinate_conversion`, and recompute `profile_digest` over the
   canonical JSON excluding that field.

6. **Check it without hardware.** Feed recorded or synthetic samples in and
   inspect the resulting candidates — validate side, layout, sequence, and
   staleness the way the built-in sources do. Golden-trace comparison is the
   norm here; the frozen traces are in `assets/golden/`.

Two things you do **not** need to do: register the device anywhere (composition
wires it explicitly), and touch anything under `dex_runtime` or
`dex_hardware_linker` — the import contracts forbid your adapter from depending
on them, and nothing about a new device should require it.
