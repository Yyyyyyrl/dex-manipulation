#!/usr/bin/env python3
"""Regenerate the console's deterministic Latin WOFF2 assets."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "assets" / "fonts"
UNICODES = "U+0000-00FF,U+2000-206F,U+2100-214F,U+2190-21FF,U+2200-22FF,U+25A0-25FF,U+2600-26FF"
SOURCES = {
    "NotoSans-Regular.ttf": (
        "ui-regular.woff2",
        "89c3c497f618fdaa0b2d1e98fef93582f28c71debd2c4a8cdf41f190ced2909d",
    ),
    "NotoSans-Bold.ttf": (
        "ui-bold.woff2",
        "e83493c945848ecd4a9ad0f6d19164541a0d3e23a9c952304a00a46e00272ac5",
    ),
    "NotoSansMono-Regular.ttf": (
        "ui-mono.woff2",
        "6b692c4b6d15ccf59f1c1fe8d11cb8a92f51960f3e9f1f523781755a3af7e29f",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(source_dir: Path) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for source_name, (output_name, expected_source_digest) in SOURCES.items():
        source = source_dir / source_name
        if not source.is_file():
            raise FileNotFoundError(f"missing font source: {source}")
        actual_source_digest = _sha256(source)
        if actual_source_digest != expected_source_digest:
            raise RuntimeError(
                f"source digest mismatch for {source_name}: "
                f"{actual_source_digest} != {expected_source_digest}"
            )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "fontTools.subset",
                str(source),
                f"--output-file={OUTPUT / output_name}",
                "--flavor=woff2",
                f"--unicodes={UNICODES}",
                "--layout-features=*",
                "--name-IDs=*",
                "--name-legacy",
                "--name-languages=*",
                "--glyph-names",
                "--symbol-cmap",
                "--legacy-cmap",
                "--notdef-glyph",
                "--notdef-outline",
                "--recommended-glyphs",
            ],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/usr/share/fonts/truetype/noto"),
        help="directory containing the exact source TTF files listed in SOURCE.md",
    )
    args = parser.parse_args()
    build(args.source_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
