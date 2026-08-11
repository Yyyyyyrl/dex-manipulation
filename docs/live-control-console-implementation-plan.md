# Live Control Console implementation plan

Status: approved OpenXR/Quest UI implemented in fake mode; physical OpenXR, LinkerHand, D435, and Hitbot HIL pending  
Plan date: 2026-08-08 (America/Los_Angeles)  
Primary repository: `/home/user/dex-manipulation`  
Telemetry integration repository: `/home/user/dex_teleop`

## 2026-08-09 OpenXR / Quest 3S migration update

This section supersedes the Manus/Vive product path in the historical plan
below. The earlier sections remain as an implementation record; they are not
the current startup contract.

Current control topology:

```text
Quest 3S + WiVRn + XR_EXT_hand_tracking
                    |
                    v
        one VRHandReader producer
             /             \
            v               v
OpenXR 26 joints        left wrist 6DoF
      |                       |
      v                       v
DexPilot retarget       dex_teleop transforms
      |                       |
      v                       v
exclusive LinkerGateway  single Hitbot SDK owner
```

### Implementation checklist

- [x] Read and trace `/home/user/dex_teleop/main_new.py`, `VRHandReader`, the
  OpenXR-to-DexPilot mapping, wrist delta calculation, and robot-frame transform.
- [x] Add one latest-wins OpenXR producer with loopback UDP fanout so the hand
  and arm consume the same immutable Quest frame without opening OpenXR twice.
- [x] Validate the exact `openxr-hand-26-v1` joint names, parents, validity
  mask, side, focused session, monotonic sequence, and timestamps.
- [x] Add the versioned G20 OpenXR/DexPilot teleoperation profile.
- [x] Preserve `LinkerGateway` as the sole LinkerHand CAN/SDK owner.
- [x] Replace the Vive arm owner with one OpenXR wrist/Hitbot owner.
- [x] Apply target position as `current_tcp + robot_delta * 1000 * 0.7`.
- [x] Apply world-frame orientation deltas by left multiplication.
- [x] Pass the measured command interval to `ServoJ`, with a 0.05 second
  fallback on first command or a greater-than-one-second discontinuity.
- [x] Retain fixed-pose arm hold, heartbeat lease, stability verification,
  explicit re-anchor, and fail-closed release sequencing.
- [x] Replace all visible Manus/Vive UI labels with English OpenXR/VR labels.
- [x] Add OpenXR device/runtime/session/joint/rate/age/pinch monitoring.
- [x] Add VR-to-robot mapping and control-coupling panels.
- [x] Keep D435 RGB/depth, LinkerHand 16-joint layers, Hitbot TCP plots,
  readiness, latency, switch gate, and safe stop in one 1920x1080 view.
- [x] Keep all fonts bundled and digest-verified; no local font installation or
  network font dependency is used.
- [x] Add fake-only OpenXR, D435, LinkerHand, and Hitbot end-to-end validation.
- [ ] Run the supervised real Quest/WiVRn + LinkerHand + D435 observation gate.
- [ ] Run the staffed real Hitbot tracking gate with RL switching disabled.
- [ ] Run the separately authorized fixed-arm hold and RL handoff gate.

Implemented render: [vr-live-console-implemented.png](design/vr-live-console-implemented.png)  
Approved layout reference: [vr-live-console-layout-proposal-v1.png](design/vr-live-console-layout-proposal-v1.png)

### Acceptance notes

- Software evidence: focused OpenXR adapter/source/controller/UI tests pass;
  the fake aggregate snapshot reports healthy `runtime`, `openxr`, `linker`,
  `d435`, and `hitbot` sources with OpenXR/Linker sample correlation.
- Visual evidence: the implemented 1920x1080 headless render was captured from
  the live fake telemetry server, not from the design generator.
- Safety evidence: no hardware process was started during this implementation
  pass. The launcher still requires `CONFIRM`; the RL path additionally
  requires `--enable-rl-switch`, `ENABLE RL`, a reachable matching hold
  controller, and normal runtime readiness.
- Pending hardware note: software completion does not prove WiVRn session
  focus, physical coordinate direction, workspace scale, D435 USB health,
  Hitbot TCP/IK health, hold stability, or safe real motion. Record those as
  separate HIL evidence before routine use.

## 1. Objective

Deliver a clear, English-only live operator console for:

- Manus glove input used to control LinkerHand;
- LinkerHand G20 requested, authorized, effective, and measured motion;
- Hitbot arm wrist-tracker input, target TCP, actual TCP, IK, and servo health;
- Intel RealSense D435 RGB and colorized depth monitoring;
- mixed-control state, ownership, readiness, latency, faults, and safe operator
  actions.

The console must not depend on operating-system-installed fonts, external font
CDNs, JavaScript CDNs, or a second hardware connection. Monitoring must remain
read-only and must never block or change the timing of the control loops.

## 2. Confirmed product decisions

- [x] All visible UI text will be English.
- [x] No Chinese localization or language switch will be implemented.
- [x] Fonts will be bundled WOFF2 web assets, not operating-system fonts.
- [x] The first arm visualization will use truthful target/actual TCP plots,
  not an invented full 3D robot model.
- [x] Manus, LinkerHand, D435, and Hitbot are top-level monitoring areas. The
  approved desktop layout stacks Manus and LinkerHand on the left, gives D435
  the largest center panel, and keeps Hitbot tracking on the right.
- [x] Actual/measured data uses cyan and requested/target data uses amber.
- [x] Existing switch and stop actions remain visually prominent.
- [x] UI telemetry is read-only; it must not issue hand or arm target commands.
- [x] Hitbot remains controlled by `dex_teleop` during this project phase.
- [x] The implementation must work in fake/replay mode without physical
  hardware.

## 3. Current implementation findings

### 3.1 Web UI

The current demo UI is embedded as one large HTML/CSS/JavaScript string in
`tools/switch_web_demo.py`.

Known limitations:

- the CSS font stack uses `system-ui`, `Segoe UI`, and `PingFang SC`, producing
  machine-dependent fallback and rendering;
- status is polled every 100 ms and glove geometry every 60 ms;
- the page is a narrow single-column layout rather than an operator console;
- the Manus plot is a simple projected skeleton;
- LinkerHand displays five selected joint values rather than all 16 semantic
  joints;
- there is no arm telemetry panel;
- no explicit sequence, source rate, dropped-frame, or stale-source information
  is displayed.

### 3.2 Manus data path

`tools/manus_glove_bridge.py` forwards a 25-node ROS 2 Manus skeleton to the
web demo over local UDP. The current payload is sufficient for a basic skeleton
but does not carry the complete monitoring metadata required by the new UI.

The current real-glove UI path is not the runtime control path:

- the browser receives real Manus geometry from the UDP bridge;
- the demo runtime is still constructed with `VirtualManusSource` and
  `VisibleWaveRetargeter`;
- therefore the displayed real glove cannot currently prove which sample
  generated a LinkerHand target.

The new telemetry tap must observe the exact sample consumed by the retargeter.

### 3.3 LinkerHand data path

The exclusive Linker gateway already owns hardware access and holds the data
needed for safe monitoring:

