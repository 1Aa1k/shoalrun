"""Re-select which annotator sections to show. Reuses rendered chips; no network.

The first cut let a 400 m section qualify if it merely touched water beyond the
50 m shore buffer, so frames straddling the bank got through with most of their
area on land. Nate's complaint, and correct: those are the sections where he
already knows the rocks are, and marking them teaches the scorer nothing about
open water.

This applies a stricter rule -- the WHOLE frame must sit beyond the buffer, so
no shoreline appears in the picture at all -- and rewrites index.html against
the chips that are already on disk. Section ids and filenames are untouched, so
existing marks in localStorage stay valid and any section dropped here can be
brought back by loosening the numbers rather than re-rendering 188 MB.

The buffer shrinks from island edges too, since the lake polygon carries islands
as holes. An island shore is a shore.

  SHOALRUN_OFFSHORE_M  distance from any shore            (default 100)
  SHOALRUN_COVER       fraction of the frame that must
                       be beyond it, 1.0 = all of it      (default 1.0)
"""

import json
import os
import sys
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "annotate"

OFFSHORE_M = float(os.environ.get("SHOALRUN_OFFSHORE_M", "100"))
COVER = float(os.environ.get("SHOALRUN_COVER", "1.0"))


def main():
    meta = json.loads((OUT / "sections.json").read_text())
    sec_m = meta["section_m"]
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(
        lambda x, y: fwd.transform(x, y),
        shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"]),
    )
    off = lake.buffer(-OFFSHORE_M)

    kept = []
    for s in meta["sections"]:
        ox, oy = s["origin"]
        b = box(ox, oy - sec_m, ox + sec_m, oy)
        frac = off.intersection(b).area / (sec_m * sec_m)
        if frac >= COVER - 1e-9:
            s = dict(s)
            s["offshore_frac"] = round(frac, 3)
            kept.append(s)

    if not kept:
        raise SystemExit(f"nothing survives {OFFSHORE_M:g} m at {COVER:.0%} cover")

    # Busiest water first, so stopping early still samples where the detectors
    # disagree rather than empty middle-of-the-lake.
    kept.sort(key=lambda s: -len(s["haz"]))
    sub = dict(meta)
    sub["sections"] = kept
    sub["offshore_m"] = OFFSHORE_M
    sub["cover"] = COVER

    tpl = (ROOT / "web" / "annotate.template.html").read_text()
    (OUT / "index.html").write_text(tpl.replace("/*DATA*/", json.dumps(sub)))

    n_haz = sum(len(s["haz"]) for s in kept)
    print(f"{len(kept)} of {len(meta['sections'])} sections kept "
          f"(entire frame >= {OFFSHORE_M:g} m from any shore)")
    print(f"{n_haz} of our detections fall inside them")
    print(f"area shown: {len(kept) * sec_m * sec_m / 1e6:.1f} km2")
    print(f"\nrewrote {OUT / 'index.html'} -- chips reused, marks preserved")


if __name__ == "__main__":
    main()
