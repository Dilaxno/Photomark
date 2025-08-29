"""
Generate LUT previews for a single source image and save results into
"frontend/public/LUTs Previews" so the site can serve them statically.

- Input image: d:\Software\prv.jpg
- LUT sources: server's loaded LUTs (from app.utils.luts)
- Output: d:\Software\frontend\public\LUTs Previews\<lut_name>.jpg

Usage (PowerShell):
  # Ensure the virtualenv is active and deps are installed
  # pip install -r d:\Software\requirements.txt
  python d:\Software\scripts\generate_lut_previews.py
"""
from __future__ import annotations
import os
import sys
from typing import List

# Local imports from the backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import logger  # noqa: E402
from app.utils import luts as luts_mod  # noqa: E402

SRC_IMAGE = r"d:\Software\prv.jpg"
OUT_DIR = r"d:\Software\frontend\public\LUTs Previews"
ENGINE = "auto"  # 'auto' | 'numpy' | 'torch'


def ensure_dirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def main() -> None:
    # Initialize LUTs using same logic as app.main
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "static"))
    luts_dir = os.getenv("LUTS_DIR") or os.path.join(static_dir, "luts")
    try:
        luts_mod.load_luts_from_dir(luts_dir)
    except Exception as ex:
        logger.warning("LUTs not initialized: %s", ex)

    lut_names: List[str] = luts_mod.list_luts()
    if not lut_names:
        logger.error("No LUTs available. Ensure .cube files exist in: %s", luts_dir)
        sys.exit(2)

    if not os.path.isfile(SRC_IMAGE):
        logger.error("Source image not found: %s", SRC_IMAGE)
        sys.exit(2)

    with open(SRC_IMAGE, "rb") as f:
        raw = f.read()

    ensure_dirs(OUT_DIR)

    ok = 0
    for name in lut_names:
        try:
            out = luts_mod.apply_lut(raw, name, engine=ENGINE)
            out_path = os.path.join(OUT_DIR, f"{name}.jpg")
            with open(out_path, "wb") as wf:
                wf.write(out)
            ok += 1
            print(f"✔ {name} -> {out_path}")
        except Exception as ex:
            print(f"✖ {name} failed: {ex}")

    print(f"Done. Wrote {ok}/{len(lut_names)} previews to: {OUT_DIR}")


if __name__ == "__main__":
    main()