- measured semantic position;
- requested and authorized commands;
- last effective target;
- owner and control epoch;
- gateway acknowledgement and state freshness;
- watchdog and hardware fault state;
- semantic-to-native mapping diagnostics.

The UI must read immutable copies published by the runtime. It must not import
the Linker SDK or open a second CAN connection.

Optional current, speed, or force telemetry may be added later, but those
queries must execute inside the existing gateway thread and be copied into the
published state.

### 3.4 Hitbot data path

`/home/user/dex_teleop/dex_teleop/arm_controller.py` currently performs:

1. wrist-tracker delta calculation;
2. tracker-to-robot coordinate transformation;
3. actual TCP read;
4. target TCP calculation;
5. inverse kinematics;
6. `ServoJ` command submission.

The relevant values are currently printed or remain local variables. There is
no typed telemetry sample, source health, sequence number, or non-blocking
publisher.

The Hitbot network client uses shared mutable request data. The UI must not
read from that socket directly or create concurrent query threads.

## 4. Target architecture

```text
CONTROL PLANE

Manus ROS2/SDK -> exact runtime sample -> retargeter -> safety/arbitration
                                                     -> Linker gateway

Wrist tracker -> ArmController transform -> IK -> existing Hitbot ServoJ path


READ-ONLY TELEMETRY PLANE

exact Manus sample -----------+
runtime/Linker snapshot -------+--> latest-value TelemetryHub
ArmController observer --------+              |
                                              +--> /api/snapshot
                                              +--> /api/live (SSE)
                                                        |
                                                        v
                                                browser console

CONTROL REQUESTS

browser -> existing POST switch/stop endpoints -> runtime request boundary
```

### Architectural rules

- The control plane never waits for the telemetry plane.
- Every producer performs a non-blocking latest-value publish.
- Slow browsers lose intermediate display frames rather than creating backlog.
- Hardware clients remain exclusively owned by their current controller or
  gateway.
- The browser cannot send joint, TCP, IK, or servo targets.
- All live messages carry monotonic time and sequence identity.
- Wall-clock time is display metadata only and is never used for freshness.
- A disconnected UI has no effect on the running robot.
- A stale source is visually explicit and cannot appear healthy.

## 5. Proposed telemetry contract

### 5.1 Common envelope

```json
{
  "schema_version": 1,
  "source": "manus | linker | hitbot | runtime",
  "sequence": 0,
  "sample_monotonic_ns": 0,
  "received_monotonic_ns": 0,
  "age_ms": 0.0,
  "rate_hz": 0.0,
  "dropped_since_last": 0,
  "health": "healthy | degraded | stale | fault"
}
```

### 5.2 Manus payload

Required fields:

- glove ID and side;
- exact input sample sequence;
- node ID, parent ID, position, and validity for all available nodes;
- layout identity and node count;
- source timestamp, receive timestamp, source rate, and age;
- retargeter candidate sequence generated from that sample;
- optional ergonomics data when supplied by the SDK.

### 5.3 LinkerHand payload

Required fields:

- 16 measured semantic joint positions;
- 16 requested target positions;
- 16 authorized target positions;
- 16 effective target positions;
- per-joint following error;
- hand owner, control epoch, command identity, and acknowledgement;
- state age, command age, gateway rate, watchdog state, and hardware faults;
- optional 20-slot native mapping preview for diagnostics.

### 5.4 Hitbot payload

Required fields:

- tracker pose and transformed robot-frame delta;
- actual TCP position and orientation;
- requested TCP position and orientation;
- IK target joints and IK result;
- servo command result and actual servo interval;
- TCP query, IK, and servo timing;
- tracking enabled state, health, failure reason, and consecutive failure count.

### 5.5 Aggregate browser snapshot

The server will emit one aggregate snapshot at a display rate of 20 Hz. Each
source retains its independent sequence, time, and health. Aggregation must not
pretend that samples were captured simultaneously.

## 6. Visual and interaction specification

### 6.1 Layout

Desktop-first 12-column layout:

- header: session state, owner, and source-health chips;
- left panel: `MANUS TRACKING`;
- center panel: `LINKERHAND G20`;
- right panel: `HITBOT ARM`;
- lower panel: mode timeline, readiness, and latency history;
- fixed action rail: `SWITCH TO RL` and `SAFE STOP`.

The layout must remain usable at 1280 x 720 and scale cleanly on high-DPI
displays. Mobile control is not a release requirement.

### 6.2 Color semantics

| Meaning | Color |
|---|---|
| Actual/measured | `#56D4FF` |
| Target/requested | `#F7B955` |
| Active owner/control | `#4C7DFF` |
| Healthy/ready | `#45D483` |
| Fault/stop | `#FF5C6C` |
| Background | `#0B0F14` |
| Panel | `#111923` |

Red must be reserved for faults, stale safety-critical data, and stop actions.

### 6.3 Font delivery and rendering

Planned assets:

```text
assets/fonts/ui-regular.woff2
assets/fonts/ui-bold.woff2
assets/fonts/ui-mono.woff2
assets/fonts/LICENSE.txt
assets/fonts/SHA256SUMS
```

Requirements:

- Latin-only glyph coverage because the complete UI is English;
- no Google Fonts, font CDN, or operating-system font path;
- approved redistribution license recorded beside the assets;
- asset digests recorded and tested;
- `font-synthesis: none`, normal kerning, stable line heights, and whole-pixel
  layout for primary labels;
- telemetry numbers use the bundled monospaced face and tabular numerals;
- the application waits for required font faces before revealing the console;
- a missing or invalid font asset produces a visible startup fault rather than
  silently switching to an unverified system font.

### 6.4 Graphics

- DOM elements render all text and controls.
- SVG renders the Manus skeleton, axes, and compact vector diagrams.
- High-DPI Canvas renders rolling traces and TCP trails.
- Canvas backing dimensions are multiplied by `devicePixelRatio`.
- No external charting or 3D library is required in the first release.
- The arm panel uses synchronized top XY and side XZ plots until real joint
  feedback and an approved robot model are available.

## 7. Implementation steps

## Step 0 - Freeze scope, baselines, and safety boundaries

### Goal

Record the approved UI scope and prove that the existing control behavior is
unchanged before implementation begins.

### Checklist

- [x] Confirm this plan and the English-only visual design with the operator.
- [x] Record the starting Git commit and dirty-file inventory in both
  repositories.
- [x] Preserve all pre-existing uncommitted changes.
- [x] Record current fake-runtime test results.
- [x] Record current import-linter results.
- [x] Record current web-demo behavior and endpoint inventory.
- [x] Confirm that only switch and stop are writable browser actions.
- [x] Confirm that real arm control integration remains out of scope.
- [x] Define source stale thresholds and display-rate budgets.
- [x] Define the required browsers and minimum display resolution.

### Acceptance criteria

- The baseline is reproducible and contains no physical actuation.
- Scope explicitly separates monitoring from hand and arm control.
- Existing dirty worktrees are documented and preserved.

### Acceptance notes

