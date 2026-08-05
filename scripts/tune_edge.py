"""Sweep detection thresholds against the depth survey. No network, no imagery.

detect_edge.py did the expensive half once: it wrote per-pixel weighted mean
z-scores for brightness, DoG blob response, gradient edge strength, and NIR.
This script does the cheap half as many times as we like -- pick thresholds,
label pixels, and score the result.

WHAT IS BEING OPTIMISED, and why it is not recall.

Recall against the 32 hand-mapped rocks saturates: at a 30 m match radius a
random scatter of a few thousand points already "finds" half of them, so the
metric cannot tell a good detector from a dense one. Worse, optimising it
rewards emitting more.

The depth survey does not have that problem. It is independent of the imagery
(lead line, 1954), it covers the whole lake, and it makes a falsifiable
prediction: a real hazard sits in shallow water. The score here is

    offshore lift = (median depth of shore-matched random points)
                    - (median depth of detections)

restricted to water more than 50 m from shore. Restricted, because near shore
everything is shallow and a detector gets credit it did not earn; shore-matched,
because otherwise a detector that simply traces the bank wins without knowing
anything about depth. That is not hypothetical -- it is what the current
detectors do, and this metric is what exposed it.

A configuration that emits more detections does NOT score better here, because
the null is resampled to match. That is the property recall lacked.
"""

import itertools
import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_contours import build_surface
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "data" / "evidence"

OFFSHORE_M = 50.0
MIN_BLOB_M2 = 4.0
MAX_BLOB_M2 = 5000.0
N_POOL = 20000
SEED = 31

# Swept grid. Deliberately coarse -- with only 32 reference rocks and an
# interpolated depth surface, fitting to two decimal places would be fitting
# noise.
BRIGHT_T = (1.5, 2.0, 2.5, 3.0)
DOG_T = (0.0, 1.0, 1.5, 2.0, 2.5)
GRAD_T = (0.0, 1.0, 1.5, 2.0)


def load_evidence():
    meta = json.loads((EVID / "meta.json").read_text())
    arrs = {k: np.load(EVID / f"{k}.npy") for k in ("bright", "dog", "grad", "nir")}
    arrs["total_w"] = np.load(EVID / "total_w.npy")
    return meta, arrs


