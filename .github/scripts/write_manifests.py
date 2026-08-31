#!/usr/bin/env python3
"""Write the ESP Web Tools manifests that the browser installer flashes from.

One manifest per board profile in ``glucocube.contract.ESP32_BOARDS``, each
naming that board's factory image on this release. The board list is the
source of truth: a profile added there and built by the release workflow
gets an installer entry without anyone remembering to add one.

The URLs name the release explicitly rather than pointing at "latest", so a
page opened while a release is being published cannot hand somebody the
bootloader from one version and the app from another.

    python3 .github/scripts/write_manifests.py --version 2.1.0 --tag v2.1.0 \
        --out artifacts/manifests
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from glucocube import contract  # noqa: E402

RELEASE_URL = "https://github.com/{repo}/releases/download/{tag}/{asset}"

# ESP Web Tools names chip families this way; the profiles are all S3 today,
# which is why each board gets its own manifest rather than one manifest
# with several builds — the browser would have no way to tell them apart.
CHIP_FAMILIES = {"esp32s3": "ESP32-S3", "esp32": "ESP32", "esp32c3": "ESP32-C3"}


def manifest(board: dict, version: str, tag: str) -> dict:
    asset = f"glucocube-{board['id']}-{version}-factory.bin"
    return {
        "name": f"GlucoCube — {board['name']}",
        "version": version,
        "funding_url": f"https://github.com/{contract.REPO}",
        # A board being flashed from the browser is either new or being
        # moved between versions by hand; erasing first is what makes the
        # second case behave like the first.
        "new_install_prompt_erase": True,
        "builds": [
            {
                "chipFamily": CHIP_FAMILIES.get(board["chip"], "ESP32-S3"),
                "parts": [
                    {
                        "path": RELEASE_URL.format(
                            repo=contract.REPO, tag=tag, asset=asset,
                        ),
                        "offset": 0,
                    },
                ],
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for board in contract.ESP32_BOARDS:
        path = out / f"manifest-{board['id']}.json"
        path.write_text(
            json.dumps(manifest(board, args.version, args.tag), indent=2,
                       ensure_ascii=False) + "\n"
        )
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
