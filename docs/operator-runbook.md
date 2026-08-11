# Hand-only operator runbook and teleop-only rollback

This runbook covers only the adopted M0-M3 hand-only path. The current checkout
has not passed the physical release gates listed in `implementation-progress.md`.
Do not use it for a live hand until the deployment binding, E-stop, OpenXR,
PCsensor, CAN, and task-specific HIL checks are confirmed.

## Preconditions

Before a live run, all of the following must be true:

1. The independent robot E-stop is reachable and has been exercised.
2. The hand identity is the confirmed LinkerHand G20 left hand and the serial
   matches the frozen calibration.
3. The pinned Linker SDK G20 driver digest passes bootstrap verification.
4. Quest 3S is connected through WiVRn, the OpenXR session is focused, and all
   26 left-hand joints required by DexPilot remain valid and fresh.
5. The PCsensor device is visible at the configured evdev by-id path, reports
   USB `3553:b001`, advertises KEY_F12, and can be grabbed exclusively.
6. A real exported policy package is in an immutable local store.
7. DeploymentBinding values have been reviewed; do not substitute guessed CAN,
   speed, torque, safety, watchdog, rate, package, or logging values.
8. The mounted-screwdriver task is commissioned before free-object rotation.

The runtime is not an E-stop. F12 is a transition request, not a safety device.

## Installation and non-actuating checks

From `/home/user/dex-manipulation`:

```bash
./bootstrap.sh
source .venv/bin/activate
pytest
```

Inspect a package. Unsigned local trust must be explicit:

```bash
dex-runtime verify-package /absolute/path/to/package --allow-unsigned-local
```

Run strict preflight. This opens no hardware transport:

```bash
dex-runtime preflight /absolute/path/to/deployment.json
```

List the configured package store after the same strict preflight:

```bash
dex-runtime list-policies /absolute/path/to/deployment.json
```

Stop if preflight reports any identity, digest, schema, calibration, codec,
rate, acknowledgement, action-limit, or calibrated-envelope mismatch.

## Live control console

The console is an English-only, loopback-only operator surface. It uses bundled
and digest-verified WOFF2 fonts; it does not load fonts, JavaScript, or other
assets from the network. Its telemetry paths are read-only. The only browser
actions are the existing F12 transition request and safe-stop request.

Install the scoped D435 USB permission rule once on a new host, then unplug and
reconnect the camera as instructed:

```bash
./tools/install_d435_udev.sh
```

The installer requires the exact `INSTALL D435 RULE` token and sudo. It installs
only the Intel `8086:0b07` D435 rule bundled from librealsense v2.58.3; it does
not install a kernel patch or start a camera or robot process.

Run the import and argument check without opening CAN, UDP listeners, or an HTTP
server:

```bash
.venv/bin/python tools/switch_web_demo.py \
  --dry-run --transport fake --policy synthetic \
  --vr off --arm-telemetry live --camera off
```

Rehearse the complete console without hardware:

```bash
.venv/bin/python tools/switch_web_demo.py \
  --transport fake --policy synthetic \
  --vr fake --vr-python .venv/bin/python \
  --arm-telemetry fake --camera fake
```

Open `http://127.0.0.1:8765/`. The simulated sources are explicitly labelled
`SIMULATED`; they are never presented as real healthy hardware.

For the authorized real OpenXR + LinkerHand + Hitbot monitoring stack, the
supervised one-command launcher is preferred over starting three terminals by
hand:

```bash
cd /home/user/dex-manipulation
./tools/start_live_ui.sh
```

The launcher checks the pinned environments, D435 Python capability, active
Hitbot Ethernet profile, 1 Mbit/s `can0`, robot reachability, duplicate UI/arm
owners, `/home/user/dex_teleop/main_new.py`/`VRHandReader`, OpenXR Python
support, and healthy D435 RGB/depth frames.
By default it starts `flatpak run io.github.wivrn.wivrn//stable` before the OpenXR
bridge. If that Flatpak is already running, the launcher reuses it and does not
stop the externally owned instance. Use `--no-wivrn` only for an OpenXR runtime
managed outside this launcher.
The UI and camera intentionally use separate Python environments. The D435
worker defaults to `/home/user/miniconda3/bin/python`, matching the verified
`dex-forge/tools/realsense_capture.py --live` path. Override it only when a
different executable has itself been verified:

