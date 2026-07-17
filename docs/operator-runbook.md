# Hand-only operator runbook and teleop-only rollback

This runbook covers only the adopted M0-M3 hand-only path. The current checkout
has not passed the physical release gates listed in `implementation-progress.md`.
Do not use it for a live hand until the deployment binding, E-stop, Manus,
PCsensor, CAN, and task-specific HIL checks are confirmed.

## Preconditions

Before a live run, all of the following must be true:

1. The independent robot E-stop is reachable and has been exercised.
2. The hand identity is the confirmed LinkerHand G20 left hand and the serial
   matches the frozen calibration.
3. The pinned Linker SDK G20 driver digest passes bootstrap verification.
4. The configured Manus source produces the expected left-hand layout and
   remains fresh.
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
