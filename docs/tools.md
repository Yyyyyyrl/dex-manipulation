# `tools/` — commissioning and demonstration

`tools/` is a repository-local package: not part of the runtime, not installed
as a distribution, and never imported by anything under `src/`. It holds the
surfaces used to bring hardware up, demonstrate the system, and capture
evidence.

Everything here runs the **production** runtime, handoff, safety, mapping, and
gateway. Where a device is unavailable, a tool substitutes a bounded fake for
that device only — it does not substitute a fake control path.

## Which entry point should I use?

Two, depending on whether you are developing or running a real session.

| | `run_console.py` | `start_live_ui.sh` |
|---|---|---|
| Interface | Browser, HTTP + SSE | Launches the browser console |
| Status | **Current. Start here.** | **The authorized live path** |
| Hardware | Real hand, or none | Real everything |
| Operator input | Real OpenXR, or virtual | Real Quest 3S / WiVRn |
| Use it when | Developing, demoing, verifying | Running the real supervised session |

Both drive the same `HandOnlyRuntime`; they differ in what they substitute and
in how much they are allowed to do. `tools/control_console/` is the shared
implementation they are built from — it is a library, not an entry point.

For anything involving real hardware, [`operator-runbook.md`](operator-runbook.md)
is the authority, not this file.

## Entry points

### `run_console.py`
Browser console for teleop/RL hand switching. Runs the hand-only runtime behind
a stdlib HTTP server with Server-Sent Events, bundled offline fonts, full
16-joint hand state, OpenXR monitoring, and an optional clearly-labelled
synthetic arm source.

```bash
# no hardware at all -- note that every source must be faked separately
python tools/run_console.py --transport fake --policy synthetic \
    --vr fake --vr-python .venv/bin/python --arm-telemetry fake --camera fake

python tools/run_console.py --transport hand   # real LinkerHand on can0
```

`--transport` selects the hand only. `--policy`, `--vr`, `--camera`, and
`--arm-telemetry` each default to `real`, so `--transport fake` on its own tries
to build a real policy from `dex-forge` and exits — with `FileNotFoundError` if
there is no `dex-forge` checkout on the host, or `PolicyPackageExportError` if
there is one but the bundle cannot be exported. The module docstring's two-line
summary is misleading on this point; `build_fake_command()` in `soak_verify.py`
is the authoritative fake invocation.

### `start_live_ui.sh`
The supervised live launcher, and the only sanctioned way to start the full
stack. Requires an exact `CONFIRM` token; starts WiVRn, the Quest 3S OpenXR hand
source, LinkerHand, the D435 source in its isolated camera interpreter, and the
single OpenXR/Hitbot owner; opens the console; and keeps the policy in
`RL_SHADOW`. Ctrl-C performs an ordered shutdown.

```bash
tools/start_live_ui.sh --dry-run   # check preconditions, start nothing
```

`--enable-rl-switch` exists but is reserved for the explicitly authorized
hardware-in-the-loop sequence.

### `vr_hitbot_controller.py`
**The single Hitbot SDK owner.** Consumes the shared OpenXR wrist stream, does
IK and TCP pose integration, and serves the hold protocol that
`src/dex_runtime/real_arm.py` speaks. Never start a second one. During a hold it
repeats one fixed ServoJ target, verifies TCP stability, and discards operator
wrist deltas.

Runs under `.venv-hitbot`, a separate interpreter — see the note at the end.

### `demo_policy_factory.py`
Builds a **synthetic** policy package from nothing — no `dex-forge` checkout and
no trained weights required. This is the fastest way to get a package that
`verify-package`, `preflight`, and the console will accept. Use
`build_demo_policy.py` instead when you have a real Stage-2 export.

### `build_demo_policy.py`
Repackages a `dex-forge` Stage-2 `deploy.pth` into a package this runtime will
load: strips fields the runtime rejects, rebinds calibration compatibility from
the training hand to the deployment hand, checks action bounds against the
safety envelope, and recomputes the content-addressed id. See
[interfaces/policy.md](interfaces/policy.md).