```bash
DEX_CAMERA_PYTHON=/absolute/path/to/python ./tools/start_live_ui.sh
```

It requires the exact `CONFIRM` token before starting hardware, uses only the
synthetic policy in `RL_SHADOW`, and never sends `/api/switch` or an F12 event.
The default live mode is monitoring-only: the switch control and backend switch
endpoint remain disabled by the physical-release gate. The implemented Hitbot
hold gateway does not become operator-accessible merely because telemetry is
healthy. Do not interpret LinkerHand readiness as arm-hold proof.
It writes per-process logs below
`.artifacts/control-console/live-runs/<UTC-time>-<pid>/`, opens the loopback UI,
and remains in the foreground as the process supervisor. Press Ctrl-C in that
terminal for ordered shutdown. Monitoring mode stops Hitbot before the UI;
real-arm switch mode first completes UI hand-back/re-anchor while the hold
controller is alive, then stops Hitbot and OpenXR. A 10-second hand-back timeout
continues bounded shutdown and must be recorded as a failed HIL run.

Useful variants:

```bash
./tools/start_live_ui.sh --dry-run
./tools/start_live_ui.sh --no-browser
./tools/start_live_ui.sh --no-hitbot
```

Only during an explicitly authorized real-arm hold HIL gate, use:

```bash
./tools/start_live_ui.sh --enable-rl-switch
```

This variant requires both `CONFIRM` and the separate exact token `ENABLE RL`.
The button remains unavailable until the loopback hold controller answers with
the matching session and arm epoch. This option is not released for unattended
or routine operation until the pending acceptance notes below pass.

The switch gate text is intentionally explicit:

- `RL SWITCH NOT AUTHORIZED` means the launcher was not given the physical
  release option and token.
- `ARM HOLD NOT READY` means switching was authorized but the verified Hitbot
  hold controller is not reachable or healthy.
- `ARM HOLD READY` means the authorization and live hold probe both passed;
  normal runtime readiness and physical supervision still apply.

Run the reproducible five-minute bounded-state, refresh, and slow-client check:

```bash
.venv/bin/python tools/control_console/soak_verify.py \
  --duration-s 300 --viewer-count 2 \
  --output .artifacts/control-console/fake-soak-300s.json
```

This verifier is hard-coded to fake transport, synthetic policy, fake OpenXR,
fake D435, and fake arm telemetry. It cannot be configured for hardware and never calls
the switch or stop endpoints. It waits for the three 200-point latency rings,
measures a no-viewer phase, then adds two repeated-refresh viewers and one SSE
viewer that intentionally does not consume its response body. A nonzero exit
means the health, identity, viewer, memory-budget, or bounded-history check
failed.

Interpret the five areas as follows:

- `OPENXR HAND TRACKING` shows the exact 26-joint Quest/WiVRn sample consumed
  by the production DexPilot retargeter. The OpenXR and Linker control sample
  sequences must correlate.
- `LINKERHAND G20` shows all 16 semantic joints. Cyan is measured, purple is
  requested, green is safety-authorized, and amber is the effective target.
  Owner, epoch, acknowledgement, and command identity must agree.
- `D435 LIVE VIEW` shows the complete RGB frame with colorized depth in the
  picture-in-picture window. Its source runs independently and read-only;
  `STALE` or `FAULT` does not authorize, block, or modify a robot command.
- `HITBOT ARM` shows tracker-derived target TCP, the actual TCP already read by
  that control cycle, IK/ServoJ result, cycle timing, and bounded XY/XZ trails.
  The console never opens the Hitbot socket and never sends an arm target.
- `READINESS` is the exact provider evidence from one completed control tick:
  operator confirmation, hand state, gateway health, and policy compatibility.

Operator confirmation is deliberately time-bounded. If `OPERATOR` shows
expired, recheck hardware identity, task state, workspace clearance, and E-stop
access, then click `CONFIRM OPERATOR`. This refreshes only the operator evidence;
it does not press F12, transfer ownership, or automatically renew again. The
button reads `OPERATOR CONFIRMED` and disables itself while that evidence is
valid.

