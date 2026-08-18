# dex-manipulation

*[English](README.md) | [中文](README.zh.md)*

Neutral hardware runtime for mixed teleoperation / reinforcement-learning
control of a dexterous hand.

This repository owns the internal contracts, teleoperation adapters, policy
execution, exclusive hardware gateways, handoff supervision, and observability.
It does not train policies, does not depend on Isaac Lab, and does not import
`dex-forge` training code.

```
operator device ──▶ retargeting ──▶ ┌───────────────┐ ──▶ safety ──▶ gateway ──▶ hand
                                    │   handoff     │
trained policy  ──▶ inference  ──▶ │  supervisor   │ ◀── readiness evidence
                                    └───────────────┘ ◀──▶ arm hold lease
```

The supervisor decides who is allowed to move the hand. Every forward step
toward policy control is gated on readiness evidence, a full policy observation
history, and a verified arm hold; every failure falls back to holding still.

## Quickstart

```bash
./bootstrap.sh core          # use Python 3.10-3.12
source .venv/bin/activate
# run the whole stack with no hardware at all
python -m tools.control_console.soak_verify --duration-s 30 --viewer-count 1

# or look at it: http://127.0.0.1:8765/
python tools/switch_web_demo.py --transport fake --policy synthetic \
    --vr fake --vr-python .venv/bin/python --arm-telemetry fake --camera fake
```

`--transport` only selects the *hand*. The policy, operator input, camera, and
arm telemetry each default to real and must be faked separately, as above.

Full walkthrough in [docs/onboarding.md](docs/onboarding.md).

## Documentation

| | Read it for |
|---|---|
| [Onboarding](docs/onboarding.md) · [中文](docs/zh/onboarding.md) | Getting it running, repository map, troubleshooting |
| [Architecture](docs/architecture.md) · [中文](docs/zh/architecture.md) | The control path, the state machine, and why it is built this way |
| [Teleop interface](docs/interfaces/teleop.md) · [中文](docs/zh/interfaces/teleop.md) | Adding an operator device |
| [Policy interface](docs/interfaces/policy.md) · [中文](docs/zh/interfaces/policy.md) | Packaging and deploying a trained policy |
| [Hardware interface](docs/interfaces/hardware.md) | Adding a hand or an arm |
| [Tools](docs/tools.md) | What every script in `tools/` is for |
| [Operator runbook](docs/operator-runbook.md) | **The authority for anything touching real hardware** |
| [Frozen decisions](docs/frozen-decisions.md) | Frozen hardware/format decisions and artifact provenance |

## CLI

```bash
dex-runtime preflight CONFIG                             # prove a deployment, actuate nothing
dex-runtime run CONFIG                                   # start the runtime
dex-runtime list-policies CONFIG                         # inspect the configured policy stores
dex-runtime verify-package PACKAGE [--allow-unsigned-local]
```

## Safety

This moves real hardware. [`docs/operator-runbook.md`](docs/operator-runbook.md)
is the authority; the essentials:

- E-stop and the documented preconditions come first.
- Fake mode is genuinely isolated. `--transport fake` and the soak verifier
  cannot open CAN, OpenXR, camera, or arm hardware.
- Real-arm switching is off by default. `--enable-rl-switch` is reserved for an
  explicitly authorized hardware-in-the-loop sequence.
- One owner per resource. Never run the LinkerHand ROS SDK alongside this
  runtime, and never start a second Hitbot owner. The exclusive `LinkerGateway`
  is the only CAN/hand command path; `dex_teleop/main_new.py` is imported for
  its reader and transform code but never run as a second hand owner.
- Ctrl-C in the launcher terminal is the supported stop; it shuts down in order.

## Scope

The implemented delivery boundary is the architecture document's thin critical
path:

- **M0** — frozen Linker mapping, calibration, schema, and canonical model
- **M1** — internal contracts, OpenXR/DexPilot semantic retargeting, exclusive Linker gateway
- **M2** — exact policy codecs, continuous shadow, fake-arm handoff, blend, hand-back
- **M3** — JSONL events and traces, terminal status, F12 PCsensor switching

Perception feeds operator surfaces only and never the policy; multi-process
operation, replay, and real-policy arm control remain deferred or gated behind
`--enable-rl-switch`. [docs/operator-runbook.md](docs/operator-runbook.md) is the
authority on what is releasable; [docs/frozen-decisions.md](docs/frozen-decisions.md)
records what cannot change.

## Development

```bash
lint-imports           # layering contracts (enforced in CI)
ruff check . && ruff format --check .
mypy
```

The layering in [`.importlinter`](.importlinter) is enforced, not advisory:
`dex_contracts` depends on nothing, teleop adapters may not reach hardware or
the runtime, and the hardware adapter may not reach the supervisor.
