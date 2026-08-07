"""Decide whether stage-exposure detection actually found rocks, or just found pixels.

The bar is set by what already failed here. Satellite-derived bathymetry looked
convincing until it was scored against the 260 MDIFW soundings and came back at
AUC 0.507 -- a coin flip -- and the control finished it off: NIR, which cannot
physically carry depth, correlated with depth harder than green did. A detector
that is never scored against an independent measurement is a drawing.

So this scores the same way, against the same soundings, and reports the same
statistic, so the two numbers can be put side by side.

Three questions, in order of how much they matter:

1. Do candidates sit in shallow water? Soundings are the only independent depth
   measurement on this lake. If exposure candidates are not systematically
   shallower than the lake as a whole, there is nothing here.

2. Does the rung track depth? A feature dry in four flights should be shallower
   than one dry in one. If rung is unordered with respect to sounded depth, then
   the staircase is not a staircase and only the binary detection survives.

3. Does the shuffled control collapse? `exposure_stack.py --shuffle` runs the
   identical pipeline with the stage order permuted. Everything the imagery
   contributes survives that permutation; only the stage ordering does not. If
   the control scores as well as the real run, the ordering is decoration and the
   result is whatever the imagery was going to say anyway.

Question 3 is the one that would have caught SDB a day earlier.
"""

import json
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NEAR_M = 100.0     # a sounding this close is evidence about this candidate
RECALL_M = 40.0    # a candidate this close counts as finding a known rock


def load_points(path, depth_key=None):
    fc = json.loads(Path(path).read_text())
    lon, lat, extra = [], [], []
    for ft in fc["features"]:
        g = ft.get("geometry")
        if not g:
            continue
        if g["type"] == "Point":
            x, y = g["coordinates"][:2]
        elif g["type"] == "Polygon":
            ring = g["coordinates"][0]
            x = float(np.mean([p[0] for p in ring]))
            y = float(np.mean([p[1] for p in ring]))
        else:
            continue
        lon.append(x)
        lat.append(y)
        extra.append(ft.get("properties", {}))
    return np.array(lon), np.array(lat), extra


def to_utm(lon, lat):
    fwd = Transformer.from_crs("EPSG:4326", lake_crs(), always_xy=True)
    x, y = fwd.transform(lon, lat)
    return np.asarray(x), np.asarray(y)


def nearest(ax, ay, bx, by):
    """For each a, the index of and distance to the closest b. O(n*m), fine at n=1e4."""
    if len(bx) == 0:
        return np.zeros(len(ax), int), np.full(len(ax), np.inf)
    d = np.hypot(ax[:, None] - bx[None, :], ay[:, None] - by[None, :])
    idx = d.argmin(axis=1)
    return idx, d[np.arange(len(ax)), idx]


def auc(scores, labels):
    """Rank AUC. Reported so it sits directly beside SDB's 0.507."""
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def score_run(path, sx, sy, sdepth, rx, ry, label):
    if not Path(path).exists():
        print(f"\n{label}: {path} not found -- skipped")
        return None
    lon, lat, props = load_points(path)
    if len(lon) == 0:
        print(f"\n{label}: no candidates")
        return {"n": 0}
    cx, cy = to_utm(lon, lat)
    print(f"\n=== {label} === {len(cx)} candidates")

    # 1. depth under the candidates, against the lake's own sounded distribution
    si, sd = nearest(cx, cy, sx, sy)
    close = sd <= NEAR_M
    if close.sum() >= 10:
        near_depth = sdepth[si[close]]
        u = stats.mannwhitneyu(near_depth, sdepth, alternative="less")
        a = auc(np.concatenate([-near_depth, -sdepth]),
                np.concatenate([np.ones(len(near_depth), bool),
                                np.zeros(len(sdepth), bool)]))
        print(f"  nearest sounding within {NEAR_M:.0f} m for {close.sum()}/{len(cx)} candidates")
        print(f"  sounded depth at candidates: median {np.median(near_depth):.1f} ft "
              f"vs lake median {np.median(sdepth):.1f} ft")
        print(f"  shallower than chance: AUC {a:.3f}, Mann-Whitney p = {u.pvalue:.2g}")
        print(f"  (satellite-derived bathymetry scored AUC 0.507 on this lake)")
    else:
        a, u = float("nan"), None
        print(f"  only {close.sum()} candidates have a sounding within {NEAR_M:.0f} m "
              f"-- too few to score against depth")

    # 2. does the rung order match the depth order
    rungs = np.array([p.get("rung", -1) for p in props], "float32")
    ok = close & (rungs >= 0)
    rho = float("nan")
    if ok.sum() >= 10 and len(np.unique(rungs[ok])) > 1:
        rho = float(stats.spearmanr(rungs[ok], sdepth[si[ok]]).statistic)
        print(f"  rung vs sounded depth: Spearman {rho:+.3f} "
              f"(negative = higher rung is shallower, which is the claim)")

    # 3. recall against rocks somebody actually knows about
    _, rd = nearest(rx, ry, cx, cy)
    hit = (rd <= RECALL_M).sum()
    print(f"  known rocks recovered: {hit}/{len(rx)} within {RECALL_M:.0f} m")

    return {"n": int(len(cx)), "auc": a, "rho_rung_depth": rho,
            "recall": f"{hit}/{len(rx)}",
            "p": float(u.pvalue) if u is not None else None}


def main():
    slon, slat, sprops = load_points(DATA / "soundings.geojson")
    sx, sy = to_utm(slon, slat)
    sdepth = np.array([p["depth_ft"] for p in sprops], "float32")
    print(f"{len(sx)} soundings, depth median {np.median(sdepth):.1f} ft, "
          f"range {sdepth.min():.0f}-{sdepth.max():.0f} ft")

    rlon, rlat, _ = load_points(DATA / "reference_rocks.geojson")
    rx, ry = to_utm(rlon, rlat)
    print(f"{len(rx)} reference rocks")

    real = score_run(DATA / "exposure.geojson", sx, sy, sdepth, rx, ry, "REAL stage order")

    controls = sorted(DATA.glob("exposure_shuffled_*.geojson"))
    ctl = [score_run(p, sx, sy, sdepth, rx, ry, f"CONTROL {p.stem.split('_')[-1]}")
           for p in controls]
    ctl = [c for c in ctl if c]

    print("\n" + "=" * 68)
    if not real or not real.get("n"):
        print("VERDICT: the real run found nothing. Nothing to validate.")
        return
    if not ctl:
        print("VERDICT: no shuffled control was run, so the ordering is unproven.")
        print("  .venv/bin/python scripts/exposure_stack.py --shuffle 1")
        return
    cn = np.array([c["n"] for c in ctl], "float32")
    ca = np.array([c.get("auc", np.nan) for c in ctl], "float32")
    print(f"real: {real['n']} candidates, AUC {real.get('auc', float('nan')):.3f}")
    print(f"control: {cn.mean():.0f} candidates on average, "
          f"AUC {np.nanmean(ca):.3f}")
    if real["n"] > 2 * max(cn.max(), 1) and (np.isnan(np.nanmean(ca))
                                             or real.get("auc", 0) > np.nanmean(ca) + 0.1):
        print("VERDICT: the stage ordering is carrying the signal.")
    else:
        print("VERDICT: the control matches the real run. The ordering is not doing "
              "the work -- treat these as imagery persistence, not depth.")


if __name__ == "__main__":
    main()