`DEGRADED`, `STALE`, and `FAULT` are explicit text states, not color-only
indicators. A degraded source may retain its last geometry for diagnosis; stale
or faulted OpenXR geometry is hidden. Hover a source or readiness item for its
reason. Never infer safe motion from a plausible-looking plot.

### Authorized live observation

The following commands are not a read-only HIL test: the console command opens
the real Linker CAN transport, and the dex_teleop command runs the existing
Hitbot tracker controller. They may actuate hardware. Run them only after every
physical precondition above passes, the E-stop is staffed, and the run is
explicitly authorized.

Start the Linker/OpenXR runtime and its loopback console from this repository:

```bash
.venv/bin/python tools/switch_web_demo.py \
  --transport hand --policy real --deploy /absolute/path/to/deploy.pth \
  --vr real --vr-python /home/user/miniconda3/envs/dexmachina/bin/python \
  --teleop-root /home/user/dex_teleop \
  --arm-telemetry live --arm-udp-port 8780 \
  --arm-hold-port 8781
```

If the authorized Hitbot tracking session is part of the test, start the single
OpenXR/Hitbot owner in a separate terminal. It consumes the bridge fanout on
UDP 8771 and publishes telemetry on UDP 8780:

```bash
cd /home/user/dex-manipulation
.venv-hitbot/bin/python tools/vr_hitbot_controller.py \
  --vr-port 8771 --telemetry-port 8780 --hold-port 8781 \
  --teleop-root /home/user/dex_teleop
```

The commands above still start in monitoring-only mode. The direct UI command
may include `--enable-real-arm-hold-switch` only inside the same explicitly
authorized HIL gate. During hold, `vr_hitbot_controller.py` remains the only
Hitbot SDK owner,
repeats one fixed ServoJ target, verifies actual TCP stability, and discards
OpenXR wrist deltas. A missing heartbeat enters `FAULT_HOLD` and never resumes
tracking automatically. Release requires a fresh OpenXR re-anchor acknowledgement.

From a third terminal, capture one minute of loopback-only GET evidence. For
Gate B, require the real OpenXR and Linker sources:

```bash
cd /home/user/dex-manipulation
.venv/bin/python tools/control_console/hil_observe.py \
  --url http://127.0.0.1:8765 --duration-s 60 \
  --require openxr linker \
  --output /absolute/path/to/hil-openxr-linker.json
```

For the combined Gate B/C observation after the authorized Hitbot owner is
running, require all three sources:

```bash
.venv/bin/python tools/control_console/hil_observe.py \
  --url http://127.0.0.1:8765 --duration-s 60 \
  --require openxr linker hitbot \
  --output /absolute/path/to/hil-openxr-linker-hitbot.json
```

The observer cannot start a runtime or hardware connection. It accepts only a
loopback HTTP base URL and sends only `GET /api/snapshot`. It rejects simulated
OpenXR/Hitbot modes and fails if readiness, source health, focused 26-joint layout,
OpenXR-to-Linker sequence identity, 16-joint state, epoch, acknowledgement,
command identity, tracker pose, TCP, or IK evidence is missing at any sampled
instant. Preserve a passing JSON report with the console/controller logs; never
edit a failed report into a pass.

Do not start a second Linker SDK/CAN process or another Hitbot interface. Keep
the console and publisher on loopback and keep UDP port 8780 identical on both
sides. Killing the browser or the Hitbot telemetry publisher must not be used as
a motion-stop mechanism; use normal hand-back/shutdown or the independent
E-stop as appropriate.

For normal console shutdown, return the hand to teleoperation, use `SAFE STOP`,
wait for the stopped state, and then Ctrl-C the console process. Stop the
dex_teleop owner through its existing Ctrl-C procedure. Preserve the console
runtime logs shown in the footer together with the dex_teleop test record.

## Live hand-only process

Only after the physical prerequisites pass:

```bash
dex-runtime run /absolute/path/to/deployment.json
```

Startup behavior:

1. The validated composition opens one exclusive Linker gateway thread.
2. Manus and PCsensor sources start on worker threads.
3. The process waits for a fresh hand state and a valid Manus sample.
4. Teleoperation owns the hand; the fake arm owns no physical transport.
5. Terminal status and JSONL recording begin.
6. The terminal requests an operator ID and the exact token `CONFIRM`.

