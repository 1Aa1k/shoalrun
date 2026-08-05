"""Measure recall against independently mapped rocks.

Every check so far has been about PRECISION -- is what the detector found real?
None measured RECALL -- of the rocks that are actually out there, how many did it
find? Nate says the map is missing rocks he knows, including ones marked with
buoys, and precision metrics are structurally incapable of catching that. A
detector that finds three rocks and gets all three right scores 100% precision
and is still useless.

Two independent reference sets, neither derived from any imagery we processed:

  OSM rocks     32 features (reef, bare_rock, and two tagged seamark obstruction
                / foul_ground) hand-mapped by people who know this lake.
  State buoys   Maine DACF navigational aids. ~2,500 statewide, with explicit
                Rocks and Hazard Area types.

Recall against these is the number that actually answers Nate's question.
"""

import json
import re
import sys
import zipfile
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# How close a detection must be to count as finding a reference rock. Generous:
# a hand-drawn OSM polygon's centroid and a detected blob's centroid will not
# coincide, and the whole point is to measure gross misses, not registration.
MATCH_M = 30.0


def osm_rocks(fwd):
    raw = json.loads((DATA / "osm_rocks_raw.json").read_text())
    out = []
    for e in raw["elements"]:
        tags = e.get("tags", {})
        if "geometry" in e:
            pts = [(p["lon"], p["lat"]) for p in e["geometry"]]
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
        elif "lat" in e:
            lon, lat = e["lon"], e["lat"]
        else:
            continue
        out.append({
            "lon": lon, "lat": lat,
            "kind": tags.get("natural", "seamark"),
            "seamark": any(k.startswith("seamark") for k in tags),
            "geom": shp_transform(lambda x, y: fwd.transform(x, y), Point(lon, lat)),
        })
    return out


def state_buoys(fwd, lake):
    path = DATA / "buoys_by_lake.kmz"
    if not path.exists():
        return []
    t = zipfile.ZipFile(path).read("doc.kml").decode("utf-8", "replace")
    out = []
    for p in t.split("<Placemark")[1:]:
        m = re.search(r"<coordinates>\s*([-\d.]+),([-\d.]+)", p)
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        g = shp_transform(lambda x, y: fwd.transform(x, y), Point(lon, lat))
        # Only buoys actually on this lake, judged geometrically -- the state
        # file's own lake-name field attributes these to the adjacent chain.
        if not lake.buffer(30).contains(g):
            continue
        name = re.search(r"<name>(.*?)</name>", p, re.S)
        lk = re.search(r"Lake Name:</td><td>(.*?)</td>", p, re.S)
        out.append({
            "lon": lon, "lat": lat,
            "kind": (name.group(1) if name else "").strip().replace("&amp;", "&"),
            "lake_field": (lk.group(1) if lk else "").strip(),
            "geom": g,
        })
    return out


def load_detections(fwd):
    """Every detection layer that currently exists, keyed by name."""
    layers = {}
    for label, fn in (
        ("sentinel-10m", "verified.geojson"),
        ("naip-1m", "rocks_naip.geojson"),
        ("naip-0.3m", "rocks_naip_03.geojson"),
        ("naip-bright-1m", "rocks_bright.geojson"),
    ):
        path = DATA / fn
        if not path.exists():
            continue
        feats = json.loads(path.read_text())["features"]
        pts = []
        for f in feats:
            p = f["properties"]
            pts.append(shp_transform(lambda x, y: fwd.transform(x, y), Point(p["lon"], p["lat"])))
        layers[label] = pts
    return layers


def null_recall(refs, n_det, lake, trials=5, seed=11):
    """Recall a detector would score by scattering n_det points at random.

    Without this, a high recall number proves nothing. A detector that flags
    12,000 blobs on a 34 km2 lake puts something within 30 m of almost any point
    you name, so it will "find" the reference rocks whether or not it can see
    them. The honest score is recall MINUS this baseline. It is also why
    detection count is not a figure of merit: raising the count raises the
    baseline just as fast.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    minx, miny, maxx, maxy = lake.bounds
    scores = []
    for _ in range(trials):
        pts = []
        while len(pts) < n_det:
            xs = rng.uniform(minx, maxx, n_det)
            ys = rng.uniform(miny, maxy, n_det)
            pts.extend((x, y) for x, y in zip(xs, ys) if lake.contains(Point(x, y)))
        arr = np.array(pts[:n_det])
        hit = 0
        for r in refs:
            g = r["geom"]
            d = np.hypot(arr[:, 0] - g.x, arr[:, 1] - g.y)
            if d.min() <= MATCH_M:
                hit += 1
        scores.append(hit / len(refs) * 100)
    return float(sorted(scores)[len(scores) // 2])


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(
        lambda x, y: fwd.transform(x, y),
        shape(json.loads((DATA / "lake.geojson").read_text())["geometry"]),
    )

    refs = []
    for r in osm_rocks(fwd):
        if lake.buffer(50).contains(r["geom"]):
            r["source"] = "osm"
            refs.append(r)
    for b in state_buoys(fwd, lake):
        b["source"] = "state_buoy"
        refs.append(b)

    n_osm = sum(1 for r in refs if r["source"] == "osm")
    n_buoy = sum(1 for r in refs if r["source"] == "state_buoy")
    print(f"reference rocks on this lake: {len(refs)}  (OSM {n_osm}, state buoys {n_buoy})")
    if n_buoy:
        fields = {r.get("lake_field") for r in refs if r["source"] == "state_buoy"}
        print(f"  state buoy lake-name field says: {fields}")

    layers = load_detections(fwd)
    if not layers:
        raise SystemExit("no detection layers found yet")

    print(f"\nRECALL at {MATCH_M:.0f} m, against a same-size random null:\n")
    print(f"{'layer':16s} {'detections':>10s} {'recall':>8s} {'random':>8s} {'lift':>7s} {'err_m':>7s}")
    for label, pts in layers.items():
        found, errs = 0, []
        for r in refs:
            d = min((p.distance(r["geom"]) for p in pts), default=1e9)
            if d <= MATCH_M:
                found += 1
                errs.append(d)
        null = null_recall(refs, len(pts), lake)
        recall = found / len(refs) * 100
        err = sorted(errs)[len(errs) // 2] if errs else float("nan")
        print(f"{label:16s} {len(pts):10d} {recall:7.0f}% {null:7.0f}% "
              f"{recall - null:+6.0f}% {err:7.1f}")

    # Detail the misses for the best layer, so they can be looked at directly.
    best = max(layers.items(), key=lambda kv: sum(
        1 for r in refs if any(p.distance(r["geom"]) <= MATCH_M for p in kv[1])))
    label, pts = best
    misses = [r for r in refs if not any(p.distance(r["geom"]) <= MATCH_M for p in pts)]
    print(f"\nmissed by {label}: {len(misses)}")
    for r in misses[:20]:
        print(f"  {r['lat']:.5f}, {r['lon']:.5f}  {r['source']:11s} {r['kind']}")

    (DATA / "reference_rocks.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {k: v for k, v in r.items() if k != "geom"},
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        } for r in refs],
    }))
    print(f"\nwrote {DATA / 'reference_rocks.geojson'}")


if __name__ == "__main__":
    main()