- Status: `ACCEPTED - SOFTWARE BASELINE`
- Reviewer: Codex implementation pass; operator approved work from this plan.
- Review date: 2026-08-08
- Baseline commit(s): dex-manipulation
  `e82bb53e475956c6f3fc29fccebccfa10ed8b070`; local `main` equals
  `origin/main`. The dex_teleop baseline will be frozen immediately before
  Step 5 because Step 0 makes no changes in that repository.
- Dirty-worktree evidence: pre-existing modifications in `bootstrap.sh`,
  `pyproject.toml`, four runtime modules, and two tests were inspected and
  preserved. Pre-existing untracked `.claude/` and `tools/` were preserved.
- Test evidence: `.venv/bin/python -m pytest -q` -> `37 passed in 7.09s`;
  `.venv/bin/lint-imports` -> `3 kept, 0 broken`.
- Endpoint evidence: current GET endpoints are `/`, `/api/status`, and
  `/api/glove`; current POST endpoints are `/api/switch` and
  `/api/stop`. No joint, TCP, IK, or servo target endpoint exists.
- Timing/display decisions: aggregate browser display rate 20 Hz; Manus source
  validation threshold 100 ms and completed-frame display timeout 300 ms;
  Linker completed-frame timeout 250 ms while its gateway remains 50 Hz;
  Hitbot listener timeout 500 ms.
- Browser/display decisions: current stable Chromium and Firefox; minimum
  supported viewport 1280 x 720; high-DPI scaling through 200 percent.
- Exceptions or deviations: `.venv/bin/pytest` is not installed, so the
  equivalent module command was used. No physical hardware was opened.
- Follow-up actions: repeat the same suite and import contracts after Gate A.

## Step 1 - Extract the UI package and bundle fonts

### Goal

Replace the embedded demo page with a maintainable English-only static UI and
deterministic font rendering.

### Planned files

```text
tools/control_console/
  server.py
  assets/index.html
  assets/app.css
  assets/app.js
  assets/fonts/*
```

`tools/switch_web_demo.py` will become a thin compatibility launcher or be
replaced after its callers are migrated.

### Checklist

- [x] Select the redistributable Latin sans and monospaced font families.
- [x] Verify the font license and save it with the assets.
- [x] Generate or acquire deterministic WOFF2 files.
- [x] Record SHA256 for every font asset.
- [x] Add explicit `@font-face` rules for every used weight.
- [x] Disable synthesized font weights and styles.
- [x] Add a required-font startup check.
- [x] Extract HTML, CSS, and JavaScript from the Python string.
- [x] Implement the 12-column desktop layout.
- [x] Implement source-health chips and the fixed action rail.
- [x] Implement responsive behavior down to 1280 x 720.
- [x] Preserve keyboard shortcuts and visible focus styles.
- [x] Ensure all UI strings are English.
- [x] Confirm there are no external runtime asset requests.

### Acceptance criteria

- The console renders identically on a clean machine with no project-specific
  system fonts installed.
- All required font requests are served locally and pass digest verification.
- No visible UI string is Chinese or pseudo-localized.
- The UI is readable at 100%, 125%, 150%, and 200% display scale.
- Keyboard focus, F12, and stop controls remain visible and operable.

### Acceptance notes

- Status: `IMPLEMENTED - CHROMIUM ACCEPTED; FIREFOX PENDING`
- Reviewer: Codex implementation and visual inspection.
- Review date: 2026-08-08
- Font family and version: Noto Sans regular/bold and Noto Sans Mono regular,
  source package `fonts-noto-core 20201225-2`; Latin WOFF2 subsets generated
  by fontTools 4.55.3 and Brotli 1.1.0.
- Font license evidence:
  `tools/control_console/assets/fonts/LICENSE.txt` contains SIL OFL 1.1;
  source file hashes and build parameters are recorded in `SOURCE.md`.
- Asset digest evidence: `sha256sum -c SHA256SUMS` reports `OK` for all
  three WOFF2 files and `LICENSE.txt`; the server verifies font digests before
  binding the HTTP port.
- Screenshots and tested display scales: Chrome 148.0.7778.178 rendered healthy
  fake-runtime captures at 1536 x 960 and minimum 1280 x 720, plus device-scale
  captures at 125, 150, and 200 percent. Evidence is under
  `.artifacts/control-console/`.
- English/offline evidence: automated assets test rejects CJK UI strings,
  `system-ui`, PingFang, and external HTTP(S) resources. The legacy embedded
  page and handler were removed.
- Exceptions or deviations: the approved source environment has no Firefox
  executable, so Firefox visual verification remains open for Step 7. The
  delivered font set uses the two weights actually referenced by CSS instead
  of unused medium and semibold files.
- Follow-up actions: repeat visual checks in current stable Firefox before
  release; retain the startup font-fault gate and SHA256 checks.

## Step 2 - Implement the read-only TelemetryHub and SSE channel

### Goal

Create a typed, bounded, latest-value telemetry path that cannot delay the
control loop.

### Planned files

```text
src/dex_runtime/telemetry.py
tools/control_console/telemetry.py
tools/control_console/server.py
tests/test_telemetry.py
tests/test_control_console.py
```

### Checklist

- [x] Define immutable source and aggregate telemetry dataclasses.
- [x] Add schema version, sequence, monotonic time, age, rate, drop count, and
  health to every source.
- [x] Implement a bounded latest-value slot per source.
- [x] Prove that publish operations do not block on browser or network work.
- [x] Implement `/api/snapshot` for initial state and diagnostics.
- [x] Implement `/api/live` as Server-Sent Events.
- [x] Emit aggregate display snapshots at 20 Hz.
- [x] Add an SSE heartbeat and automatic browser reconnect.
- [x] Keep existing switch and stop POST boundaries separate from telemetry.
- [x] Reject malformed, oversized, non-finite, or unsupported telemetry.
- [x] Add explicit `STALE` and `FAULT` source states.
- [x] Add fake producers for Manus, LinkerHand, Hitbot, and runtime status.
- [x] Add deterministic schema and reconnect tests.

### Acceptance criteria

- A stalled or disconnected browser cannot block a producer.
- Memory use remains bounded with zero connected clients and with slow clients.
- Intermediate UI frames may be dropped, but the latest valid snapshot is
  eventually displayed.
- SSE reconnect restores a complete snapshot without restarting the runtime.
- Invalid payloads produce source-local faults and do not crash the server.

### Acceptance notes

- Status: `ACCEPTED - GATE A SOFTWARE`
- Reviewer: Codex implementation pass.
- Review date: 2026-08-08
- Measured publish latency: 20,000 single-source replacements averaged
  0.239 microseconds per `TelemetryHub.publish` on this host; the final
  snapshot retained one source and revision 20,000.
- Slow-client/drop test evidence: the hub stores exactly one immutable envelope
  per source; SSE clients read snapshots and own no producer queue. Unit tests
  replace 100 unread values and prove only sequence 99 remains.
- SSE reconnect evidence: HTTP integration test opens, closes, and reconnects
  to `/api/live`, receiving a complete snapshot on both connections. The
  browser uses native EventSource automatic reconnect and a one-second stale
  indicator.
