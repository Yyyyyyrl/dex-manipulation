# dex-manipulation

Neutral hardware runtime for the Dexterous Manipulation Architecture V2.1.
This repository owns contracts, teleoperation adapters, policy execution,
exclusive hardware gateways, handoff supervision, and observability. It does
not depend on Isaac Lab or import `dex-forge` training code.

The implemented delivery boundary is the document's thin critical path:

- M0: frozen Linker mapping, calibration, schema, and canonical model;
- M1: internal contracts, Manus semantic retargeting, and exclusive Linker gateway;
- M2: exact policy codecs, continuous shadow, fake-arm handoff, blend, and hand-back;
- M3: JSONL events/traces, terminal status, and F12 PCsensor switching.

Later arm-vendor, Vive-stack, perception, multi-process, and rich-UI work is
intentionally deferred by the architecture document.

Run `./bootstrap.sh`, then `source .venv/bin/activate` and `pytest`.


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
