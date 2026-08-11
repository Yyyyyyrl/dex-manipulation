# Console layout design references

Visual references for the live control console. Both images describe the current
OpenXR/Quest 3S topology.

| Image | Status | What it shows |
|---|---|---|
| `vr-live-console-implemented.png` | **Current — implemented** | The actual 1920×1080 headless render with fake telemetry. This is what the console looks like today. |
| `vr-live-console-layout-proposal-v1.png` | Current — approved reference | The approved OpenXR layout: operator source left, D435 centre, Hitbot right. The implemented console follows this. |

Two earlier `d435-*` layout proposals, from before the 2026-08-09 move from the
Manus/Vive topology to OpenXR/Quest 3S, were removed. They are recoverable from
git history if the earlier reasoning is ever needed.

For the reasoning behind the change, see
[`../live-control-console-implementation-plan.md`](../live-control-console-implementation-plan.md).
For the console implementation itself, see [`../tools.md`](../tools.md).
