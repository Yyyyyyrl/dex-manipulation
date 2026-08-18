# Frozen decisions and artifact provenance

This records the decisions that are *frozen* — the ones downstream code, golden
fixtures, and calibrations are built around, which cannot be changed without
invalidating a shipped artifact. It is provenance, not status.

For delivery status and what must be true before touching real hardware, see
[operator-runbook.md](operator-runbook.md), which is the authority. For how the
system is put together, see [architecture.md](architecture.md).

## Provenance

The runtime was implemented against a fixed revision of an external
architecture document:

- source document: `dex-forge/docs/dex-architecture-revision-v2.md`
- source SHA256: `29120e020b07e527cf1ae98d40f446539e5d569552a1ba9c4013ed2ad9564f4e`
- runtime distribution name: `dex-manipulation`

This repository does not vendor that document. The digest is recorded so the
revision the contracts were derived from can be identified later.

## Frozen decisions

| Decision | Frozen value | Where it is enforced |
|---|---|---|
| Hand | LinkerHand G20, **left** | `src/dex_hardware_linker/` |
| Hand serial | `LHT20-010-415-L-B-1-D` | recorded in the frozen calibration |
| `thumb_cmc_roll` bias | **−10 degrees**, retained deliberately | versioned TeleopProfile |
| Policy package format | canonical JSON manifest + separate actor and adapter Safetensors, SHA256 content identity | `src/dex_runtime/policy_package.py` |
| Package trust mode | unsigned packages allowed **only** from a local immutable store, explicitly | `verify-package --allow-unsigned-local` |
| Operator switch | **F12** | `src/dex_runtime/operator_switch.py` |
| Foot switch identity | PCsensor USB `3553:b001`, configured evdev by-id path, exclusive grab, debounce | `operator_switch.py` |

The `thumb_cmc_roll` bias is a pre-existing hardware bias, not a tuning
parameter. Changing it invalidates the mapping golden fixture below.

## Frozen artifact digests

These pin frozen *data*. All three are verifiable against the current checkout:

```bash
shasum -a 256 \
  src/dex_hardware_linker/assets/calibrations/linker_g20_left_lht20_010_415_v1.json \
  assets/golden/linker_mapping_golden_v1.json \
  assets/golden/proprio_codec_golden_v1.json
```

| Artifact | SHA256 |
|---|---|
| frozen G20 left calibration | `9dfcb4b26cb0db69877b1b7ab23c07511996dfd5fd66169ed992daffc05368d0` |
| mapping golden fixture | `ccbee30e5881a990e5cf32d67df164e3056cbef075f647f1f8b9d0e9d763253a` |
| proprio codec golden fixture | `cf7b2aa582becbb1ad0258e91c08f566e7734a35589e613ebcac9b70c4d2f3e1` |

If any of these stops matching, either a frozen artifact was edited or a golden
fixture was regenerated. Both need review rather than a digest update.

Source-file digests are deliberately **not** recorded here. An earlier revision
of this document pinned a SHA256 for `src/dex_runtime/codecs.py`; ordinary
maintenance of that file invalidated it, which made the record look like a
failure when nothing was wrong. What is worth pinning is frozen *data*, above.

This repository carries no automated test suite. These digests are therefore the
only remaining guard on the frozen artifacts, and nothing recomputes them for
you — run the command above by hand before trusting a checkout.

## Cross-repository boundary

`dex-manipulation` does not import `dex-forge` training code and does not depend
on Isaac Lab. The two repositories exchange only golden fixtures and exported
policy package files, by value. The layering that keeps this true is enforced by
`lint-imports` against [`../.importlinter`](../.importlinter), not by convention.
