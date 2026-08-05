"""Re-select annotator sections by how SURPRISING a rock there would be.

Distance from shore was the wrong proxy and produced the wrong sections. Water
100 m off a straight bank is far from shore and holds no surprises -- you can see
the bank, you know to stay off it. A gap between two islands is close to shore by
that measure and is precisely where a boat gets hurt, because you drive through
it at speed expecting a channel.

What actually makes a rock unexpected is one of two things:

  A SHALLOW SPOT IN DEEP WATER. A hump that rises out of otherwise deep water,
  with no bank nearby to warn you. Scored from the 1954 surface as the drop
  between the depth around a section and the shallowest point inside it. This is
  the classic mid-lake shoal.

  A PASSAGE. Water with land on two or more sides within a few hundred metres --
  a gap between islands, or between an island and the mainland. Boats funnel
  through these, and islands are the tops of ridges, so the ridge usually
  continues underwater between them. Scored by counting distinct landmasses
  near the section.

Islands come from the lake polygon's interior rings, so "land" means the
mainland bank and every island equally.

Chips are already rendered for every section; this only re-picks and re-orders
them, so it costs seconds and no network. Marks in localStorage survive because
section ids do not change.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box, shape
from shapely.ops import transform as shp_transform, unary_union
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_contours import build_surface
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "annotate"

RING_M = 900.0        # neighbourhood the section is compared against
GAP_RADIUS_M = 300.0  # how far to look for separate landmasses
MIN_DEEP_AROUND = 8.0 # ft; below this the whole area is shallow and nothing stands out
TOP_N = int(os.environ.get("SHOALRUN_TOP_N", "120"))


def main():
    meta = json.loads((OUT / "sections.json").read_text())
    sec_m = meta["section_m"]
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake_ll = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
    lake = shp_transform(lambda x, y: fwd.transform(x, y), lake_ll)

    # Land = mainland bank + every island, treated identically. Islands are the
    # polygon's interior rings.
    lands = [Polygon(lake.exterior).boundary]
    islands = [Polygon(r) for r in lake.interiors]
    lands += [g.boundary for g in islands]
    land_tree = STRtree(lands)
    print(f"{len(islands)} islands + mainland")

    grid, gx, gy, _, _, _ = build_surface(verbose=False)

    def depths_in(geom):
        minx, miny, maxx, maxy = geom.bounds
        i0, i1 = np.searchsorted(gx, minx), np.searchsorted(gx, maxx)
        j0, j1 = np.searchsorted(gy, miny), np.searchsorted(gy, maxy)
        sub = grid[max(j0 - 1, 0):j1 + 1, max(i0 - 1, 0):i1 + 1]
        return sub[np.isfinite(sub)]

    rows = []
    for s in meta["sections"]:
        ox, oy = s["origin"]
        b = box(ox, oy - sec_m, ox + sec_m, oy)
        c = b.centroid

        d_in = depths_in(b)
        d_ring = depths_in(c.buffer(RING_M))
        if d_in.size < 4 or d_ring.size < 20:
            continue
        around = float(np.median(d_ring))
        shallowest = float(np.percentile(d_in, 5))
        # A rise is only a surprise if the water around it is deep enough that
        # you would not already be going slow.
        rise = (around - shallowest) if around >= MIN_DEEP_AROUND else 0.0

        # Distinct landmasses within reach. Two or more means a passage.
        near = {int(j) for j in land_tree.query(c.buffer(GAP_RADIUS_M))
                if lands[int(j)].distance(c) <= GAP_RADIUS_M}
        n_land = len(near)
        # A passage only counts where the water leading into it is deep. In a
        # bay that is 1 ft deep everywhere, an island gap is not a surprise --
        # you are already crawling. Without this the shallow flats at the top of
        # the lake outranked genuine 40-ft-to-3-ft humps.
        passage = n_land >= 2 and around >= MIN_DEEP_AROUND

        # Blend: a mid-lake rise and an island gap are both wanted, and a
        # section that is BOTH is the best square on the lake to check.
        score = rise + (12.0 if passage else 0.0) + 4.0 * max(0, n_land - 2 if passage else 0)
        if score <= 0:
            continue
        rows.append({
            **s,
            "around_ft": round(around, 1),
            "shallowest_ft": round(shallowest, 1),
            "rise_ft": round(rise, 1),
            "n_land": n_land,
            "why": ("gap between land" if passage and rise < 5 else
                    "gap + shallow rise" if passage else
                    "shallow spot in deep water"),
            "score": round(score, 1),
        })

    rows.sort(key=lambda r: -r["score"])
    rows = rows[:TOP_N]
    sub = dict(meta)
    sub["sections"] = rows

    tpl = (ROOT / "web" / "annotate.template.html").read_text()
    (OUT / "index.html").write_text(tpl.replace("/*DATA*/", json.dumps(sub)))

    kinds = {}
    for r in rows:
        kinds[r["why"]] = kinds.get(r["why"], 0) + 1
    print(f"\n{len(rows)} sections selected, ordered most surprising first")
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"  {v:4d}  {k}")
    print(f"\ntop 10:")
    for r in rows[:10]:
        print(f"  #{r['id']:<4} {r['why']:28s} around {r['around_ft']:5.1f} ft, "
              f"shallowest {r['shallowest_ft']:5.1f} ft, {r['n_land']} landmasses near")
    print(f"\nrewrote {OUT / 'index.html'} -- chips reused, marks preserved")


if __name__ == "__main__":
    main()
