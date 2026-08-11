# Console layout design references

Visual references for the live control console. Read this before working from
any of the images — two of the four are superseded and describe a topology the
system no longer uses.

| Image | Status | What it shows |
|---|---|---|
| `vr-live-console-implemented.png` | **Current — implemented** | The actual 1920×1080 headless render with fake telemetry. This is what the console looks like today. |
| `vr-live-console-layout-proposal-v1.png` | Current — approved reference | The approved OpenXR layout: operator source left, D435 centre, Hitbot right. The implemented console follows this. |
| `d435-live-console-layout-proposal-v1.png` | **Superseded** | Early D435-centric proposal, from the Manus/Vive era |
| `d435-live-console-layout-proposal-v2.png` | **Superseded** | Second iteration of the same, also pre-OpenXR |

The two `d435-*` proposals predate the 2026-08-09 move from the Manus/Vive
topology to OpenXR/Quest 3S. They are kept because they record why the layout
ended up as it did, not because anything should be built from them.

For the reasoning behind the change, see
[`../live-control-console-implementation-plan.md`](../live-control-console-implementation-plan.md).
For the console implementation itself, see [`../tools.md`](../tools.md).