- Schema version: 1.
- Test evidence: targeted telemetry/console suite `8 passed`; complete
  repository suite `45 passed in 7.48s`; import-linter `3 kept, 0 broken`;
  JavaScript syntax and `git diff --check` passed.
- Fake integration evidence: live snapshot contained exactly `runtime`,
  `manus`, `linker`, and `hitbot`; Manus reported 25 nodes, Linker 16
  semantic joints, and Hitbot a 60-point synthetic trail. Single-browser
  capture left runtime and Linker healthy.
- Exceptions or deviations: a deliberately CPU-heavy parallel two-browser
  capture caused the existing demo runtime to reject an expired command. The
  UI correctly showed `FAULT` and `LINKER STALE`; sequential captures
  remained healthy. Formal timing/load budgets remain a Step 7 release gate.
- Follow-up actions: connect exact Manus/runtime and authorized-command
  telemetry in Steps 3-4 without changing the hub or SSE boundary.

## Step 3 - Attach Manus monitoring to the exact control sample

### Goal

Ensure the Manus visualization represents the same sample that generated the
retargeted LinkerHand command.

### Planned files

```text
tools/manus_glove_bridge.py
src/dex_teleop_adapters/manus.py
src/dex_runtime/application.py            # telemetry tap only
tools/control_console/telemetry.py
tests/test_manus_telemetry.py
```

### Checklist

- [x] Inventory the exact production Manus source and retargeter entrypoints.
- [x] Version the local bridge payload.
- [x] Add glove ID, side, layout, validity, source timestamp, and source
  sequence.
- [x] Add source rate, received time, and dropped-frame accounting.
- [x] Attach telemetry after source validation and before/at retargeting.
- [x] Carry the Manus sample identity into the teleop candidate telemetry.
- [x] Remove or clearly label any visualization-only sample path.
- [x] Render the 25-node skeleton with explicit coordinate axes.
- [x] Show `HEALTHY`, `DEGRADED`, `STALE`, and layout mismatch states.
- [x] Add fake, malformed, stale, wrong-side, and node-loss tests.
- [x] Verify that telemetry serialization never runs on the ROS callback's
  critical path when it could block.

### Acceptance criteria

- The UI sample ID can be matched to the retargeted candidate generated from
  that sample.
- Wrong-side, wrong-layout, stale, and incomplete samples are clearly visible.
- The production control path does not depend on whether the UI is running.
- The previous condition where real Manus was display-only cannot appear as a
  normal healthy control state.

### Acceptance notes

- Status: `ACCEPTED - SOFTWARE/FAKE E2E + REAL MANUS GATE B HIL`
- Reviewer: Codex implementation and correlation review.
- Review date: 2026-08-08
- Manus SDK/ROS message version: configured
  `manus_ros2_msgs/ManusGlove` topic `/manus_glove_0`; local UDP schema 1,
  layout `manus-raw-25-v1`, 25 fixed parent-indexed nodes.
- Sample-to-candidate correlation evidence: final fake snapshot showed Manus
  source sequence 710, teleop candidate source sequence 710, Linker control
  sample sequence 710, `control_correlated=true`, and
  `drives_current_command=true`.
- Tested source rate and stale threshold: bridge fake source 30 Hz; strict
  source validation 100 ms. The UI envelope allows 300 ms only because it
  represents the last completed 10 Hz control tick; source health from that
  exact tick remains authoritative.
- Fault injection evidence: schema/layout/side/parent/count/valid-mask and
  non-finite rejection, sequence gaps/reordering, source stale, and display-only
  X mirroring are covered. A deliberately blocked JSON serializer proves the
  callback-side single-slot offer returns before serialization completes.
- Authorized HIL evidence (2026-08-09): real `/manus_glove_0` produced the
  fixed 25-node `manus-raw-25-v1` layout at approximately 120 Hz. The combined
  60-second observer collected 300 samples with no failures and preserved exact
  Manus candidate sequence = Linker control sample sequence = 15,813.
- Exceptions or deviations: the ROS root node reports itself as its parent;
  the bridge now normalizes only node 0 to the schema root parent `-1` before
  validation. A regression test preserves that boundary behavior.
- Evidence: `.artifacts/control-console/hil-manus-linker-hitbot-20260809.json`
  and `.artifacts/control-console/live-manus-linker-hitbot-20260809.png`.
- Follow-up actions: none for Gate B monitoring; real-policy release remains a
  separate Gate D activity.

## Step 4 - Publish full LinkerHand command and state telemetry

### Goal

Visualize all 16 semantic joints and distinguish requested, authorized,
effective, and measured values without opening another hardware connection.

### Planned files

```text
src/dex_hardware_linker/gateway.py
src/dex_runtime/application.py
src/dex_runtime/telemetry.py
tools/control_console/assets/app.js
tests/test_linker_telemetry.py
```

### Checklist

- [x] Publish immutable copies from the exclusive gateway/runtime boundary.
- [x] Include all 16 semantic joint names in stable order.
- [x] Publish requested, authorized, effective, and measured position vectors.
- [x] Calculate per-joint following error with explicit units.
- [x] Publish owner, epoch, command identity, and acknowledgement.
- [x] Publish state age, command age, gateway rate, watchdog, and faults.
- [x] Include optional native 20-slot mapping diagnostics.
- [x] Render all 16 joint tracks with actual/target overlays.
- [x] Add joint selection and summary maximum/RMS error without hiding faults.
- [x] Add clear stale-state and missing-acknowledgement rendering.
- [x] Prove that the UI does not import or instantiate the Linker SDK.
- [x] Add fake-gateway correlation, epoch mismatch, stale state, and fault tests.

### Acceptance criteria

- Every displayed measured value originates from the exclusive gateway state.
- Every target layer has an explicit identity and cannot be confused with an
  actual measurement.
- Owner, epoch, and acknowledgement agree with the runtime trace for the same
  sequence.
- UI start, stop, refresh, or failure does not open CAN or alter gateway rate.

### Acceptance notes

- Status: `ACCEPTED - SOFTWARE/FAKE E2E + REAL LINKER GATE B HIL`
- Reviewer: Codex same-tick identity and UI-layer review.
- Review date: 2026-08-08
- Joint schema/version: `linker-g20-left-semantic-16-v1`, 16 semantic joints in
  frozen calibration order; optional native mapping contains all 20 slots.
- Target-to-ack correlation evidence: final fake snapshot at runtime tick 237
  showed control sample/candidate source sequence 710, 16 joints, and exact
  authorized command ID = gateway acknowledgement ID = acknowledgement
  effective target ID = runtime effective target ID, with `SENT_TO_BUS` evidence.
- Gateway timing comparison with UI on/off: fake gateway remained 50 Hz and
  immutable correlated runtime frames 10 Hz. The repeatable 300-second soak
  measured no-viewer scheduler lateness over 402 ticks at mean 0.174396 ms,
  p95 0.260991 ms, max 0.674668 ms. Two refresh viewers plus one unread slow
  SSE connection covered 2,705 ticks at mean 0.199650 ms, p95 0.277144 ms,
  max 4.361074 ms.
- Fault injection evidence: command ID mismatch, control/state epoch mismatch,
  missing acknowledgement, stale state, and gateway fault render as distinct
  source-local states; no runtime-source fault is synthesized from a Linker-only
  telemetry fault.