### `switch_demo_backend.py`
Shared backend for the web demo: runtime construction, virtual OpenXR source,
visible retargeter, synthetic policy. Imported, not run. `_base_config()` is the
canonical worked example of a deployment config.

### Bridges

Both are senders that pair with a receiver in `control_console/`. They exist
because the console's venv cannot import ROS 2, and because the OpenXR stream
has two consumers.

| Sender | Receives in | Purpose |
|---|---|---|
| `manus_glove_bridge.py` | `control_console/manus_source.py` | Runs in the ROS 2 environment, subscribes to `ManusGlove`, datagrams the 25-node skeleton to loopback |
| `openxr_hand_bridge.py` | `dex_teleop_adapters.UdpOpenXRSource` and `vr_hitbot_controller.py` | Fans the 26-joint Quest hand stream out to both the hand runtime and the arm owner, loopback only |

### Setup scripts

`install_d435_udev.sh` installs the RealSense udev rule from `vendor/realsense/`.
Run once, then replug the camera.

## `tools/control_console/`

The console implementation. A library, plus two runnable utilities.

| Module | Role |
|---|---|
| `server.py` | Threaded HTTP server: static assets and the SSE event stream |
| `telemetry.py` | Aggregates runtime, policy, teleop, linker, and arm snapshots into console payloads |
| `camera_source.py` | D435 RGB/depth frame buffer, plus `SyntheticD435Source` |
| `realsense_worker.py` | Camera subprocess, isolated so a stall cannot block the control loop |
| `arm_listener.py` | **Read-only** UDP listener for Hitbot cycle telemetry. Opens no robot SDK. |
| `manus_source.py` | Validating UDP receiver for the Manus bridge |
| `build_fonts.py` | Builds the digest-verified WOFF2 bundle. The console is offline by design — no CDN. |
| `soak_verify.py` | **Runnable.** Hardware-free soak |
| `hil_observe.py` | **Runnable.** Read-only evidence capture from an authorized live console |

### `soak_verify.py`

The best zero-hardware check in the repository. Hard-wired to fake transport,
synthetic policy, fake OpenXR, fake D435, and fake arm telemetry; it cannot be
configured to open real hardware, and never calls switch or stop.

```bash
python -m tools.control_console.soak_verify --duration-s 30 --viewer-count 1
```

Minimum duration 30 s. Reports request timing percentiles, RSS growth against
`--max-rss-growth-mib`, and any unhealthy samples.

### `hil_observe.py`

Reads the loopback aggregate snapshot of an already-authorized live console and
validates it. Imports no hardware SDK and exposes no control action, so it is
safe to run during a live session to capture evidence.

## Notes for maintainers

**Bench commissioning has no dedicated UI.** A Tkinter UI (`hil_switch_ui.py`)
used to cover testing a real hand with no VR available, by substituting virtual
operator input. It was removed as duplicated surface — it independently
reproduced policy loading, retargeter construction, and operator switch
handling. `run_console.py` covers the same ground, but if you need that
workflow back, the virtual-source approach is worth recovering from git history
rather than rewriting.

**`.venv-hitbot` is load-bearing and undocumented.** `start_live_ui.sh` hard-codes
`.venv-hitbot/bin/python` as the Hitbot interpreter and fails if it is missing.
It currently contains a compiled `pysurvive` build that is not reproducible from
`bootstrap.sh` or any other checked-in recipe. If that directory is lost, the
live launcher cannot start, and there is no documented way to rebuild it. Worth
fixing.

**Arm controller logs are unbounded.** A live run can emit hundreds of thousands
of repeated tracebacks into `.artifacts/control-console/live-runs/*/hitbot.log`
when the arm's TCP socket breaks, with no rotation and no retry backoff. One
observed run reached 1.6 GB. `.artifacts/` is gitignored, so this is a disk and
diagnosability problem rather than a repository one.
