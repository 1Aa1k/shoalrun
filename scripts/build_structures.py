"""Shoreline buildings, camps and piers as orientation landmarks.

4,568 hazard dots on an empty shoreline are hard to place. Nate's friend reads
the lake by its camps -- Baxter House, Pamola House, the Icehouse, the boathouse.
Drawing those turns the map from an abstract hazard cloud into somewhere he
recognises, which is also what makes it possible for him to say "that rock is in
the wrong spot" -- feedback that needs a shared frame of reference.

Buildings are rendered as footprints, piers as lines. Names are kept where OSM
has them.

OSM is not the whole shoreline. It has 271 structures here and Maine E911 has
144 addressed ones within 400 m of the water, 39 of which are more than 75 m
from anything OSM traced -- whole roads (Evergreen Way, Rolands Way, Beech Lane)
with nothing on them. Both lidar and Microsoft's imagery model missed the same
camps, because the shoreline is closed spruce canopy and a roof under it is
invisible from above. E911 is not a detection: it is the address a dispatcher
sends an ambulance to, so it exists whether or not the camp can be seen.

Those are merged in as points, kind `address`, deduped against the OSM
footprints. They carry `source` so a point that is one town's address record is
never mistaken for a traced outline.
"""

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "osm_structures.json"
E911 = ROOT / "data" / "structures_e911.geojson"
OUT = ROOT / "data" / "structures.geojson"

# An E911 point this close to an OSM footprint is the same building. The points
# are placed by eye off aerial imagery and land 20-40 m from the roof often
# enough that a tighter radius would double-draw half the shoreline.
DEDUPE_M = 40.0
M_PER_DEG_LAT = 111_320.0


def _centroid(geom):
    """Rough centre of any of the three geometry types built above.

    Rough is the right word here: this feeds a 40 m dedupe test, and the exact
    centroid of a 10 m camp is not what decides it.
    """
    g = geom["type"]
    if g == "Point":
        return geom["coordinates"]
    ring = geom["coordinates"][0] if g == "Polygon" else geom["coordinates"]
    # A closed ring repeats its first vertex, which would weight that corner
    # double and drag the centre a metre or two towards it.
    if len(ring) > 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    n = len(ring)
    return [sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n]


def e911_points(osm_feats, path=E911, dedupe_m=DEDUPE_M):
    """Addressed structures E911 knows about and OSM does not."""
    if not path.exists():
        print("no E911 file; run scripts/fetch_e911.py")
        return []
    gj = json.loads(path.read_text())
    known = [_centroid(f["geometry"]) for f in osm_feats if f.get("geometry")]
    if not known:
        return []
    lat_mid = sum(p[1] for p in known) / len(known)
    dlat = dedupe_m / M_PER_DEG_LAT
    dlon = dlat / math.cos(math.radians(lat_mid))

    out = []
    for f in gj.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        # Compared in degrees scaled per axis, so the test is a true circle in
        # metres rather than one squashed by the latitude.
        if any(((lon - kx) / dlon) ** 2 + ((lat - ky) / dlat) ** 2 <= 1.0
               for kx, ky in known):
            continue
        p = f["properties"]
        props = {"kind": "address", "source": "maine-e911"}
        if p.get("ADDRESS"):
            props["name"] = p["ADDRESS"]
        out.append({"type": "Feature", "properties": props,
                    "geometry": {"type": "Point",
                                 "coordinates": [round(lon, 6), round(lat, 6)]}})
    print(f"E911: {len(gj.get('features', []))} on the lake, {len(out)} not "
          f"within {dedupe_m:.0f} m of an OSM structure")
    return out


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

    feats += e911_points(feats)

    import collections
    print(f"{len(feats)} structures: {dict(collections.Counter(f['properties']['kind'] for f in feats))}")
    print(f"named: {sum(1 for f in feats if 'name' in f['properties'])}")
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