- Authorized HIL evidence (2026-08-09): the exclusive Linker SDK gateway
  connected to `can0`, reported serial `LHT20-010-415-L-B-1-D`, sustained
  10 Hz telemetry, retained owner `teleoperation`, epoch 1, and exact
  authorized = acknowledged = effective command identity throughout the
  passing 60-second observer.
- Real-CAN commissioning note: a 50 ms command deadline faulted when the SDK's
  state-read plus send path exceeded that budget. The HIL demo deadline is now
  exactly one 100 ms control period; the one-second gateway watchdog is
  unchanged. The subsequent combined observer passed with no fault samples.
- Exceptions or deviations: current/speed/force telemetry remains optional and
  absent. This pass used the bounded synthetic policy in `RL_SHADOW`; it does
  not release real-policy ownership or F12 activation.
- Evidence: `.artifacts/control-console/hil-manus-linker-hitbot-20260809.json`
  and runtime logs `/tmp/dex-switch-demo-xrdkmdix`.
- Follow-up actions: measure UI-disabled/enabled/slow-viewer timing in a longer
  release-grade real-hardware soak before Gate D.

## Step 5 - Add Hitbot tracking telemetry in dex_teleop

### Goal

Publish a read-only snapshot of each existing wrist-tracking control cycle
without adding robot queries or changing command ownership.

### Planned files

```text
/home/user/dex_teleop/dex_teleop/arm_controller.py
/home/user/dex_teleop/dex_teleop/hitbot_utils/hitbot_interface.py
/home/user/dex_teleop/dex_teleop/arm_telemetry.py
/home/user/dex_teleop/dex_teleop/arm_telemetry_bridge.py
tools/control_console/arm_listener.py
tests/test_arm_telemetry.py
```

Changes under `/home/user/dex_teleop` require a separate dirty-worktree review
before editing.

### Checklist

- [x] Record the dex_teleop commit, submodule state, and dirty-file inventory.
- [x] Define an immutable `ArmTelemetrySample`.
- [x] Add a non-blocking observer callback to `ArmController`.
- [x] Capture tracker pose and transformed robot-frame delta.
- [x] Capture the already-read actual TCP without an additional robot query.
- [x] Capture target TCP, IK target, IK result, and ServoJ result.
- [x] Measure TCP query, IK, total cycle, and servo interval timing.
- [x] Publish failure reason and consecutive failure count.
- [x] Implement a localhost-only UDP or Unix datagram publisher.
- [x] Implement a bounded latest-value listener in the console server.
- [x] Do not expose a browser endpoint that accepts arm targets.
- [x] Do not create a second Hitbot connection or concurrent socket reader.
- [x] Add fake/replay tests before any real-arm observation.
- [x] Add top XY and side XZ actual/target trails to the UI.
- [x] Add an orientation-axis glyph and explicit millimeter/degree units.

### Acceptance criteria

- Existing Hitbot command ordering and ownership remain unchanged.
- Enabling telemetry adds no robot query and no control-loop wait.
- UI actual TCP equals the value used by the corresponding control cycle.
- UI target TCP and IK result correlate by sequence with that cycle.
- Killing the bridge or browser has no effect on arm tracking.
- Fake/replay mode exercises the complete arm panel without hardware.

### Acceptance notes

- Status: `SOFTWARE ACCEPTED; SERVO_DT HITBOT REVALIDATION PENDING`
- Reviewer: Codex cross-repository preservation and observer review.
- Review date: 2026-08-08
- dex_teleop baseline commit and dirty state: commit
  `4e4a8d38dad56764454fd2d9a5985244cec1fe67`; dex-retargeting submodule
  `3f56141bc8bd2760d5e452e382937269554ebb21` (`v0.5.0`). The repository was
  already heavily dirty, including operator calibration/ServoJ edits in
  `arm_controller.py` and an existing `exp_vive2arm.py`; those edits were
  preserved and instrumented in place.
- Observer overhead measurement: 1,000 offers at 1 kHz were all accepted with
  mean 4.038 microseconds, p99 7.126 microseconds, max 12.811 microseconds, zero
  replacement/contention/send errors. A 20,000-offer saturation burst remained
  non-blocking (mean 0.363 microseconds, max 22.004 microseconds) and dropped
  contention rather than waiting, as designed.
- Command-order comparison: fake interface proves exactly one existing actual
  TCP query followed by IK then ServoJ; IK failure emits telemetry and never
  calls ServoJ. The observer error path is fail-isolated from motion.
- Fake/replay evidence: cross-repository publisher/listener schema 1 preserves
  sequence, actual/target TCP, IK/servo result, cycle timing, failures, and
  bounded 200-point trails. The full fake UI shows a 60-point synthetic trail.
- Authorized HIL evidence (2026-08-09): the existing Vive/Hitbot owner produced
  live tracker, actual/target TCP, IK, ServoJ, and timing telemetry at about
  13 Hz. The final observer collected 300 samples with no failures; Hitbot
  source age mean/p95/max was 72.152/105.729/110.719 ms.
- Historical real-controller correction: measured ServoJ interval had been fed
  back into the next ServoJ `t`, creating a positive timing ramp toward
  600-1000 ms.
  Command duration is now fixed at 50 ms while measured interval remains
  telemetry-only. Real cycles then remained approximately 75-79 ms, and the UI
  marks cycle latency above 200 ms or servo interval above 150 ms degraded.
- Operator-directed ServoJ update (2026-08-09): `rob_move_with_increase` now
  passes the measured send-to-send interval to ServoJ as `t`. The first command
  and any interval above one second use the 50 ms fallback. Existing telemetry,
  command ordering, IK-failure skip, and hold control remain intact. Mock-only
  dex_teleop tests pass 13/13; the earlier fixed-50-ms HIL timing evidence does
  not validate this new behavior, so bounded staffed Hitbot revalidation is
  required before restoring physical acceptance.
- Live-indicator correction (2026-08-09): recording
  `Screencast from 2026-08-09 23-03-27.webm` showed simultaneous Manus/Linker
  health-chip flicker. The matching run trace reached 8.543 seconds scheduler
  lateness. Root cause was the arm-hold status property sharing the same lock as
  a 350 ms UDP probe: delayed Hitbot responses held that lock while the hand
  control thread tried to serialize read-only arm status. `RealArmGateway` now
  separates request serialization from non-blocking status snapshots. The raw
  stale thresholds remain unchanged; this fixes cross-subsystem blocking rather
  than masking the health transition in JavaScript.
- Exceptions or deviations: the UI listener timeout remains 500 ms. No second
  Hitbot connection or added robot query exists; existing controller ownership
  and command ordering are unchanged.
- Evidence: `.artifacts/control-console/hil-manus-linker-hitbot-20260809.json`
  and `.artifacts/control-console/live-manus-linker-hitbot-20260809.png`.
- Follow-up actions: longer release-grade timing/visibility soak remains Gate D.

## Step 6 - Complete dashboard rendering and operator behavior

### Goal

Join the three sources into the approved visual hierarchy while preserving
clear control-state and fault semantics.

