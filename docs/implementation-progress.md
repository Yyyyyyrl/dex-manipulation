# Architecture V2.1 implementation progress

Status date: 2026-07-16 (America/Los_Angeles)

## Authority and adopted scope

The implementation follows:

- source document: `/home/user/dex-forge/docs/dex-architecture-revision-v2.md`;
- source SHA256: `29120e020b07e527cf1ae98d40f446539e5d569552a1ba9c4013ed2ad9564f4e`;
- adopted delivery path: M0, M1, M2, and M3 only;
- runtime repository and distribution name: `dex-manipulation`;
- runtime path: `/home/user/dex-manipulation`.

Confirmed initial decisions:

- hand: LinkerHand G20, left;
- serial recorded in the frozen calibration: `LHT20-010-415-L-B-1-D`;
- existing `thumb_cmc_roll` bias: retained as `-10 degrees` in the versioned
  TeleopProfile;
- policy package: canonical JSON manifest plus actor and adapter Safetensors,
  with SHA256 content identity;
- initial trust mode: explicitly allowed unsigned packages from a local
  immutable store;
- operator switch: F12;
- foot switch identity: PCsensor USB `3553:b001` at the configured evdev
  by-id path, with exclusive grab and debounce.

The architecture's post-M3 work remains deferred: the real arm adapter, Vive
arm stack, rich UI package, multi-process split, replay tooling, authenticated
capability tokens, and optional perception providers.

## Repository preservation

The existing dex-forge checkout was not reset or cleaned:

- branch at implementation start/final verification: `rand`;
- baseline commit: `ff3261d91b78b47110c0513244b602f40dffa9ef`;
- the checkout already contained a large dirty worktree and generated assets;
- implementation edits were limited to architecture/deployment files and the
  stale canonical-URDF assertions discovered by regression testing.

`/home/user/dex_teleop` was treated as read-only. Its pre-existing dirty files
and submodule state were not modified.

`/home/user/dex-manipulation` was an empty directory. It is now a Git repository
on branch `main`; no commit was created because commit/publish was not requested.

## M0 - mapping freeze

Software status: complete.

Implemented in dex-forge:

- immutable semantic joint schema for the 16 policy joints;
- immutable Linker calibration for the confirmed G20 left hand and serial;
- explicit calibration author value `unrecorded` where historical authorship
  could not be inferred;
- canonical superset URDF and content-addressed model manifest;
- five fixed fingertip links/joints without changing the 21-joint
  revolute/mimic actuation partition;
- pure semantic-to-native mapping and inverse diagnostics;
- golden mapping fixture, including the thumb slot 5/10 regression;
- M0 baseline inventory in dex-forge `docs/m0-baseline-2026-07-16.md`.

The frozen calibration and mapping golden fixture are copied into the runtime
repository and parity-tested rather than imported from dex-forge.

## M1 - teleoperation through the exclusive gateway

Software status: complete. Real-hand acceptance: pending.

Implemented:

- `dex_contracts`: immutable identity, timing, ownership, state, candidate,
  command, acknowledgement, readiness, and serialization contracts;
- `dex_teleop_adapters.ManusHandSource`: timestamped ROS 2 source, layout and
  side checks, health, and no actuator access;
- versioned TeleopProfile with canonical digest, pinned retargeting config,
  name-based projection, filter reset behavior, and explicit thumb bias;
- DexPilot retargeter factory with configuration-bound candidate TTL;
- `dex_hardware_linker.LinkerMapper`: frozen pure mapping boundary;
- `LinkerGateway`: the only component that opens the Linker transport, on one
  dedicated thread, with a bounded command channel, ownership epoch checks,
  command identity checks, deadlines, acknowledged-command lease renewal,
  mapping diagnostics, and watchdog containment;
- fake and pinned Linker SDK transports; the SDK import and CAN open remain
  lazy until gateway start;
- idempotent bootstrap that verifies or applies the pinned G20 driver patch;
- fake Manus candidate to authorized command to exclusive gateway regression.

Not executed in this environment:

- a live Manus ROS frame through DexPilot to the real hand;
- a real CAN transport open or servo command;
- M1 physical continuity and limit checks.

## M2 - hand-only mixed-control switching

Software status: complete. Real-hand acceptance: pending.

Dex-forge corrections:

- explicit `ProprioCodecSpec` in every deployable bundle;
- mounted policy: one 32-value frame at 10 Hz;
- free-object policy: last three 32-value frames, exact 96-value actor input,
  at 20 Hz;
- measured/effective-target encoding and golden traces shared by value, not by
  runtime imports;
- reset now requires measured position and acknowledged effective target and
  never substitutes home;
- CLI cadence cannot silently override the package cadence;
- Stage-2 direct export rejects missing or mismatched codec metadata;
- canonical policy-package exporter writes separate actor and adapter
  Safetensors with SHA256 digests and a canonical manifest.

Runtime implementation:

- strict canonical package validator, including rejection of non-standard
  non-finite JSON constants, descriptor-only registry scan, compatibility
  checks, and explicit unsigned-local trust;
- environment-free actor and adapter reconstruction;
- `PolicySession` lifecycle: loaded, shadow, active, deactivated, closed;
- reset from measured and effective positions, 30 fresh history ticks, exact
  cadence and sequence enforcement, continuous preview, same-tick activation,
  no double history advance, and explicit inference trace;
- provider-based readiness aggregation with initial required providers for
  operator confirmation, hand-state freshness/effective evidence, gateway
  health, and policy compatibility;
- semantic safety checks for position, per-tick delta, target rate, following
  error, state age, and command deadline;
- paired task ID/version identity on applicable candidates, commands, and
  acknowledgements, with mismatched session, hand, side, schema, task,
  version, package, or calibration rejected before actuation;
