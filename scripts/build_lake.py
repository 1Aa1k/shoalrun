"""Assemble the OSM multipolygon relation for Millinocket Lake into clean GeoJSON.

OSM hands back a relation as an unordered bag of open ways tagged outer/inner.
They have to be stitched end-to-end into closed rings before shapely will take
them. Islands (inner rings) matter here: they are shoreline too, and shoreline
is where the rocks are.
"""

import json
from pathlib import Path

from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "lake_raw.json"
OUT = ROOT / "data" / "lake.geojson"

# Endpoints within this many degrees are treated as the same node. OSM ways in a
# relation share exact node coords, so this only absorbs float round-tripping.
SNAP = 1e-9


def _key(pt):
    return (round(pt["lon"] / SNAP), round(pt["lat"] / SNAP))


def stitch(ways):
    """Join open ways into closed rings. Returns list of coordinate rings."""
    remaining = [list(w) for w in ways if w]
    rings = []

    while remaining:
        chain = remaining.pop()
        progressed = True
        while progressed and _key(chain[0]) != _key(chain[-1]):
            progressed = False
            for i, cand in enumerate(remaining):
                if _key(cand[0]) == _key(chain[-1]):
                    chain.extend(cand[1:])
                elif _key(cand[-1]) == _key(chain[-1]):
                    chain.extend(reversed(cand[:-1]))
                elif _key(cand[-1]) == _key(chain[0]):
                    chain = cand[:-1] + chain
                elif _key(cand[0]) == _key(chain[0]):
                    chain = list(reversed(cand[1:])) + chain
                else:
                    continue
                remaining.pop(i)
                progressed = True
                break

        if _key(chain[0]) != _key(chain[-1]):
            # An unclosed chain means the relation is broken upstream. Skip it
            # rather than silently closing a gap that may be kilometres wide.
            print(f"  warning: dropped unclosed chain of {len(chain)} pts")
            continue
        rings.append([(p["lon"], p["lat"]) for p in chain])

    return rings


def main():
    rel = json.loads(RAW.read_text())["elements"][0]

    outer_ways = [m["geometry"] for m in rel["members"] if m["role"] == "outer"]
    inner_ways = [m["geometry"] for m in rel["members"] if m["role"] == "inner"]

    outers = stitch(outer_ways)
    inners = stitch(inner_ways)
    print(f"outer rings: {len(outers)}  inner rings (islands): {len(inners)}")

    # Assign each island to whichever outer ring contains it.
    shells = [Polygon(r) for r in outers]
    holes = [Polygon(r) for r in inners]
    polys = []
    for shell in shells:
        contained = [h.exterior.coords for h in holes if shell.contains(h.representative_point())]
        polys.append(Polygon(shell.exterior.coords, contained))

    lake = unary_union(polys)

    # Metric area via an equal-area projection centred on the lake.
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    area_m2 = abs(geod.geometry_area_perimeter(lake)[0])
    perim_m = geod.geometry_area_perimeter(lake)[1]

    print(f"area: {area_m2 / 1e6:.2f} km^2   shoreline: {abs(perim_m) / 1000:.1f} km")
    print(f"islands kept: {sum(len(p.interiors) for p in getattr(lake, 'geoms', [lake]))}")

    OUT.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "name": "Millinocket Lake",
                    "osm_relation": rel["id"],
                    "area_km2": round(area_m2 / 1e6, 3),
                    "shoreline_km": round(abs(perim_m) / 1000, 2),
                    "source": "OpenStreetMap (ODbL)",
                },
                "geometry": mapping(lake),
            }
        )
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