### Checklist

- [x] Implement the final header and runtime-state chip.
- [x] Implement independent health chips for Manus, LinkerHand, and Hitbot.
- [x] Implement Manus perspective skeleton and metrics.
- [x] Implement LinkerHand 16-joint overlays and error summary.
- [x] Implement Hitbot XY/XZ plots and TCP metrics.
- [x] Implement the mixed-control state timeline.
- [x] Implement the readiness provider checklist.
- [x] Implement bounded rolling latency traces.
- [x] Display source age and stale/fault state without relying on color alone.
- [x] Display explicit units for every physical value.
- [x] Preserve visible switch rejection and safe-stop feedback.
- [x] Add accessible focus states, labels, and contrast.
- [x] Prevent accidental double submission of switch/stop requests.
- [x] Add explicit time-bounded operator-confirmation refresh without coupling
  it to F12 or automatic renewal.
- [x] Make all UI state reconstructable from the current aggregate snapshot.
- [x] Verify every visible string is English.

### Acceptance criteria

- An operator can identify runtime state, owner, readiness, and all source
  health states without scrolling at 1280 x 720.
- Actual and target values remain distinguishable without relying only on
  color.
- Source staleness is visible within the approved threshold.
- No panel displays synthetic healthy values when its real source is absent.
- Switch and stop feedback is immediate, explicit, and sequence-aware.

### Acceptance notes

- Status: `ACCEPTED - SOFTWARE VISUAL REVIEW`
- Reviewer: Codex visual and interaction review; operator sign-off pending.
- Review date: 2026-08-08
- Tested resolutions/scales: Chromium 148 at 1280 x 720 and 1536 x 960;
  device scales 100, 125, 150, and 200 percent.
- Accessibility/contrast evidence: DOM text/controls, explicit source-state
  words, shape-distinct Linker layers, ARIA labels, keyboard-selectable joints,
  visible `:focus-visible`, and red reserved for fault/stop. Color is never the
  only state or target-layer identifier.
- Operator review notes: all visible strings are English; synthetic sources are
  visibly labelled; readiness lists exact OPERATOR/HAND STATE/GATEWAY/POLICY
  evidence rather than hard-coding a count.
- Screenshot references:
  `.artifacts/control-console/final-fake-readiness-1280x720.png` plus the
  DPI/resolution captures in the same directory, and the real-source capture
  `.artifacts/control-console/live-manus-linker-hitbot-20260809.png`.
- Exceptions or deviations: Firefox is not installed in the review environment.
  Browser-side latency arrays were replaced by bounded server-side 10-second
  rings so refresh/reconnect reconstructs the plots from one snapshot.
- Follow-up actions: obtain operator and current-stable-Firefox visual sign-off.

## Step 7 - Verification, HIL gates, and release handoff

### Goal

Prove that the console is accurate, bounded, non-actuating, and operationally
safe before it is used during live control.

### Checklist

- [x] Run the complete dex-manipulation test suite.
- [x] Run import-linter and preserve the architecture boundaries.
- [x] Run schema, malformed-input, stale-source, and reconnect tests.
- [x] Run font asset, license, digest, and offline-load tests.
- [x] Run fake end-to-end Manus -> Linker -> UI correlation.
- [x] Run fake/replay Hitbot tracking visualization.
- [ ] Measure runtime timing with UI disabled, enabled, disconnected, and slow.
- [x] Confirm bounded memory over an extended run.
- [x] Confirm browser refresh and multiple viewers do not affect control.
- [x] Confirm no second CAN, Linker SDK, or Hitbot connection is opened.
- [x] Review event/trace correlation for owner, epoch, target, state, and ack.
- [x] Perform read-only Manus and Linker HIL observation after authorization.
- [x] Perform read-only Hitbot HIL observation after authorization.
- [x] Exercise source loss and stale/fault displays without issuing motion.
- [x] Update `docs/operator-runbook.md` with console startup and interpretation.
- [x] Add a one-command live supervisor with duplicate-owner checks, explicit
  confirmation, health waits, per-process logs, and ordered safe shutdown.
- [x] Record the final test evidence and all remaining physical gates.

### Acceptance criteria

- All automated checks pass and are recorded with exact commands/results.
- Control-loop timing remains within its approved budget with the UI enabled.
- The console remains non-actuating except for the pre-existing switch/stop
  request endpoints.
- Displayed sequences correlate with runtime and controller evidence.
- Physical use remains blocked until existing E-stop and HIL release gates pass.
- The operator runbook explains startup, stale/fault states, and shutdown.

### Acceptance notes

- Status: `SOFTWARE + AUTHORIZED MONITORING HIL ACCEPTED; CONTROL RELEASE BLOCKED`
- Reviewer: Codex automated, trace, and fake-system review.
- Review date: 2026-08-09
- dex-manipulation test evidence: `.venv/bin/python -m pytest -q` ->
  `79 passed in 7.59s`; launcher dry-run, Bash syntax, JavaScript syntax,
  Python compilation, and `git diff --check` passed.
- Fake full-cycle evidence: the live console completed `RL_SHADOW` ->
  `RL_ACTIVE` (policy owner, epoch 3) -> automatic hand-back -> `RL_SHADOW`
  (teleoperation owner, epoch 5) with `fault=null`. Regression coverage now
  preserves exact manifest limits across float32 inference and freezes each
  handoff blend's entry target so interpolation cannot compound its step size.
- dex_teleop test evidence: focused `tests/test_arm_telemetry.py` run with the
  dex-manipulation virtual environment -> `4 passed in 0.04s`; compilation and
  scoped `git diff --check` passed.
- Import-linter evidence: analyzed 37 files and 68 dependencies; all three
  contracts kept, zero broken.
- Timing and memory evidence: the hardware-forbidden 300-second verifier passed
  with no unhealthy sample. No-viewer mean/p95/max lateness was
  0.174396/0.260991/0.674668 ms; two refresh viewers plus one unread slow SSE
  produced 0.199650/0.277144/4.361074 ms. The viewers completed 5,213 and 5,214
  snapshot refreshes without error. RSS began at 596,644 KiB, peaked at
  599,520 KiB, and grew 2,876 KiB, below the 32 MiB budget. All three latency
  histories remained exactly 200 points, final revision was 25,028, and the
  four correlated Manus/Linker sequences were all 9,331. Evidence:
  `.artifacts/control-console/fake-soak-300s.json`.
- Offline/font evidence: local WOFF2 digests and license verification pass;
  automated assets checks reject system-font stacks, CJK UI strings, and
  external HTTP(S) assets. Chromium captures pass through 200 percent DPI.
- Authorized HIL evidence (2026-08-09): combined Manus + Linker + Hitbot
  observation passed for 60 seconds at 5 Hz (300 samples), with zero failure
  samples and runtime state exclusively `RL_SHADOW`. Exact final correlation
  and source-age statistics are recorded in
  `.artifacts/control-console/hil-manus-linker-hitbot-20260809.json`.
- Regression evidence after HIL fixes: complete dex-manipulation suite
  `79 passed in 7.59s`; import-linter `3 kept, 0 broken`; dex_teleop arm
  telemetry suite `4 passed in 0.04s`.