Do not enter `CONFIRM` until the displayed identity, policy, source/gateway
health, readiness checklist, physical task state, and E-stop readiness have
been checked. Confirmation is recorded with operator/session/package identity
and bounded validity.

After confirmation and machine-verifiable readiness pass, the selected policy
enters continuous shadow while teleoperation still owns the hand. The shadow
history must reach 30 fresh ticks. F12 never bypasses this gate.

F12 tap behavior:

- in `RL_SHADOW`: request arm-hold verification and hand blend to RL;
- before history/readiness is valid: record a visible rejection and remain in
  teleoperation;
- in `RL_ACTIVE`: request hand-back;
- in any other state: record a visible rejection;
- key repeat events are ignored; a press edge is one request.

During RL execution, Manus continues updating so hand-back has a current live
endpoint. After hand-back completes, teleoperation owns the hand and the
selected policy is automatically reset and re-primed in `RL_SHADOW`.

## Normal hand-back and shutdown

1. Tap F12 once while status reports `RL_ACTIVE`.
2. Observe `HAND_BACK_PREPARE`, `HAND_BACK_BLEND`, and
   `ARM_TELEOP_REANCHOR`.
3. Verify the displayed hand owner returns to `teleoperation` and the arm owner
   returns through the fake hold/re-anchor path.
4. The selected policy may immediately report `RL_SHADOW`; this is expected
   continuous preview only. It does not own or command the hand.
5. Use Ctrl-C only after teleoperation ownership is displayed. SIGINT requests
   normal bounded shutdown and closes source/gateway threads.
6. Preserve the event and trace JSONL files for review.

If a real safety concern exists, use the independent robot E-stop rather than
waiting for Python shutdown.

## Tested teleop-only rollback

The rollback target is the new exclusive-gateway teleoperation path. Do not
start a legacy direct-actuator dex_teleop script alongside it; that would
violate the one-gateway ownership rule.

If RL is active:

1. Tap F12 once and wait for hand-back to complete.
2. Verify the actual hand owner is `teleoperation`, not only that the target
   looks plausible.
3. Stop the process after the hand is back under teleoperation.
4. Restart with the same preflighted binding.
5. Do not enter `CONFIRM` (or enter any token other than the exact uppercase
   token). The process remains `TELEOP_ACTIVE`; policy ownership cannot be
   requested.
6. An F12 press in this unarmed state is logged as a rejection and does not
   actuate a policy transition.

This rollback behavior is covered by the non-hardware application regression:
without operator confirmation, the real scheduler, safety supervisor, and
exclusive fake-transport gateway send only authorized teleoperation commands
and finish in `TELEOP_ACTIVE`. The supervisor regression separately covers the
bumpless policy-to-teleoperation blend.

To re-enable policy operation later, stop and restart, re-run preflight, review
the live state, and explicitly confirm again.

## Fault response

- Stale/unhealthy Manus invalidates operator confirmation.
- Stale hand state, missing effective-target evidence, package mismatch,
  gateway/watchdog fault, safety-limit failure, or expired readiness blocks or
  terminates the transition.
- Readiness loss during hold verification, blend, or active execution transfers
  to safe hold.
- Switch queue overflow and scheduler/runtime exceptions are recorded as
  runtime faults; the gateway watchdog remains the containment layer.
- Opening the hand is not a universal fault response.

After a fault, do not clear it by repeatedly pressing F12. Record the displayed
reason, use the independent E-stop if required, stop the process, preserve the
logs, correct the physical or configuration cause, and start again from
preflight.

## Required log review

The event JSONL must show, as applicable:

- operator confirmation and F12 action;
- requested and resulting state;
- hand/arm owner and control epoch;
- package ID and readiness snapshot;
- rejection or fault reason;
- command deadline and gateway acknowledgement;
- safe response.

The bounded control trace must reconstruct each recorded decision from source
metadata, candidates, measured state, codec/latent/action/target, authorized
command, safety decision, arbitration, mapping preview, effective target,
acknowledgement evidence, fake-arm hold state, and scheduler timing.

Replay tooling is intentionally deferred after M3. Trace files must never be
connected to live hardware by an ad hoc replay script.