- deterministic fake arm hold/re-anchor gateway;
- full ownership state machine for teleop, continuous shadow, arm hold,
  hand blend, RL active, hand-back blend, re-anchor, teleop return, and safe
  hold;
- readiness is revalidated during hold preparation, hold verification, blend,
  and active execution;
- current-tick authorized command and gateway acknowledgement are retained for
  trace reconstruction;
- after hand-back, the selected policy is reset and re-primed in continuous
  shadow while teleoperation owns the hand;
- fake end-to-end teleop to policy to teleop cycle with declared discontinuity
  bound.

Not executed in this environment:

- the first physical teleop to RL to teleop cycle on the confirmed hand;
- physical following-error, discontinuity, and timing measurements;
- task-specific mounted/free-object commissioning.

## M3 - operability

Software status: complete. Physical pedal acceptance: pending.

Implemented:

- canonical JSONL event writer;
- bounded-rate JSONL control trace;
- event records for runtime lifecycle, operator confirmation, F12 requests,
  transitions, rejections, and faults;
- transition/request/rejection records include current readiness, command
  deadline, owner/epoch, policy identity, and gateway acknowledgement when a
  command occurred;
- control traces include Manus metadata (not raw payload), source health,
  teleop and policy candidates, policy codec input/latent/action/target,
  measured hand state, authorized command and safety decision, gateway
  acknowledgement, arbitration result, effective target, readiness, fake-arm
  hold state, native mapping preview, absolute scheduler timing/lateness, and
  switch status;
- terminal status for owners, epoch, source/gateway health, package,
  compatibility, shadow history, blend, readiness, recording, and rejection;
- PCsensor F12 evdev source with USB identity enforcement, KEY_F12 capability
  check, exclusive grab, press/release events, repeat suppression, and debounce;
- strict DeploymentBinding: unknown or missing fields fail before transport
  access;
- preflight verifies hand/calibration/profile/package identities, content
  digests, codec/rate, gateway rate, package/action limits, and calibrated
  envelope without opening hardware;
- `dex-runtime preflight`, `run`, `list-policies`, and `verify-package`;
- `run` displays preflight, starts the approved single-process topology,
  requests operator identity plus the exact `CONFIRM` token, and treats F12 as
  a gated toggle request only;
- SIGINT/SIGTERM request a normal bounded shutdown;
- tested teleop-only rollback path is documented in `operator-runbook.md`.

## Verification evidence

Final software checks:

- dex-manipulation complete suite: `36 passed`;
- dex-forge architecture/deploy focused suite: `51 passed`;
- dex-forge canonical Linker suites: `27 passed`, `1 skipped` because the
  Isaac Lab cfg was not importable in that pure test context;
- dex-forge Stage-2 algorithm/deploy reconstruction suite: `10 passed`;
- cross-repository export/validate test:
  package `sha256:5ec48713725633d9d7e724c18f8f8926216493fa29e3d532326a624d848ef373`
  exported in dex-forge and validated/loaded by dex-manipulation in a separate
  process;
- codec implementation parity SHA256:
  `e0c13144b940c1bc74755080baf08f6957b57aa1c147f76968422246af0f443d`;
- codec golden fixture parity SHA256:
  `cf7b2aa582becbb1ad0258e91c08f566e7734a35589e613ebcac9b70c4d2f3e1`;
- mapping golden fixture parity SHA256:
  `ccbee30e5881a990e5cf32d67df164e3056cbef075f647f1f8b9d0e9d763253a`;
- frozen calibration file parity SHA256:
  `9dfcb4b26cb0db69877b1b7ab23c07511996dfd5fd66169ed992daffc05368d0`;
- wheel build: `dex_manipulation-0.1.0-py3-none-any.whl`, SHA256
  `25949f91fd9b6737b27a11cba33478814a286388558451e18ae1518690774f36`;
- the wheel contains the console entrypoint, runtime composition, policy
  session, and canonical URDF;
- CLI help exposes only the four adopted commands.

Import-linter is pinned in the dev dependency group and CI configuration. It
was not installed in the shared verification environment, so a local
`lint-imports` result is not claimed. Source inspection confirmed the declared
direction: contracts are leaves, teleop has no hardware/runtime import,
hardware has no runtime import, dex-manipulation has no Isaac dependency, and
dex-forge/runtime exchange only golden fixtures and package files.

## Physical and release gates still open

Read-only capability checks on 2026-07-16 found:

- `/dev/input/by-id/usb-PCsensor_FootSwitch-event-kbd`: absent;
- USB `3553:b001`: not visible;
- ROS 2 CLI: unavailable in the current shell;
- a likely local CAN device node: absent/not available.

Therefore no live hardware command was attempted.

Before any live mixed-control release, the following remain required:

1. Confirm a real DeploymentBinding without inventing values. Still needed are
   the actual CAN channel, five speed values, five torque values, immutable
   package-store path and selected real package ID, task safety thresholds,
   logging paths, and timing/watchdog values.
2. Bring up and verify the independent robot E-stop path.
3. Connect and validate the PCsensor F12 device identity, exclusive grab, and
   observable rejection/transition behavior.
4. Validate Manus ROS source layout, side, rate, staleness, and recovery.
5. Execute M1 on the real hand at conservative limits.
6. Execute the complete M2 mounted-screwdriver cycle first, measuring forward
   and return discontinuity, following error, cadence, deadline, and watchdog
   response.
7. Exercise stale input/state, gateway/process loss, and E-stop responses.
8. Commission free-object rotation only after fixed-fixture gates pass.
9. Review a recorded trace and prove every ownership/command decision can be
   reconstructed.

This record does not declare a live mixed-control release. It declares the
adopted M0-M3 software path implemented and verified without hardware access.
