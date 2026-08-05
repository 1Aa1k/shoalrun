"""Union the 1 m NAIP detections with the 10 m Sentinel ones.

Two detectors at very different resolutions disagree in informative ways, so
neither is simply thrown away:

  NAIP-only      Almost everything. Rocks too small for a 10 m pixel to ever
                 propose. This is the population that was missing.
  Both           Strongest evidence available here: two sensors, six NAIP
                 flights plus 29 Sentinel dates, independently agreeing.
  Sentinel-only  Kept but demoted. Mostly the sub-pixel false positives NAIP
                 already declined to confirm -- but a Sentinel detection is a
                 29-date persistence statistic, and NAIP is six clear-day
                 snapshots, so a real hazard that happened to be under chop on
                 all six flight days would land here. Not confident enough to
                 promote, not safe to delete.

Matching is by distance between centroids. A rock's 10 m centroid and its 1 m
centroid will not coincide exactly, so the tolerance has to exceed the coarse
pixel size without being so wide it merges genuinely separate rocks.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.strtree import STRtree
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
NAIP = ROOT / "data" / "rocks_naip.geojson"
SENT = ROOT / "data" / "verified.geojson"
OUT = ROOT / "data" / "hazards.geojson"

MATCH_M = 15.0  # a bit over one Sentinel pixel


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    naip = json.loads(NAIP.read_text())["features"]
    sent = json.loads(SENT.read_text())["features"]
    print(f"NAIP 1 m: {len(naip)}   Sentinel 10 m: {len(sent)}")

    def pt(f):
        p = f["properties"]
        return shape({"type": "Point", "coordinates": [p["lon"], p["lat"]]})

    naip_pts = [shp_transform(lambda x, y: fwd.transform(x, y), pt(f)) for f in naip]
    tree = STRtree(naip_pts) if naip_pts else None

    out = []
    matched_naip = set()

    for f in sent:
        p = dict(f["properties"])
        sp = shp_transform(lambda x, y: fwd.transform(x, y), pt(f))
        hit = None
        if tree is not None:
            for j in tree.query(sp.buffer(MATCH_M)):
                if naip_pts[j].distance(sp) <= MATCH_M:
                    hit = int(j)
                    break
        if hit is not None:
            matched_naip.add(hit)
            np_ = naip[hit]["properties"]
            p.update({
                "class": np_["class"],
                "area_m2": np_["area_m2"],
                "evidence": "both",
                "naip_flights": np_["flights"],
                "confidence_rank": 3,
            })
        else:
            p["evidence"] = "sentinel_only"
            p["confidence_rank"] = 1
        p["source"] = "merged"
        out.append({"type": "Feature", "properties": p, "geometry": None})

    for j, f in enumerate(naip):
        if j in matched_naip:
            continue
        p = dict(f["properties"])
        p["evidence"] = "naip_only"
        p["confidence_rank"] = 2
        out.append({"type": "Feature", "properties": p, "geometry": None})

    stats = defaultdict(int)
    cls = defaultdict(int)
    for f in out:
        stats[f["properties"]["evidence"]] += 1
        cls[f["properties"]["class"]] += 1

    print("\nevidence:")
    for k in ("both", "naip_only", "sentinel_only"):
        print(f"  {k:14s} {stats.get(k,0):5d}")
    print("\nclass:")
    for k, v in sorted(cls.items(), key=lambda kv: -kv[1]):
        print(f"  {k:10s} {v:5d}")
    print(f"\ntotal {len(out)}")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": out}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