def main():
    if not (EVID / "meta.json").exists():
        raise SystemExit(f"no evidence grids in {EVID} -- run detect_edge.py first")
    meta, ev = load_evidence()
    res, minx, maxy = meta["res"], meta["minx"], meta["maxy"]
    covered = ev["total_w"] >= 0.6 * meta["total_weight"]
    print(f"evidence grid {meta['W']}x{meta['H']} @ {res} m, "
          f"{covered.sum():,} usable px\n")

    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    grid, gx, gy, lake, _, _ = build_surface(verbose=False)
    shore = lake.boundary
    rng = np.random.default_rng(SEED)

    def depth_at(xs, ys):
        ix = np.clip(np.searchsorted(gx, xs) - 1, 0, len(gx) - 1)
        iy = np.clip(np.searchsorted(gy, ys) - 1, 0, len(gy) - 1)
        return grid[iy, ix]

    # Null pool. Offshore random points with shore distance and depth computed
    # once, so each configuration can cheaply draw a null MATCHED to its own
    # distance-from-shore profile.
    #
    # Matching is not optional. Even beyond 50 m, depth still falls off with
    # distance from the bank, so a configuration that clusters at 60 m looks
    # shallow next to a null spread evenly to the middle of the lake -- and it
    # would have learned nothing except "stay near the edge". An earlier version
    # of this file drew the null uniformly and inflated every score by roughly
    # 14 ft of median depth.
    bx0, by0, bx1, by1 = lake.bounds
    pool, pool_sd = [], []
    while len(pool) < N_POOL:
        rx = rng.uniform(bx0, bx1, N_POOL)
        ry = rng.uniform(by0, by1, N_POOL)
        for a, b in zip(rx, ry):
            p = Point(a, b)
            if not lake.contains(p):
                continue
            sd = shore.distance(p)
            if sd > OFFSHORE_M:
                pool.append((a, b))
                pool_sd.append(sd)
    pool = np.array(pool[:N_POOL])
    pool_sd = np.array(pool_sd[:N_POOL])
    pool_d = depth_at(pool[:, 0], pool[:, 1])
    ok = np.isfinite(pool_d)
    pool, pool_sd, pool_d = pool[ok], pool_sd[ok], pool_d[ok]
    print(f"null pool: {len(pool)} offshore points, "
          f"uniform median {np.median(pool_d):.1f} ft")

    def matched_null(det_sd):
        """Draw from the pool to match a detection set's shore-distance profile."""
        edges = np.percentile(det_sd, np.linspace(0, 100, 9))
        edges[0], edges[-1] = -np.inf, np.inf
        want = np.histogram(det_sd, bins=edges)[0]
        idx = []
        for b in range(len(want)):
            cand = np.nonzero((pool_sd >= edges[b]) & (pool_sd < edges[b + 1]))[0]
            if len(cand) == 0 or want[b] == 0:
                continue
            idx.extend(rng.choice(cand, size=min(want[b], len(cand)), replace=False))
        if not idx:
            return np.array([])
        return pool_d[np.array(idx)]

    print("each row is scored against a null matched to ITS OWN shore profile\n")

    print(f"{'bright':>7s}{'dog':>6s}{'grad':>6s}{'blobs':>8s}{'med ft':>8s}"
          f"{'lift':>7s}{'<10ft':>7s}{'gain':>6s}")
    rows = []
    for bt, dt, gt in itertools.product(BRIGHT_T, DOG_T, GRAD_T):
        hit = covered & (ev["bright"] > bt) & (ev["dog"] > dt) & (ev["grad"] > gt)
        n_px = int(hit.sum())
        if n_px < 200 or n_px > 3_000_000:
            continue
        lbl, n = ndimage.label(hit)
        if not n:
            continue
        sizes = ndimage.sum(hit, lbl, range(1, n + 1)) * res * res
        keep = np.where((sizes >= MIN_BLOB_M2) & (sizes <= MAX_BLOB_M2))[0] + 1
        if len(keep) < 20:
            continue
        cents = ndimage.center_of_mass(hit, lbl, keep)
        cy = np.array([c[0] for c in cents])
        cx = np.array([c[1] for c in cents])
        xs = minx + cx * res
        ys = maxy - cy * res

        sd_all = np.array([shore.distance(Point(x, y)) for x, y in zip(xs, ys)])
        off = sd_all > OFFSHORE_M
        if off.sum() < 20:
            continue
        d = depth_at(xs[off], ys[off])
        fin = np.isfinite(d)
        d = d[fin]
        if d.size < 20:
            continue
        nd = matched_null(sd_all[off][fin])
        if nd.size < 20:
            continue
        med = float(np.median(d))
        nmed = float(np.median(nd))
        shallow = float((d < 10).mean() * 100)
        nshallow = float((nd < 10).mean() * 100)
        rows.append({
            "bright": bt, "dog": dt, "grad": gt,
            "blobs": int(len(keep)), "offshore": int(off.sum()),
            "median_ft": round(med, 2), "null_median_ft": round(nmed, 2),
            "lift_ft": round(nmed - med, 2),
            "shallow_pct": round(shallow, 1),
            "shallow_gain": round(shallow - nshallow, 1),
        })
        print(f"{bt:7.1f}{dt:6.1f}{gt:6.1f}{len(keep):8d}{med:8.1f}"
              f"{nmed - med:+7.1f}{shallow:6.0f}%{shallow - nshallow:+6.1f}")

    if not rows:
        raise SystemExit("no configuration produced a usable blob population")

    # Rank on shallow_gain rather than lift_ft: a median can be dragged by a
    # handful of very deep false positives, while the fraction under 10 ft is
    # the question a boater actually has.
    rows.sort(key=lambda r: (-r["shallow_gain"], -r["lift_ft"]))
    print("\nbest by fraction-under-10ft gain over the matched null:")
    for r in rows[:5]:
        print(f"  bright>{r['bright']} dog>{r['dog']} grad>{r['grad']}  "
              f"{r['blobs']} blobs ({r['offshore']} offshore)  "
              f"median {r['median_ft']} ft (null {r['null_median_ft']})  "
              f"{r['shallow_pct']}% shallow ({r['shallow_gain']:+} vs null)")

    out = ROOT / "data" / "tuning_results.json"
    out.write_text(json.dumps({"offshore_m": OFFSHORE_M, "rows": rows}, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
