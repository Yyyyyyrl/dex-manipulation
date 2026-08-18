# Console layout design references

Visual reference for the live control console, on the current OpenXR/Quest 3S
topology.

| Image | Status | What it shows |
|---|---|---|
| `vr-live-console-implemented.png` | **Current — implemented** | The actual 1920×1080 headless render with fake telemetry. This is what the console looks like today. |

Earlier layout proposals — two `d435-*` images from before the 2026-08-09 move
from the Manus/Vive topology to OpenXR/Quest 3S, and the approved OpenXR
proposal the implemented console was built from — were removed once the console
landed. They are recoverable from git history if the earlier reasoning is ever
needed.

For the console implementation itself, see [`../tools.md`](../tools.md). For the
control path it renders, see [`../architecture.md`](../architecture.md).
