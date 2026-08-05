"""Shoreline buildings, camps and piers as orientation landmarks.

4,568 hazard dots on an empty shoreline are hard to place. Nate's friend reads
the lake by its camps -- Baxter House, Pamola House, the Icehouse, the boathouse.
Drawing those turns the map from an abstract hazard cloud into somewhere he
recognises, which is also what makes it possible for him to say "that rock is in
the wrong spot" -- feedback that needs a shared frame of reference.

Buildings are rendered as footprints, piers as lines. Names are kept where OSM
has them.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "osm_structures.json"
OUT = ROOT / "data" / "structures.geojson"


def main():
    els = json.loads(RAW.read_text())["elements"]
    feats = []
    for e in els:
        tags = e.get("tags", {})
        if "building" in tags:
            kind = "building"
        elif tags.get("man_made") == "pier":
            kind = "pier"
        elif tags.get("leisure") == "slipway":
            kind = "slipway"
        elif "tourism" in tags:
            kind = "camp"
        else:
            kind = "place"

        name = tags.get("name")
        if e["type"] == "way" and "geometry" in e:
            coords = [[round(p["lon"], 6), round(p["lat"], 6)] for p in e["geometry"]]
            closed = kind in ("building",) and len(coords) > 3
            geom = {"type": "Polygon", "coordinates": [coords]} if closed else \
                   {"type": "LineString", "coordinates": coords}
        else:
            c = e.get("center") or e
            if "lat" not in c:
                continue
            geom = {"type": "Point", "coordinates": [round(c["lon"], 6), round(c["lat"], 6)]}

        feats.append({"type": "Feature",
                      "properties": {"kind": kind, **({"name": name} if name else {})},
                      "geometry": geom})

    import collections
    print(f"{len(feats)} structures: {dict(collections.Counter(f['properties']['kind'] for f in feats))}")
    print(f"named: {sum(1 for f in feats if 'name' in f['properties'])}")
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
