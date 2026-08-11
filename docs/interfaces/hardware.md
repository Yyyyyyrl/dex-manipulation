# Hardware interface

How hand and arm hardware is abstracted, and what adding new hardware involves.

## Hand: the exclusive gateway

`LinkerGateway` ([`gateway.py`](../../src/dex_hardware_linker/gateway.py)) is the
only component in the system that may move the hand. Every command — from the
runtime, from the console, from the demo tools — funnels through it.

Exclusivity is structural rather than conventional: the transport is touched
only from the gateway's own thread, and callers hand work over through a queue.
That is why the LinkerHand ROS SDK is never run alongside the runtime.

Three mechanisms carry the guarantees:

**Two-phase ownership.** `prepare_ownership()` validates and reserves without
actuating; `commit_ownership()` makes it live. A transition that fails its gates
in between simply drops the preparation, so a rejected handoff cannot leave the
gateway believing a new owner is in charge.

**Epoch enforcement.** Every command carries a `control_epoch`. Anything not
matching the current owner's epoch is rejected, which makes an in-flight command
from a superseded owner harmless rather than a race.

**Explicit acknowledgement strength.** `submit()` returns a ticket; `wait()`
yields a `HandCommandAcknowledgement` carrying the `EffectiveHandTarget` and its
`AcknowledgementLevel`. `sent-to-bus` means the frame left the host — it is
*not* confirmation the hand moved. Policies declare the level they require, and
hardware that cannot supply it is refused.

## Hand: transport

`LinkerTransport` ([`transport.py`](../../src/dex_hardware_linker/transport.py))
is a four-method `Protocol`:

```python
class LinkerTransport(Protocol):
    acknowledgement_level: AcknowledgementLevel
    def open(self) -> None: ...
    def read_state(self) -> NativeHandState | None: ...
    def send(self, native_range: Sequence[int]) -> None: ...
    def close(self) -> None: ...
```

It speaks **native slot values**, not radians — 20 slots for the G20. All
semantic meaning lives in the calibration above it.

| Implementation | Use |
|---|---|
| `LinkerSdkTransport` | Real CAN through the pinned, patched LinkerHand SDK |
| `FakeLinkerTransport` | Perfect-tracking fake; the entire test suite and all fake-mode tooling run on it |

The fake is not a stub — it is what makes the whole runtime, including the
handoff state machine and the console, exercisable with no hardware.

## Hand: the calibration model

Three layers turn a named joint into a wire value:

1. **`SemanticJointSchema`** — the vocabulary. Ordered `SemanticJointSpec`
   entries (name, lower, upper, continuous) plus `MimicRelationship` entries for
   dependent joints. Carries a digest of its canonical JSON. This ordering is
   the canonical joint order for the entire system: teleop profiles, policy
   packages, and safety limits all index against it.

2. **`JointCalibration`** — per joint: `slot` (CAN position), `lower`/`upper`
   native bounds, `flip` (polarity), `offset` (zero-point bias). This is the
   per-unit physical truth, and it differs between two hands of the same model.

3. **`LinkerMapper`** — applies the above to convert semantic radians to native
   slot values and back, and is what the gateway actually calls.

Calibrations and schemas are identified by `artifact_digest`, and compatibility
is checked by comparing digests. A silently edited calibration moves the hand to
different angles, so this must be a load failure, not a surprise. Frozen assets
live in `src/dex_hardware_linker/assets/`.

## Arm: hold-only, out of process

The arm is deliberately not owned by this runtime. `tools/vr_hitbot_controller.py`
is the sole Hitbot SDK owner, in its own process;
[`real_arm.py`](../../src/dex_runtime/real_arm.py) is a loopback-only UDP
*client* that asks it to hold still and reports whether it did.

The runtime never commands arm motion. It only requests a hold, because the hand
may change owner only while the arm is verifiably still.

`ArmGateway` (defined in `handoff.py`, implemented by `RealArmGateway` and
`FakeArmGateway`):

| Call | Meaning |
|---|---|
| `prepare_hold()` | Ask the arm to stop moving |
| `enter_hold()` | Commit to holding |
| `verify_hold()` | **False until the arm has actually settled.** Polled, not assumed. |
| `reanchor_teleop()` | Re-anchor the operator's frame to where the arm actually is |
| `release_to_teleop()` | Return control |
| `status` | Current state and fault reason |

Controller states are `TELEOP → PREPARED → HOLDING → VERIFIED`, unwound via
`REANCHOR_ACKED`, with `FAULT_HOLD` absorbing failures.

Two design points:

- **`verify_hold()` is a poll, not a command.** The supervisor dwells in
  `ARM_HOLD_VERIFY` until the arm reports it has settled within tolerance. There
  is no path that assumes the hold took effect.
- **The hold is a lease.** A missing heartbeat drops the controller into
  `FAULT_HOLD`, which does not resume on its own. If the supervising process
  dies while the policy holds the hand, the arm stops rather than continuing to
  hold a target nobody is watching.

Re-anchoring exists because the operator's tracked pose drifts from the arm's
actual pose while the arm is held. Returning control without re-anchoring would
command a jump to wherever the operator's hand had wandered.

## Adding a new hand

1. **Implement `LinkerTransport`** for your bus. Keep it dumb: open, read native
   state, send native values, close. Set `acknowledgement_level` honestly — if
   you cannot confirm more than "the frame was sent", say `sent-to-bus`.
2. **Author a semantic schema** naming your joints in a fixed order, with limits
   and any mimic relationships. Compute its digest.
3. **Author a per-unit calibration** mapping each semantic joint to a slot, with
   bounds, flip, and offset. Calibrate per physical hand, not per model.
4. **Add golden fixtures.** `tests/fixtures/golden/linker_mapping_golden_v1.json`
   is the pattern: pin the semantic→native mapping so a future refactor cannot
   silently change where the hand goes.
5. **Add a teleop profile** for the device/hand pairing
   ([teleop.md](teleop.md)).

You should not need to touch `dex_runtime`. If you do, the abstraction boundary
has been crossed and the import contract will say so.

## Adding a different arm

Implement the `ArmGateway` calls against your arm, keeping the SDK in its own
process and this side a client. Preserve two properties, because the handoff
state machine depends on them:

- `verify_hold()` must return False until the arm has genuinely settled. Do not
  return True optimistically.
- The hold must fail safe on lost contact, rather than latching.

`FakeArmGateway` is the reference for the state sequence, and it is what the
handoff tests run against.
