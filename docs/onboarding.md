# Onboarding

*[English](onboarding.md) | [中文](zh/onboarding.md)*

Getting from `git clone` to a running system, with no hardware. Budget 30
minutes, most of which is downloading PyTorch.

## 1. Environment

```bash
./bootstrap.sh core     # runtime + tests, no hardware extras
./bootstrap.sh all      # everything, including CAN, evdev, RealSense, Manus
source .venv/bin/activate
```

**Use Python 3.10–3.12.** `requires-python` says `>=3.10`, but the pinned
`numpy==1.26.0` has no wheels for 3.13, and `bootstrap.sh` will fail with an
unhelpful `No matching distribution found for numpy==1.26.0`. Override the
interpreter if your default is newer:

```bash
PYTHON=python3.12 ./bootstrap.sh core
```

What the two profiles differ on:

| | `core` | `all` |
|---|---|---|
| numpy, PyYAML, safetensors, torch (CPU) | yes | yes |
| import-linter, ruff, mypy | no¹ | yes |
| `python-can` (LinkerHand CAN) | no | yes |
| `evdev` (F12 foot switch) | no | yes |
| OpenCV + `pyrealsense2` (D435) | no | yes |
| `dex-retargeting` (the solver) | no | yes |
| LinkerHand ROS SDK, pinned + patched into `.vendor/` | no | yes |

¹ CI installs these on top of `core`; see [`ci.yml`](../.github/workflows/ci.yml).

`all` additionally clones the LinkerHand ROS SDK at a pinned commit and applies
`vendor/patches/linkerhand-g20-required.patch`, verifying the driver's SHA-256
before and after. If the driver content is unrecognised it refuses to proceed
rather than patching something unexpected.

Live Manus input additionally needs ROS 2 and the `manus_ros2_msgs` workspace on
the host. These are runtime capabilities, checked before any subscription is
created, not Python dependencies.

## 2. Prove the tree is healthy

```bash
lint-imports              # the three layering contracts
ruff check . && ruff format --check .
mypy
```

## 3. Run it, with no hardware at all

The fastest honest end-to-end check is the soak verifier. It starts the real
runtime behind a fake transport, a synthetic policy, fake OpenXR input, a fake
D435, and fake arm telemetry, drives it, and reports timing and leak stats. It
is hard-wired to fake everything and *cannot* be configured to open real
hardware.

```bash
python -m tools.control_console.soak_verify --duration-s 30 --viewer-count 1
```

Minimum duration is 30 seconds. Expect JSON on stdout ending with per-request
timing percentiles and an empty `unhealthy_samples` list.

To look at it instead of measuring it, start the console directly and open
<http://127.0.0.1:8765/>:

```bash
python tools/switch_web_demo.py --transport fake --policy synthetic \
    --vr fake --vr-python .venv/bin/python --arm-telemetry fake --camera fake
```

**Every source has to be faked separately.** `--transport` selects only the
hand; `--policy`, `--vr`, `--camera`, and `--arm-telemetry` each default to
`real`. Passing `--transport fake` alone fails while trying to build a real
policy from `dex-forge`:

```
PolicyPackageExportError: bundle config is missing ['observation_semantics_version']
```

If you see that, you are missing one of the other flags, not misconfigured.

## 4. The CLI

Four subcommands, all of which need a *policy package* — a self-describing
directory, not a bare checkpoint. See
[interfaces/policy.md](interfaces/policy.md) for the format.

```bash
dex-runtime verify-package PACKAGE [--allow-unsigned-local]  # validate a package
dex-runtime preflight CONFIG      # prove a deployment is coherent, actuate nothing
dex-runtime list-policies CONFIG  # what the configured stores contain
dex-runtime run CONFIG            # start the runtime
```

The repository ships no example deployment config, because a valid one must
reference a real policy package and a real hand serial number. To get a package
to experiment with, build a synthetic one:

```bash
python -c "
from pathlib import Path
from tools.demo_policy_factory import write_demo_package
print(write_demo_package(Path('/tmp/dex-demo')))
"
dex-runtime verify-package /tmp/dex-demo --allow-unsigned-local
```

That prints the validated `PolicyDescriptor`, including the content-addressed
`package_id`. For the shape of a deployment config, read
`_base_config()` in [`tools/switch_demo_backend.py`](../tools/switch_demo_backend.py) —
it is the canonical example, kept working because the console depends on it.

`preflight` is always safe to run: it loads and cross-checks the deployment,
calibration, teleop profile, and policy package, and opens no hardware.

## 5. Repository map

"I want to change X — where do I look?"

| I want to… | Start at |
|---|---|
| Add a glove / VR device | [`interfaces/teleop.md`](interfaces/teleop.md), then `src/dex_teleop_adapters/protocols.py` |
| Deploy a policy I trained | [`interfaces/policy.md`](interfaces/policy.md), then `tools/build_demo_policy.py` |
| Change when the policy may take over | `src/dex_runtime/handoff.py` |
| Change a safety limit | Deployment config `safety` block → `src/dex_runtime/safety.py` |
| Add a precondition to switching | `src/dex_runtime/readiness.py` (add a provider) |
| Support a different hand | [`interfaces/hardware.md`](interfaces/hardware.md), then `src/dex_hardware_linker/` |
| Change what the console shows | `tools/control_console/` |
| Understand a rejected command | `safety.py` reason codes, then the JSONL event log |
| Add a config field | `src/dex_runtime/deployment.py` (strict; unknown keys are errors) |

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No matching distribution found for numpy==1.26.0` | Python 3.13. Use `PYTHON=python3.12 ./bootstrap.sh core`. |
| CAN permission denied | `can0` needs to be up and the user in the right group. Confirm with `ip link show can0` before blaming the runtime. |
| D435 not found | Install the udev rule: `sudo tools/install_d435_udev.sh`, then replug. |
| WiVRn / OpenXR session won't start | The headset session is managed outside this repo. `tools/start_live_ui.sh --dry-run` checks the preconditions without starting hardware. |
| Hand does not move, no errors | Check the event log for safety rejections. `sent-to-bus` acknowledgement means the frame left the host, *not* that the hand moved. |
| `refusing unknown G20 driver content` | The pinned LinkerHand SDK driver does not match either expected SHA-256. Do not bypass it; the patch encodes a required fix. |

## 7. Safety

This runtime moves real hardware. Before anything touches a real hand or arm,
[`operator-runbook.md`](operator-runbook.md) is the authority, not this
document.

The parts worth knowing up front:

- **E-stop and the preconditions checklist come first.** The runbook lists them.
- **Fake mode is genuinely isolated.** `--transport fake` and the soak verifier
  cannot open CAN, OpenXR, camera, or arm hardware. Use them freely.
- **Real-arm switching is off by default.** `--enable-rl-switch` exists but is
  reserved for an explicitly authorized hardware-in-the-loop sequence. The UI
  deliberately distinguishes "not authorized" from "authorized but still waiting
  for a verified arm hold".
- **One owner per resource.** Never run the LinkerHand ROS SDK next to this
  runtime, and never start a second Hitbot owner. See
  [architecture.md](architecture.md#threading-and-exclusive-ownership).
- **Ctrl-C in the launcher terminal is the supported stop.** It performs an
  ordered shutdown; killing the process does not.
