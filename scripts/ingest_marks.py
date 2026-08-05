"""Turn Nate's clicked rocks into the reference set every scorer uses.

This is the ground truth the project has been short of. The 32 OSM points were
too few to separate a good detector from a dense one, and the 1954 soundings are
independent but far too coarse to resolve a boulder. Marks made at 0.5 m on
imagery, by someone who knows the lake, are the first reference measured at the
same scale the detector works at.

Reads my_rocks.json from the annotator and writes marked_rocks.geojson.

Two things get recorded beyond the positions themselves, and both matter for
honest scoring:

  sections_reviewed  Which squares were actually looked at. Absence of a mark
                     only means "no rock" inside those; everywhere else it means
                     "not checked". Scoring precision over unreviewed water
                     would count real rocks as false alarms.

  review_area_m2     How much water that covers, so a null model can be drawn
                     from the reviewed region rather than the whole lake.
"""

import json
import sys
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform as shp_transform, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IN = DATA / "my_rocks.json"
OUT = DATA / "marked_rocks.geojson"
REGION = DATA / "reviewed_region.geojson"


def main():
    if not IN.exists():
        raise SystemExit(
            f"no {IN}\n"
            "Open dist/annotate/index.html, mark rocks, press Export, then move\n"
            "the downloaded my_rocks.json into data/."
        )
    d = json.loads(IN.read_text())
    crs = lake_crs()
    back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    feats = []
    for m in d["marks"]:
        lon, lat = back.transform(m["x"], m["y"])
        feats.append({
            "type": "Feature",
            "properties": {
                "source": "nate_marked", "section": m["section"],
                "kind": "rock", "lon": round(lon, 6), "lat": round(lat, 6),
            },
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        })
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    # Reviewed footprint, so scoring can be confined to water actually looked at.
    sec_m = d.get("section_m", 400.0)
    reviewed = set(d.get("sections_reviewed", []))
    meta = json.loads((ROOT / "dist" / "annotate" / "sections.json").read_text())
    boxes = []
    for s in meta["sections"]:
        if s["id"] in reviewed:
            ox, oy = s["origin"]
            boxes.append(box(ox, oy - sec_m, ox + sec_m, oy))
    region = unary_union(boxes) if boxes else None
    if region is not None:
        REGION.write_text(json.dumps({
            "type": "Feature",
            "properties": {"sections": len(boxes), "area_m2": round(region.area, 1)},
            "geometry": mapping(shp_transform(lambda x, y: back.transform(x, y), region)),
        }))

    lake = shp_transform(
        lambda x, y: fwd.transform(x, y),
        shape(json.loads((DATA / "lake.geojson").read_text())["geometry"]),
    )
    print(f"{len(feats)} rocks marked across {len(reviewed)} reviewed sections")
    if region is not None:
        print(f"reviewed area {region.area/1e6:.2f} km2 "
              f"({region.area/lake.area*100:.0f}% of the lake)")
        dens = len(feats) / max(region.area / 1e6, 1e-9)
        print(f"density {dens:.0f} rocks per km2 of reviewed water")
    print(f"\nwrote {OUT}")
    if region is not None:
        print(f"wrote {REGION}")
    print("\nnow: score_marks.py")


if __name__ == "__main__":
    main()
