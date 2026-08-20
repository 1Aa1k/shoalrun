#!/usr/bin/env python3
"""Does lidar-above-water actually find the rocks people already know about?

`detect_lidar.py` returns 1,967 objects standing above the lake surface. That
number means nothing on its own: a detector that flags enough blobs will land
within 30 m of any point you name. The question is whether it beats chance, and
the answer depends entirely on which chance you compare it to.

Two nulls, because the first one is not honest by itself:

  uniform      N points scattered anywhere in the lake. This is the control
               `recall_check.py` uses, and it is the one that made naip-1m look
               like it worked (97% vs 56%).

  shore-matched  N points drawn to match the DETECTIONS' own distribution of
               distance from shore. Rocks hug the shoreline -- the confirmed
               ones on this lake sit a median 2 m from it -- and so does
               anything that returns lidar near the waterline. A uniform null
               spreads its points over open water where no reference rock is,
               so it understates chance and hands the detector free lift it did
               not earn. If the detector only knows "rocks are near shore", this
               is the control that says so.

The lake-stage work died on exactly this failure: a sweep that looked like a
robustness check but moved one cutoff across all flights together, so a
per-flight bias slid through untouched. The control has to be able to catch the
error, or it is decoration.

Also scored: the 137 `open_water` marks in hazards.geojson, which are places
somebody looked and found nothing. A detector that fires there is finding waves.

    .venv/bin/python scripts/score_lidar.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Same 30 m as recall_check.py, and for the same reason: a hand-drawn OSM
# polygon's centroid and a 2 m blob's centroid will never coincide, and this is
# measuring gross misses rather than registration.
MATCH_M = 30.0

# Enough trials that the median is stable, few enough to run in seconds.
TRIALS = 21

# Pool the nulls draw from. Big enough that the shore-matched sampler can fill
# even its thinnest distance bin without replacement fatigue.
POOL = 300_000


def _fwd():
    crs = lake_crs()
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def load_lake(fwd):
    poly = shape(json.loads((DATA / "lake.geojson").read_text())["geometry"])
    return shp_transform(lambda x, y: fwd.transform(x, y), poly)


def load_points(path: Path, fwd, keep=None) -> np.ndarray:
    feats = json.loads(path.read_text())["features"]
    out = []
    for f in feats:
        p = f["properties"]
        if keep and not keep(p):
            continue
        lon = p.get("lon")
        lat = p.get("lat")
        if lon is None or lat is None:
            lon, lat = f["geometry"]["coordinates"][:2]
        out.append(fwd.transform(lon, lat))
    return np.array(out, dtype=float).reshape(-1, 2)


def osm_rocks(fwd, lake) -> np.ndarray:
    raw = json.loads((DATA / "osm_rocks_raw.json").read_text())
    out = []
    for e in raw["elements"]:
        if "geometry" in e:
            pts = [(p["lon"], p["lat"]) for p in e["geometry"]]
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
        elif "lat" in e:
            lon, lat = e["lon"], e["lat"]
        else:
            continue
        xy = fwd.transform(lon, lat)
        if lake.buffer(50).contains(Point(xy)):
            out.append(xy)
    return np.array(out, dtype=float).reshape(-1, 2)


def state_buoys(fwd, lake) -> np.ndarray:
    path = DATA / "buoys_by_lake.kmz"
    if not path.exists():
        return np.empty((0, 2))
    text = zipfile.ZipFile(path).read("doc.kml").decode("utf-8", "replace")
    out = []
    for p in text.split("<Placemark")[1:]:
        m = re.search(r"<coordinates>\s*([-\d.]+),([-\d.]+)", p)
        if not m:
            continue
        xy = fwd.transform(float(m.group(1)), float(m.group(2)))
        # Geometry decides which lake a buoy is on; the state file's own
        # lake-name field attributes these to the adjacent chain.
        if lake.buffer(30).contains(Point(xy)):
            out.append(xy)
    return np.array(out, dtype=float).reshape(-1, 2)


def nearest(refs: np.ndarray, dets: np.ndarray) -> np.ndarray:
    """Distance from every ref to its closest detection."""
    if len(refs) == 0 or len(dets) == 0:
        return np.full(len(refs), np.inf)
    tree = shapely.STRtree(shapely.points(dets))
    idx = tree.nearest(shapely.points(refs))
    return np.hypot(refs[:, 0] - dets[idx, 0], refs[:, 1] - dets[idx, 1])


def recall(refs: np.ndarray, dets: np.ndarray, match_m=MATCH_M) -> float:
    if len(refs) == 0:
        return float("nan")
    return float((nearest(refs, dets) <= match_m).mean() * 100)


def build_pool(lake, rng, n=POOL):
    """Random points inside the lake, with each one's distance from shore.

    Distance is to the polygon BOUNDARY, which includes the 74 island rings --
    a point 5 m off an island is 5 m from shore, and treating only the outer
    ring as shore would call it open water.
    """
    minx, miny, maxx, maxy = lake.bounds
    # Prepared geometry builds the spatial index once; without it contains_xy
    # walks all 75 rings for every one of the 300k candidates.
    shapely.prepare(lake)
    pts = []
    while sum(len(p) for p in pts) < n:
        xs = rng.uniform(minx, maxx, n)
        ys = rng.uniform(miny, maxy, n)
        cand = np.column_stack([xs, ys])
        inside = shapely.contains_xy(lake, cand[:, 0], cand[:, 1])
        pts.append(cand[inside])
    pool = np.vstack(pts)[:n]
    dist = shapely.distance(shapely.points(pool), lake.boundary)
    return pool, dist


def shore_dist(pts: np.ndarray, lake) -> np.ndarray:
    if len(pts) == 0:
        return np.empty(0)
    return shapely.distance(shapely.points(pts), lake.boundary)


def matched_sample(pool, pool_dist, target_dist, rng):
    """Draw len(target_dist) pool points whose shore distances match the target.

    Binned in log space because the distribution is not remotely uniform: most
    detections are within a few metres of the line and a handful are hundreds of
    metres out, and linear bins would put 95% of them in one bucket and match
    nothing.
    """
    edges = np.concatenate([[-1], np.geomspace(1, max(target_dist.max(), 2), 24)])
    want = np.histogram(target_dist, bins=edges)[0]
    pool_bin = np.digitize(pool_dist, edges) - 1
    out = []
    for b, k in enumerate(want):
        if k == 0:
            continue
        avail = np.flatnonzero(pool_bin == b)
        if len(avail) == 0:
            # No random point ever lands this far from shore -- which is itself
            # the finding, so borrow the nearest populated bin rather than
            # silently returning a short sample.
            for step in range(1, len(want)):
                for nb in (b - step, b + step):
                    if 0 <= nb < len(want):
                        avail = np.flatnonzero(pool_bin == nb)
                        if len(avail):
                            break
                if len(avail):
                    break
        if len(avail) == 0:
            continue
        out.append(pool[rng.choice(avail, size=int(k), replace=True)])
    return np.vstack(out) if out else np.empty((0, 2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detections", type=Path, default=DATA / "rocks_lidar.geojson")
    ap.add_argument("--match-m", type=float, default=MATCH_M)
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    fwd = _fwd()
    lake = load_lake(fwd)

    dets_all = load_points(args.detections, fwd)
    dets_rock = load_points(args.detections, fwd, keep=lambda p: p.get("class") == "rock")
    refs_osm = osm_rocks(fwd, lake)
    refs_buoy = state_buoys(fwd, lake)
    refs = np.vstack([refs_osm, refs_buoy])

    neg = load_points(DATA / "hazards.geojson", fwd,
                      keep=lambda p: p.get("status") == "open_water")

    print(f"detections {len(dets_all)} ({len(dets_rock)} rock, "
          f"{len(dets_all) - len(dets_rock)} island)")
    print(f"reference rocks {len(refs)} (OSM {len(refs_osm)}, buoys {len(refs_buoy)})")
    print(f"open-water negatives {len(neg)}")

    print("\nbuilding null pool...")
    pool, pool_dist = build_pool(lake, rng)
    det_dist = shore_dist(dets_rock, lake)
    ref_dist = shore_dist(refs, lake)
    print(f"  median distance from shore -- detections {np.median(det_dist):6.1f} m, "
          f"references {np.median(ref_dist):6.1f} m, random {np.median(pool_dist):6.1f} m")

    n = len(dets_rock)
    uni, matched = [], []
    for t in range(args.trials):
        r = np.random.default_rng(args.seed + t)
        uni.append(recall(refs, pool[r.choice(len(pool), n, replace=False)], args.match_m))
        matched.append(recall(refs, matched_sample(pool, pool_dist, det_dist, r), args.match_m))

    real = recall(refs, dets_rock, args.match_m)
    u = float(np.median(uni))
    m = float(np.median(matched))

    print(f"\nRECALL at {args.match_m:.0f} m, {n} detections, {args.trials} trials")
    print(f"  lidar rock                {real:6.1f}%")
    print(f"  uniform null              {u:6.1f}%   lift {real - u:+6.1f}%")
    print(f"  shore-matched null        {m:6.1f}%   lift {real - m:+6.1f}%")

    # The negative control. Firing here is firing on water somebody has looked at.
    fp = recall(neg, dets_rock, args.match_m)
    fp_m = float(np.median([
        recall(neg, matched_sample(pool, pool_dist, det_dist, np.random.default_rng(args.seed + t)),
               args.match_m)
        for t in range(args.trials)
    ]))
    print(f"\nOPEN-WATER negatives ({len(neg)}), detector should stay quiet")
    print(f"  lidar rock fires on       {fp:6.1f}%")
    print(f"  shore-matched null fires  {fp_m:6.1f}%")

    d = nearest(refs, dets_rock)
    hit = d[d <= args.match_m]
    print(f"\nmatched {len(hit)}/{len(refs)}, median miss distance "
          f"{np.median(hit) if len(hit) else float('nan'):.1f} m")
    print(f"unmatched references: {int((d > args.match_m).sum())}")


if __name__ == "__main__":
    main()
