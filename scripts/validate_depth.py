"""Score detections against the 1954 depth survey -- an INDEPENDENT yardstick.

The recall test tops out because there are only 32 hand-mapped rocks, and at a
30 m match radius a random scatter finds half of them by luck. That is a limit of
the reference set, not of what can be measured.

The soundings are a way around it. They were hand-taken by MDIFW in August 1954
from a boat with a lead line. They share no instrument, no year, and no physics
with a 2011-2023 aerial photograph, so agreement between them cannot be an
artifact of the detector. And the prediction is sharp and falsifiable:

    a rock or shoal must sit in shallow water

A detector that is finding real hazards will place them in the shallows. A
detector that is finding sun glint will place them wherever the glint was, which
has nothing to do with depth. Every detection gets a depth, and the distribution
is compared against random points drawn from the same lake.

Two nulls, because the naive one is too easy to beat:

  uniform     random points anywhere in the lake
  shoreline   random points matched to the SAME distance-from-shore distribution
              as the detections

The second is the one that matters. Shallow water is near shore, so any detector
biased toward the shoreline will look brilliant against a uniform null while
having learned nothing about depth. Beating the shoreline-matched null means the
detections are shallow for a reason other than being near the bank.

Caveats kept in view: 260 soundings over 34.5 km2 is one per 13 hectares, so the
surface between transects is interpolated, and a lake level in 1954 need not
match today's. Both add noise, and noise weakens the signal rather than
manufacturing one -- so a difference that survives this is real.
"""

import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_contours import build_surface
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

LAYERS = (
    ("sentinel-10m", "verified.geojson"),
    ("naip-1m", "rocks_naip.geojson"),
    ("naip-0.3m", "rocks_naip_03.geojson"),
    ("naip-bright-1m", "rocks_bright.geojson"),
    ("merged", "hazards.geojson"),
)

SHALLOW_FT = 10.0
SEED = 23

# Distance from shore beyond which a hazard is genuinely useful. Inside this
# band a boater already knows to be careful, and -- as the whole-lake numbers
# show -- a detector gets credit for shallowness it earned by hugging the bank.
# Offshore is where the claim is falsifiable.
OFFSHORE_M = 50.0


def depth_at(grid, gx, gy, xs, ys):
    """Nearest-cell depth lookup, NaN where outside the interpolated surface."""
    ix = np.clip(np.searchsorted(gx, xs) - 1, 0, len(gx) - 1)
    iy = np.clip(np.searchsorted(gy, ys) - 1, 0, len(gy) - 1)
    return grid[iy, ix]


def sample_matched(shore_dists, lake, shore, rng, n_want, offshore_only=False):
    """Random in-lake points whose distance-from-shore matches `shore_dists`.

    Drawn by rejection: propose uniformly, keep a point only if its shore
    distance falls in a bucket that still has room. This reproduces the
    detections' shoreline bias exactly, so anything left over is about depth.
    """
    edges = np.percentile(shore_dists, np.linspace(0, 100, 11))
    edges[0], edges[-1] = -1e9, 1e9
    quota = np.histogram(shore_dists, bins=edges)[0].astype(float)
    quota *= n_want / quota.sum()
    filled = np.zeros(len(quota))
    minx, miny, maxx, maxy = lake.bounds
    out = []
    guard = 0
    while len(out) < n_want and guard < 400:
        guard += 1
        xs = rng.uniform(minx, maxx, n_want)
        ys = rng.uniform(miny, maxy, n_want)
        for x, y in zip(xs, ys):
            p = Point(x, y)
            if not lake.contains(p):
                continue
            sd = shore.distance(p)
            # The open-ended lowest bucket would otherwise swallow nearshore
            # points the detections never contained, handing the null free
            # shallow water.
            if offshore_only and sd <= OFFSHORE_M:
                continue
            b = int(np.searchsorted(edges, sd) - 1)
            b = min(max(b, 0), len(quota) - 1)
            if filled[b] < quota[b]:
                filled[b] += 1
                out.append((x, y))
                if len(out) >= n_want:
                    break
    return np.array(out) if out else np.zeros((0, 2))


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    grid, gx, gy, lake, _, _ = build_surface(verbose=False)
    shore = lake.boundary
    rng = np.random.default_rng(SEED)

    print(f"depth surface: {np.isfinite(grid).sum():,} cells, "
          f"{np.nanmin(grid):.0f}-{np.nanmax(grid):.0f} ft\n")

    for scope in ("ALL", f"OFFSHORE >{OFFSHORE_M:g} m"):
        print(f"--- {scope} ---")
        print(f"{'layer':16s} {'n':>6s} {'median ft':>10s} {'uniform':>9s} "
              f"{'matched':>9s} {'<10ft':>7s} {'null<10':>8s}")
        run_scope(scope.startswith("OFFSHORE"), grid, gx, gy, lake, shore, fwd, rng)
        print()


def run_scope(offshore_only, grid, gx, gy, lake, shore, fwd, rng):
    for label, fn in LAYERS:
        path = DATA / fn
        if not path.exists():
            continue
        feats = json.loads(path.read_text())["features"]
        pts = [(f["properties"]["lon"], f["properties"]["lat"]) for f in feats
               if f["properties"].get("lon") is not None]
        if not pts:
            continue
        xy = np.array([fwd.transform(lon, lat) for lon, lat in pts])
        if offshore_only:
            keep = np.array([shore.distance(Point(*p)) > OFFSHORE_M for p in xy])
            xy = xy[keep]
            if len(xy) < 10:
                continue
        d = depth_at(grid, gx, gy, xy[:, 0], xy[:, 1])
        d = d[np.isfinite(d)]
        if d.size < 10:
            continue

        # Uniform null, drawn from the same region the detections are drawn from
        # -- offshore water when the scope is offshore, or the comparison is
        # rigged in the detector's favour by including shallow shoreline it was
        # never allowed to use.
        minx, miny, maxx, maxy = lake.bounds
        n_want = min(len(xy), 3000)
        up = []
        while len(up) < n_want:
            rx = rng.uniform(minx, maxx, n_want)
            ry = rng.uniform(miny, maxy, n_want)
            for a, b in zip(rx, ry):
                p = Point(a, b)
                if not lake.contains(p):
                    continue
                if offshore_only and shore.distance(p) <= OFFSHORE_M:
                    continue
                up.append((a, b))
        up = np.array(up[:n_want])
        ud = depth_at(grid, gx, gy, up[:, 0], up[:, 1])
        ud = ud[np.isfinite(ud)]

        # Shoreline-matched null -- the one that can actually falsify us.
        sd_det = np.array([shore.distance(Point(*p)) for p in xy])
        mp = sample_matched(sd_det, lake, shore, rng, min(len(xy), 3000), offshore_only)
        md = depth_at(grid, gx, gy, mp[:, 0], mp[:, 1]) if len(mp) else np.array([])
        md = md[np.isfinite(md)]

        print(f"{label:16s} {len(d):6d} {np.median(d):10.1f} "
              f"{np.median(ud):9.1f} {(np.median(md) if md.size else float('nan')):9.1f} "
              f"{(d < SHALLOW_FT).mean()*100:6.0f}% "
              f"{((md < SHALLOW_FT).mean()*100 if md.size else float('nan')):7.0f}%")

    print("\nmedian ft = depth where the detector put its hazards.")
    print("uniform / matched = same statistic for random points; matched also")
    print("copies the detections' distance-from-shore profile.")
    print("Detections should be SHALLOWER than both. Beating 'matched' is the")
    print("result that means something -- it cannot be explained by hugging shore.")


if __name__ == "__main__":
    main()
