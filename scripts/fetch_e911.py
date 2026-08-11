#!/usr/bin/env python3
"""Pull Maine E911 address points around the lake.

Lidar and imagery both failed to find the camps (see
docs/handoffs/ and `detect_structures.py`): the shoreline here is under closed
spruce canopy, so a roof is invisible from above and its height-above-ground is
indistinguishable from the tree standing next to it. Microsoft's model found 104
footprints and only 3 that OSM did not already have.

E911 is the other kind of evidence entirely. It is not a detection -- it is the
address a person will read out to a dispatcher, maintained by the town because
an ambulance has to find the driveway. A camp with no road, no clearing and no
visible roof still has one. That is exactly the population the remote sensing
was blind to.

Points are the structure location, not the parcel centroid or the driveway end
(PLACEMENT says which), so they can be compared straight against OSM building
centroids.

    .venv/bin/python scripts/fetch_e911.py

Credit: Maine Office of GIS / Maine E911, public data.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
OSM = ROOT / "data" / "structures.geojson"
OUT = ROOT / "data" / "structures_e911.geojson"

SERVICE = ("https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services/"
           "Maine_E911_Addresses_Feature/FeatureServer/0")

# How far inland from the water an address still counts as a lake camp. The
# camps sit on the shore; 400 m takes in the ones set back behind a screen of
# trees without reaching the Golden Road or the town.
NEAR_M = 400.0

# The service pages at 2000. Ask for less than that per page so a raised server
# limit cannot silently truncate a page and look like the end of the data.
PAGE = 1000
MAX_PAGES = 50          # 50,000 points is far past anything this bbox holds

M_PER_DEG_LAT = 111_320.0

# Fields worth carrying. The layer has 84; the rest are dispatch plumbing.
FIELDS = ["ADDRESS", "ST_FULLNAME", "TOWN", "PLACE_TYPE", "PLACEMENT",
          "BUILDING", "UNIT", "LANDMARK", "ESITEID"]


def _get(url: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=120) as fh:
        return json.loads(fh.read())


def fetch_bbox(bounds: tuple[float, float, float, float],
               service: str = SERVICE, verbose: bool = True) -> list[dict]:
    """Every address point in the bounds, paged.

    `exceededTransferLimit` is the server saying "there is more"; trusting the
    returned count instead would quietly stop at whatever the page size is.
    """
    west, south, east, north = bounds
    base = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(FIELDS),
        "returnGeometry": "true",
        "f": "geojson",
    }
    feats: list[dict] = []
    for page in range(MAX_PAGES):
        d = _get(f"{service}/query",
                 {**base, "resultOffset": page * PAGE, "resultRecordCount": PAGE})
        if "error" in d:
            raise SystemExit(f"service error: {d['error']}")
        got = d.get("features", [])
        feats.extend(got)
        if verbose:
            print(f"  page {page + 1}: {len(got)} points ({len(feats)} total)")
        if not d.get("properties", {}).get("exceededTransferLimit") and len(got) < PAGE:
            break
    else:
        raise SystemExit(f"hit the {MAX_PAGES}-page cap -- shrink the bbox")
    return feats


def lake_geom():
    gj = json.loads(LAKE.read_text())
    g = gj["features"][0]["geometry"] if gj.get("type") == "FeatureCollection" else gj
    return shape(g)


def osm_centroids() -> list:
    if not OSM.exists():
        return []
    gj = json.loads(OSM.read_text())
    return [shape(f["geometry"]).centroid for f in gj.get("features", []) if f.get("geometry")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--near-m", type=float, default=NEAR_M)
    ap.add_argument("--match-m", type=float, default=25.0,
                    help="how close an address must be to an OSM building to "
                         "count as already mapped")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    lake = lake_geom()
    west, south, east, north = lake.bounds
    lat_mid = (south + north) / 2
    dlat = 1.0 / M_PER_DEG_LAT
    dlon = dlat / math.cos(math.radians(lat_mid))

    pad = args.near_m * 1.5           # fetch wider than we keep, then measure
    print(f"fetching E911 within {pad:.0f} m of the lake's bounds")
    feats = fetch_bbox((west - pad * dlon, south - pad * dlat,
                        east + pad * dlon, north + pad * dlat))

    # Distance to the lake, done in degrees scaled to metres at this latitude.
    # Projecting for a 400 m threshold at 45.75 N is precision nobody needs.
    near = []
    for f in feats:
        p = shape(f["geometry"])
        # Degrees of latitude are the smaller unit here, so treating the
        # distance as latitude degrees overstates it and errs towards keeping a
        # borderline camp rather than dropping it.
        d_m = lake.distance(p) * M_PER_DEG_LAT
        if d_m <= args.near_m:
            f["_dist_m"] = round(d_m)
            near.append(f)
    print(f"  {len(near)} of {len(feats)} within {args.near_m:.0f} m of the water")

    known = osm_centroids()
    tree = STRtree(known) if known else None
    tol = args.match_m * dlat         # degrees; longitude is tighter, so this is
                                      # the generous direction and over-matches
    new = 0
    for f in near:
        p = shape(f["geometry"])
        hit = False
        if tree is not None:
            for i in tree.query(p.buffer(tol)):
                if p.distance(known[i]) <= tol:
                    hit = True
                    break
        f["_in_osm"] = hit
        new += not hit

    gj = {
        "type": "FeatureCollection",
        "meta": {
            "source": "Maine E911 NG addresses (Maine Office of GIS)",
            "url": SERVICE,
            "note": "address points, not footprints; one per addressable "
                    "structure or unit",
        },
        "features": [
            {"type": "Feature",
             "properties": {
                 "kind": "building",
                 "source": "maine-e911",
                 "in_osm": f["_in_osm"],
                 "dist_m": f["_dist_m"],
                 **{k: f["properties"].get(k) for k in FIELDS},
             },
             "geometry": f["geometry"]}
            for f in near
        ],
    }
    args.out.write_text(json.dumps(gj))

    print(f"wrote {args.out}")
    print(f"  {len(near)} addresses on the lake, {new} not within "
          f"{args.match_m:.0f} m of an OSM building")
    print(f"  OSM has {len(known)} structures to compare against")

    kinds: dict[str, int] = {}
    for f in near:
        kinds[f["properties"].get("PLACE_TYPE") or "(none)"] = \
            kinds.get(f["properties"].get("PLACE_TYPE") or "(none)", 0) + 1
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {v:4d}  {k}")


if __name__ == "__main__":
    main()
