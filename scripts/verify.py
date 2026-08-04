"""Sanity-check the candidate set and render an overview PNG.

A rock detector that fires in the middle of open water far from any shoreline is
almost certainly finding sun glint. Real hazards on a lake like this cluster
near shore, around islands, and on the shoals between them. This checks that
distribution rather than taking the pixel count on faith.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from shapely.geometry import shape
from shapely.ops import transform as st
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent.parent
lake = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
rocks = json.loads((ROOT / "data" / "rocks.geojson").read_text())["features"]

tr = Transformer.from_crs("EPSG:4326", "EPSG:32619", always_xy=True)
lake_u = st(lambda x, y: tr.transform(x, y), lake)
shore = lake_u.boundary

print(f"lake: {lake_u.area / 1e6:.2f} km2, {shore.length / 1000:.1f} km shoreline")
print(f"candidates: {len(rocks)}")

dists = {}
for f in rocks:
    g = st(lambda x, y: tr.transform(x, y), shape(f["geometry"]))
    d = g.centroid.distance(shore)
    dists.setdefault(f["properties"]["class"], []).append(d)

print("\ndistance from shoreline (m):")
for cls, ds in sorted(dists.items()):
    ds = np.array(ds)
    print(
        f"  {cls:9s} n={len(ds):4d}  median={np.median(ds):6.0f}  "
        f"p90={np.percentile(ds, 90):6.0f}  max={ds.max():6.0f}  "
        f"within 150m: {(ds <= 150).mean():.0%}"
    )

areas = np.array([f["properties"]["area_m2"] for f in rocks])
print(f"\nfootprint m2: median={np.median(areas):.0f} p90={np.percentile(areas,90):.0f} max={areas.max():.0f}")

# --- render ---------------------------------------------------------------
fig, ax = plt.subplots(figsize=(13, 10), dpi=130)
fig.patch.set_facecolor("#0d1117")
ax.set_facecolor("#0d1117")

polys = lake_u.geoms if hasattr(lake_u, "geoms") else [lake_u]
for p in polys:
    xs, ys = p.exterior.xy
    ax.fill(xs, ys, color="#12354f", zorder=1)
    ax.plot(xs, ys, color="#4a86b4", lw=0.7, zorder=3)
    for ring in p.interiors:  # islands
        xs, ys = ring.xy
        ax.fill(xs, ys, color="#0d1117", zorder=2)
        ax.plot(xs, ys, color="#4a86b4", lw=0.6, zorder=3)

# Depth contours underneath the hazards, so the two can be read together.
contour_path = ROOT / "data" / "contours.geojson"
if contour_path.exists():
    cfeats = json.loads(contour_path.read_text())["features"]
    depths = sorted({f["properties"]["depth_ft"] for f in cfeats})
    for f in cfeats:
        d = f["properties"]["depth_ft"]
        g = st(lambda x, y: tr.transform(x, y), shape(f["geometry"]))
        shade = 0.25 + 0.6 * (d / max(depths))
        ax.plot(*g.xy, color=(0.35 * shade, 0.6 * shade, 0.95 * shade), lw=0.9, zorder=4)
    # Label the deepest contour so the basin is identifiable.
    deepest = [f for f in cfeats if f["properties"]["depth_ft"] == max(depths)]
    for f in deepest:
        g = st(lambda x, y: tr.transform(x, y), shape(f["geometry"]))
        c = g.centroid
        ax.text(c.x, c.y, f"{max(depths)} ft", color="#9fd0ff", fontsize=9,
                ha="center", zorder=6)

style = {"exposed": ("#c3d0da", "exposed rock/ledge"), "shoal": ("#ffb02e", "submerged shoal")}
for cls, (color, label) in style.items():
    pts = [(f["properties"]["lon"], f["properties"]["lat"]) for f in rocks
           if f["properties"]["class"] == cls]
    if not pts:
        continue
    xy = np.array([tr.transform(lo, la) for lo, la in pts])
    ax.scatter(xy[:, 0], xy[:, 1], s=34, c=color, edgecolors="k",
               linewidths=0.4, alpha=0.9, zorder=5, label=f"{label} ({len(pts)})")

ax.set_aspect("equal")
ax.axis("off")
ax.legend(loc="upper left", facecolor="#161b22", edgecolor="#30363d", labelcolor="#c9d1d9", fontsize=10)
ax.set_title("Millinocket Lake - rock candidates from 30 Sentinel-2 scenes (Jul/Aug 2019-2026)\n"
             "UNVERIFIED - navigation aid only, not a chart",
             color="#c9d1d9", fontsize=12, pad=14)
out = ROOT / "data" / "overview.png"
fig.savefig(out, facecolor=fig.get_facecolor(), bbox_inches="tight")
print(f"\nwrote {out}")