- Remaining physical/release gates: real-loop enabled/disabled/slow-viewer
  timing and longer hardware soak, operator visual review, current-stable
  Firefox review, real policy artifact/preflight, foot-switch commissioning,
  and task-specific control release.
- Exceptions or deviations: the five-minute soak is strong software regression
  evidence but does not replace the release-grade real-hardware timing/soak.
- Final disposition: `REAL ARM HOLD SOFTWARE IMPLEMENTED OFFLINE; LIVE MONITORING ACCEPTED; PHYSICAL RL SWITCH HIL PENDING`
- [x] Fail closed in real Hitbot telemetry mode: disable the UI switch control
  and reject backend switch requests until a measured, acknowledged real-arm
  hold gateway replaces the current fake-arm implementation.

Acceptance note: the default live launcher must expose monitoring only. The page
must show `RL SWITCH DISABLED`, `/api/switch` must reject the request, and the
runtime must remain in `RL_SHADOW` unless the separate physical-release option
and confirmation token are both present.

## Step 8 - Add the verified real Hitbot hold gateway

### Checklist

- [x] Keep the Hitbot SDK and robot socket exclusively inside the existing
  `dex_teleop` arm process.
- [x] Add a loopback-only request/ack protocol with schema version, command ID,
  control-session ID, arm epoch, monotonic deadline, and bounded hold lease.
- [x] Freeze Vive-driven commands before TCP/IK preparation begins; preparation
  failure enters `FAULT_HOLD` instead of returning to tracker motion.
- [x] Capture the actual TCP, solve one anchor IK target, repeat that fixed
  ServoJ target, and measure actual TCP error on each hold cycle.
- [x] Require five consecutive samples within 1.5 mm and 1.5 degrees before
  reporting `VERIFIED`.
- [x] Refresh the one-second hold lease from runtime heartbeats; expiry remains
  in fixed `FAULT_HOLD` and cannot resume Vive automatically.
- [x] Require a fresh tracker sample for re-anchor and refresh that anchor at
  the exact release boundary.
- [x] Replace the runtime's hard-coded fake arm gateway with the deployment-
  selected `hitbot-hold-v1` gateway for the live console.
- [x] Verify the hold continuously through hand blend, RL active, hand-back,
  and re-anchor; loss transfers the hand to safety ownership.
- [x] Display `HOLDING`, `HOLD VERIFIED`, `HOLD FAULT`, and measured hold error
  in the English Hitbot panel.
- [x] Keep live switching disabled by default behind `--enable-rl-switch` and a
  second exact `ENABLE RL` confirmation.
- [x] In RL-enabled shutdown, complete hand-back/re-anchor while the Hitbot hold
  controller is alive; bound this wait to 10 seconds before stopping processes.
- [x] Add offline tests for protocol identity/deadline validation, idempotency,
  verified hold, heartbeat loss, prepare failure, stale re-anchor, reconnect,
  runtime gating, and telemetry rendering.
- [ ] Run a staffed, bounded hold-only HIL test without activating hand RL.
- [ ] Verify hold error and zero tracker-driven arm displacement while the Vive
  tracker is deliberately moved during hold.
- [ ] Run one bounded `RL_SHADOW -> RL_ACTIVE -> hand-back -> RL_SHADOW` cycle
  with automatic timeout and confirm the first post-release arm delta is safe.
- [ ] Exercise controller loss, runtime loss, stale tracker, and E-stop recovery
  procedures and preserve logs.

### Acceptance notes

- Offline acceptance is complete; no real Hitbot hold or RL transition was
  commanded during this implementation pass.
- The default one-command launcher remains monitoring-only. The physical path
  requires `--enable-rl-switch`, the normal `CONFIRM` token, and the separate
  `ENABLE RL` token.
- A valid response is not sufficient by itself: the UI switch is enabled only
  when the hold controller is reachable in `TELEOP`, and the handoff cannot
  leave `ARM_HOLD_VERIFY` until measured stability is acknowledged.
- Heartbeat expiry is intentionally non-recovering. Stop the supervised stack,
  inspect the fault, and restart from teleoperation; do not add automatic
  release behavior.
- Physical release remains blocked until all four pending checklist items above
  have signed evidence in the run artifacts.

## Step 9 - Add isolated D435 RGB/depth monitoring and approved layout

### Goal

Add a large, live D435 vision panel without coupling camera availability or
frame delivery to either robot control loop, and make real-arm switch gating
unambiguous to the operator.

### Checklist

- [x] Record operator approval of the D435-centered desktop layout.
- [x] Keep camera capture in a dedicated read-only child process with no runtime,
  LinkerHand, Hitbot, IK, ServoJ, or switch-command reference.
- [x] Keep only the latest RGB JPEG and latest colorized-depth JPEG in memory.
- [x] Add a live D435 source using lazy `pyrealsense2` and OpenCV imports.
- [x] Select aligned 640 x 480 RGB and depth at 30 FPS, matching the verified
  host capture utility.
- [x] Reject an attached RealSense device whose reported model is not in the
  D435 family.
- [x] Add a clearly labelled synthetic camera for offline UI review.
- [x] Serve RGB and depth through same-origin MJPEG endpoints.
- [x] Publish independent D435 sequence, rate, dimensions, serial, health,
  stale reason, and fault metadata through the telemetry hub.
- [x] Render Manus and compact LinkerHand on the left, full D435 RGB with depth
  picture-in-picture in the center, and Hitbot tracking on the right.
- [x] Keep all visible UI text in English and continue using only the bundled
  verified WOFF2 fonts.
- [x] Distinguish `RL SWITCH NOT AUTHORIZED`, `ARM HOLD NOT READY`, and
  `ARM HOLD READY` in both backend state and button presentation.
- [x] Add D435 dependency checks and `--camera d435` to the one-command live
  launcher.
- [x] Add a confirmation-gated installer for the scoped official D435 udev
  permission rule and fail launcher preflight when no matching rule exists.
- [x] Add source, telemetry, MJPEG, layout, launcher, and switch-gate tests.
- [x] Capture a 1680 x 945 full-viewport synthetic-camera screenshot.
- [x] Verify the selected physical device model and serial with the operator's
  D435 connected.
- [ ] Record live RGB/depth rate, freshness, USB disconnect behavior, and
  restart recovery without moving either robot.

### Acceptance criteria

- A D435 or camera-SDK fault changes only the D435 telemetry health and panel.
- The browser can display RGB and depth without external assets or a second
  process that can issue robot commands.
- The approved desktop layout fits a 1680 x 945 viewport without scrolling.
- Live RL authorization and live arm-hold readiness cannot share the same
  ambiguous disabled label.
- The one-command launcher fails before hardware startup if its camera Python
  capability is absent, and fails its health wait if D435 frames do not arrive.
- Physical-camera acceptance remains pending until observed D435 evidence is
  recorded; a synthetic screenshot is not physical HIL evidence.

### Acceptance notes

