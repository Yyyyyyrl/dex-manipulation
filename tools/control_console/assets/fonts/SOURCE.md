# Bundled UI font provenance

The live console uses Latin subsets of Noto Sans and Noto Sans Mono. The
browser loads only the WOFF2 files in this directory. It does not use an
operating-system font path or an external font service at runtime.

Source files used for this build:

| Source file | SHA256 |
|---|---|
| `NotoSans-Regular.ttf` | `89c3c497f618fdaa0b2d1e98fef93582f28c71debd2c4a8cdf41f190ced2909d` |
| `NotoSans-Bold.ttf` | `e83493c945848ecd4a9ad0f6d19164541a0d3e23a9c952304a00a46e00272ac5` |
| `NotoSansMono-Regular.ttf` | `6b692c4b6d15ccf59f1c1fe8d11cb8a92f51960f3e9f1f523781755a3af7e29f` |

Build tooling:

- fontTools `4.55.3`;
- Brotli `1.1.0`;
- output flavor `woff2`;
- glyph ranges are declared in `tools/control_console/build_fonts.py`.

The fonts are distributed under the SIL Open Font License 1.1. The complete
Debian Noto copyright and license notice is preserved in `LICENSE.txt`.

