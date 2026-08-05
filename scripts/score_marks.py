"""Score every detector against Nate's marked rocks -- recall AND precision.

This is the first scoring in the project that can measure precision honestly,
and precision is the number that was always missing.

Recall alone cannot be trusted: with thousands of detections on a 34 km2 lake, a
random scatter finds most reference rocks by luck, so recall rewards emitting
more. Precision is the opposite pressure -- of the things flagged, how many are
real -- but it requires knowing where rocks are NOT, and that requires water
somebody actually examined. The annotator records exactly that.

So scoring is confined to reviewed sections. Inside them, absence of a mark
means "no rock here" and a detection there is genuinely a false alarm. Outside
them nothing can be concluded, and nothing is.

Reported per layer:

  recall     marked rocks with a detection within MATCH_M
  precision  detections (in reviewed water) within MATCH_M of a marked rock
  F1         the two combined, since either alone is trivially gamed
  null       the same recall from a random scatter of equal size in the same
             reviewed water -- what luck alone would score
"""

import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

MATCH_M = 15.0   # tighter than the 30 m used against OSM: these marks are
                 # placed on 0.5 m imagery, so they are good to a few metres and
                 # a loose radius would hand out credit that was not earned.
N_NULL_TRIALS = 7
SEED = 41

LAYERS = (
    ("sentinel-10m", "verified.geojson"),
    ("naip-1m", "rocks_naip.geojson"),
    ("naip-0.3m", "rocks_naip_03.geojson"),
    ("naip-bright-1m", "rocks_bright.geojson"),
    ("merged", "hazards.geojson"),
)


def main():
    marks_p = DATA / "marked_rocks.geojson"
    region_p = DATA / "reviewed_region.geojson"
    if not marks_p.exists():
        raise SystemExit("no marked_rocks.geojson -- run ingest_marks.py first")

    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_m = lambda g: shp_transform(lambda x, y: fwd.transform(x, y), g)

    marks = [to_m(shape(f["geometry"]))
             for f in json.loads(marks_p.read_text())["features"]]
    if not marks:
        raise SystemExit("no marks in marked_rocks.geojson")
    region = to_m(shape(json.loads(region_p.read_text())["geometry"])) if region_p.exists() else None
    if region is None:
        raise SystemExit("no reviewed_region.geojson -- precision needs it")

    mark_tree = STRtree(marks)
    rng = np.random.default_rng(SEED)
    print(f"{len(marks)} marked rocks in {region.area/1e6:.2f} km2 of reviewed water")
    print(f"matching at {MATCH_M:.0f} m\n")
    print(f"{'layer':16s}{'in region':>10s}{'recall':>8s}{'null':>7s}"
          f"{'prec':>7s}{'F1':>7s}")

    minx, miny, maxx, maxy = region.bounds
    for label, fn in LAYERS:
        p = DATA / fn
        if not p.exists():
            continue
        feats = json.loads(p.read_text())["features"]
        pts = [Point(*fwd.transform(f["properties"]["lon"], f["properties"]["lat"]))
               for f in feats if f["properties"].get("lon") is not None]
        # Only detections inside reviewed water can be judged.
        pts = [q for q in pts if region.contains(q)]
        if len(pts) < 5:
            continue
        tree = STRtree(pts)

        found = sum(1 for m in marks
                    if any(pts[int(j)].distance(m) <= MATCH_M
                           for j in tree.query(m.buffer(MATCH_M))))
        hits = sum(1 for q in pts
                   if any(marks[int(j)].distance(q) <= MATCH_M
                          for j in mark_tree.query(q.buffer(MATCH_M))))
        recall = found / len(marks) * 100
        prec = hits / len(pts) * 100
        f1 = (2 * recall * prec / (recall + prec)) if (recall + prec) else 0.0

        # Null: the same number of points scattered at random through the same
        # reviewed water. Anything the detector scores above this is skill.
        nulls = []
        for _ in range(N_NULL_TRIALS):
            rp = []
            while len(rp) < len(pts):
                xs = rng.uniform(minx, maxx, len(pts))
                ys = rng.uniform(miny, maxy, len(pts))
                rp.extend(Point(a, b) for a, b in zip(xs, ys)
                          if region.contains(Point(a, b)))
            rp = rp[:len(pts)]
            rt = STRtree(rp)
            nf = sum(1 for m in marks
                     if any(rp[int(j)].distance(m) <= MATCH_M
                            for j in rt.query(m.buffer(MATCH_M))))
            nulls.append(nf / len(marks) * 100)
        null = float(np.median(nulls))

        print(f"{label:16s}{len(pts):10d}{recall:7.0f}%{null:6.0f}%"
              f"{prec:6.0f}%{f1:6.0f}%")

    print("\nrecall  = your rocks we found.   null = what random scatter scores.")
    print("prec    = our detections that are actually one of your rocks.")
    print("Precision is the number that was never measurable before; a detector")
    print("cannot inflate it by flagging more.")


if __name__ == "__main__":
    main()
