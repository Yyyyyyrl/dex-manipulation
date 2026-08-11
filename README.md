# dex-manipulation

Neutral hardware runtime for the Dexterous Manipulation Architecture V2.1.
This repository owns contracts, teleoperation adapters, policy execution,
exclusive hardware gateways, handoff supervision, and observability. It does
not depend on Isaac Lab or import `dex-forge` training code.

The implemented delivery boundary is the document's thin critical path:

- M0: frozen Linker mapping, calibration, schema, and canonical model;
- M1: internal contracts, OpenXR/DexPilot semantic retargeting, and exclusive Linker gateway;
- M2: exact policy codecs, continuous shadow, fake-arm handoff, blend, and hand-back;
- M3: JSONL events/traces, terminal status, and F12 PCsensor switching.

Perception, multi-process, replay, and real-policy arm-control release work
remain deferred or gated as recorded in the implementation plan.

Run `./bootstrap.sh`, then `source .venv/bin/activate` and `pytest`.

After the documented hardware and E-stop preconditions pass, start the
supervised English live console with:

```bash
./tools/start_live_ui.sh
```

The launcher requires the exact `CONFIRM` token, runs
`flatpak run io.github.wivrn.wivrn//stable`, starts the Quest 3S/WiVRn
OpenXR hand source, LinkerHand, the read-only D435 RGB/depth source in the
host's isolated camera Python, and the single OpenXR/Hitbot owner, opens
`http://127.0.0.1:8765/`, and keeps the synthetic policy in `RL_SHADOW`. Press
Ctrl-C in the launcher terminal for ordered safe shutdown. Use `--dry-run` to
check prerequisites without starting hardware. When real Hitbot telemetry is
enabled, both the UI control and the backend switch endpoint remain disabled
by default. The real-arm hold gateway is implemented, but
`--enable-rl-switch` is reserved for the documented, explicitly authorized HIL
release sequence. The UI distinguishes an unauthorized switch from an
authorized switch that is still waiting for verified arm hold.

The OpenXR integration imports `VRHandReader`, wrist-delta transforms, and arm
controller contracts from `/home/user/dex_teleop/main_new.py` and its modules.
It does not run `main_new.py` as a second LinkerHand owner: the exclusive
`LinkerGateway` in this repository remains the only CAN/hand command path.
Use `--no-wivrn` only when WiVRn is already managed externally.


Implementation status and uncompleted physical release gates are recorded in
[`docs/implementation-progress.md`](docs/implementation-progress.md). The
approved hand-only operating and teleop-only rollback procedure is in
[`docs/operator-runbook.md`](docs/operator-runbook.md).

The implemented operator commands are:

```text
dex-runtime preflight CONFIG
dex-runtime run CONFIG
dex-runtime list-policies CONFIG
dex-runtime verify-package PACKAGE [--allow-unsigned-local]
```