- Status: `HOST CAPTURE ACCEPTED; LIVE UI FRAME OBSERVATION PENDING`
- Review date: 2026-08-09
- Source isolation: `tools/control_console/camera_source.py` owns a bounded IPC
  relay and condition-protected latest-frame buffer; an external child process
  exclusively owns librealsense and the camera pipeline. The worker directly
  executes `realsense_worker.py` with the known-good host camera Python, so it
  does not import the UI package or any robot runtime. The source has no robot
  controller, gateway, or writable action dependency. A blocked SDK
  initialization is terminated during bounded shutdown.
- Packaging: `pyrealsense2==2.58.3.10794` and
  `opencv-python-headless==4.11.0.86` are pinned in the camera optional
  dependency and full bootstrap profile. The worker may intentionally use a
  separately verified host Python; the UI process does not import that SDK.
- Test evidence: `.venv/bin/python -m pytest -q` -> `98 passed in 11.06s`;
  JavaScript syntax, Bash syntax, Python compilation, dry-run argument output,
  and `git diff --check` passed.
- Screenshot evidence:
  `.artifacts/control-console/screenshots/d435-live-console-implemented.png`.
  It uses synthetic Manus, LinkerHand, D435, and Hitbot sources and therefore
  proves layout only, not device connectivity.
- Physical identity evidence (2026-08-09): Linux sysfs reports
  `Intel(R) RealSense(TM) Depth Camera 435`, USB `8086:0b07`, serial
  `144223022813`, and 5000 Mb/s USB speed. Five UVC interfaces are bound to
  `uvcvideo`, with kernel video records `video0` through `video5`.
- Frame HIL limitation: the managed Codex execution sandbox does not expose
  the host `/dev/video*` nodes, so it cannot complete RGB/depth frame capture.
  This is not counted as frame-health acceptance and no robot process was
  started during the probe.
- First host UI evidence: the D435 panel reported
  `RuntimeError: No device connected`. The scoped udev rule was subsequently
  installed and the camera reconnected. The operator then verified live color
  and depth with `/home/user/dex-forge/tools/realsense_capture.py --live` from
  `/home/user/miniconda3/bin/python`, proving the device and that Python stack.
  The original UI still used its Python 3.11 environment and a different stream
  configuration, so the udev rule was not the complete root cause.
- Environment repair (2026-08-09): the live launcher now starts the isolated
  D435 worker with `/home/user/miniconda3/bin/python` by default (overridable by
  `DEX_CAMERA_PYTHON`) and mirrors the verified utility's aligned 640 x 480 at
  30 FPS, RGB-control query, and 30 + 45 frame warmup path. The parent UI keeps
  its own `.venv` and receives only JPEG frames and metadata.
- Isolation regression evidence: with D435 device access unavailable inside
  the managed Codex sandbox, a fake-robot run remained healthy in `RL_SHADOW`;
  Linker and Hitbot stayed healthy while only D435 faulted. Dedicated tests
  cover child-process SDK faults and bounded termination of a stuck capture
  process.
- Remaining evidence: rerun the host UI, observe model `D435`, serial
  `144223022813`, and sustained RGB/depth health in the browser, then perform a
  non-motion disconnect/restart check before marking this step physically
  accepted.

## 9. Recommended delivery gates

### Gate A - UI and fake telemetry review

Status: `ACCEPTED`

Includes Steps 0-2 and the fake portion of Step 6.

Approval permits implementation of real read-only source adapters. It does not
permit physical actuation.

### Gate B - Real Manus and Linker read-only monitoring

Status: `ACCEPTED - AUTHORIZED HIL PASSED 2026-08-09`

Includes Steps 3-4 and source correlation evidence.

Approval permits the console to observe an already-authorized live hand run. It
does not permit bypassing preflight, readiness, ownership, safety, or E-stop
requirements.

### Gate C - Hitbot read-only monitoring

Status: `ACCEPTED - AUTHORIZED HIL PASSED 2026-08-09`

Includes Step 5 after dex_teleop preservation review.

Approval permits observation of an existing Hitbot tracking session. It does
not add arm commands to dex-manipulation and does not authorize a real arm
adapter.

### Gate D - Operator-console release

Status: `BLOCKED ON REAL-POLICY/PEDAL RELEASE, HARDWARE SOAK, FIREFOX, AND OPERATOR SIGN-OFF`

Includes Step 7 and the updated runbook.

Release remains subordinate to all existing physical and safety gates in
`docs/implementation-progress.md` and `docs/operator-runbook.md`.

### Gate E - Real Hitbot hold and RL handoff

Status: `SOFTWARE COMPLETE; STAFFED PHYSICAL HIL PENDING`

Includes Step 8. Approval requires the hold-only test, deliberate tracker-motion
test, bounded full handoff, loss injection, E-stop recovery, and preserved logs.
Passing fake or loopback tests does not release physical switching.

### Gate F - D435 live vision

Status: `SOFTWARE COMPLETE; PHYSICAL D435 OBSERVATION PENDING`

Includes Step 9. Approval requires the intended D435 model/serial readback,
sustained healthy RGB/depth frames, and a non-motion disconnect/restart check.
Camera health never grants robot readiness and cannot authorize a switch.

## 10. Explicitly out of scope

- Browser-issued hand joint, arm joint, TCP, IK, or ServoJ targets.
- Any arm motion mode other than holding the measured transition anchor.
- Automatic arm release after heartbeat, runtime, or telemetry loss.
- Replacing the independent physical E-stop.
- Full robot digital-twin rendering without verified joint feedback and model.
- Cloud telemetry, remote access, authentication, or internet exposure.
- Mobile operator control.
- Chinese UI, localization infrastructure, or runtime language switching.
- Connecting trace replay directly to physical hardware.

## 11. Final definition of done

The project is complete only when all of the following are true:

- [x] The UI is fully English and uses only bundled, verified WOFF2 fonts.
- [x] No runtime CDN or operating-system font dependency exists.
- [x] Manus visualization is tied to the exact runtime control sample.
- [x] LinkerHand displays all required target and measured layers for 16 joints.
- [x] Hitbot displays tracker, target TCP, actual TCP, IK, servo, and health
  telemetry without additional hardware queries.
- [x] D435 RGB and colorized depth have an isolated, bounded, read-only source,
  same-origin streams, and explicit stale/fault presentation.
- [x] All telemetry producers are non-blocking and bounded.
- [x] SSE reconnect, stale states, malformed data, and source loss are tested.
- [x] UI failure or disconnection has no control-loop effect.
- [x] No unauthorized hardware connection or motion endpoint exists.
- [x] Real arm hold commands remain inside the existing single Hitbot SDK owner.
- [x] The default live launcher cannot activate the physical RL switch path.
- [ ] Staffed real-arm hold, deliberate tracker-motion, full handoff, and fault
  recovery acceptance notes are signed and preserved.
- [x] Fake/replay mode demonstrates all panels without hardware.
- [x] Automated, offline-font, and authorized read-only HIL evidence is
  recorded in the acceptance notes.
- [ ] Release-grade real-hardware timing variants and extended soak are
  recorded in the acceptance notes.
- [ ] Physical D435 identity, sustained frame health, disconnect, and restart
  evidence is recorded in the acceptance notes.
- [x] The operator runbook is updated and remaining physical gates are explicit.
