"""Turn the multi-date stack into rock candidates.

The central problem is that a whitecap and a rock look identical in any single
image: both are a bright non-water blob sitting in open water. They separate on
two axes that only a time series exposes.

  1. Persistence.   A wave tip is in exactly one scene. A rock is in many.
  2. Stage coupling. This lake is regulated, so its level moves. A rock that
     dries out at drawdown is land *precisely when the lake is low* -- its
     exposure correlates hard with stage. A whitecap is driven by wind, which is
     independent of lake level, so its correlation with stage is noise.

Axis 2 is what does the real work. Persistence alone would also promote a spot
that happens to be windy in every image; requiring the exposure to track the
water level is what a wave cannot fake.

Three output classes:

  exposed   Land in most scenes. A visible ledge or boulder. Least dangerous --
            your friend can see these -- but they anchor the map's credibility,
            because he can check them by eye.
  drawdown  Land only when the lake is low. THE boat-killer class: invisible at
            full pond, hard rock a foot under the prop.
  shoal     Never dries out, but persistently brighter in green than the water
            around it. Green penetrates a few metres of clear water, so this is
            a shallow bottom seen through the water column.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import rasterio
from rasterio.features import shapes
from scipy import ndimage

from shoalrun_config import MIN_SUN_ELEVATION_DEG, lake_crs, solar_elevation
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "data" / "stack.npz"
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "rocks.geojson"
EDGE_OUT = ROOT / "data" / "edges.geojson"

# Derived from the lake's own centroid, so the pipeline is not pinned to Maine.
LAKE_CRS = lake_crs()

# The water/land split is found per scene by Otsu rather than fixed. Sentinel-2
# processing baseline 04.00 (Jan 2022) added a +1000 DN offset to every band, so
# a stack spanning that date has two different radiometric conventions in it --
# measured here, the NDWI split moves from -0.333 (2019) to -0.234 (2023). Any
# hardcoded threshold is wrong for one era or the other. Otsu also absorbs
# atmospheric and sun-angle variation for free, and because it is per scene, the
# offset cancels out of every statistic downstream.
OTSU_BINS = 512
NDWI_FALLBACK = -0.25   # only if a scene's histogram is degenerate

# ESA rolled processing baseline 04.00 out on this date; scenes from here on
# carry a +1000 DN offset in every band.
BASELINE_0400_DATE = "2022-01-25"
SHORE_BUFFER_M = 20.0   # pixels this close to mapped shore are mixels, not rocks
MIN_VALID_OBS = 12      # a pixel needs this many clean looks before we trust it

# Whole-scene quality gate, separate from the sun-elevation gate. A scene that is
# mostly cloud and shadow still passes an elevation test but should not get an
# equal vote in a persistence statistic -- its few clear pixels are unrepresentative
# and its water mask is unreliable. Measured: the one scene below this threshold
# reported the lake 20 points drier than every neighbouring date.
MIN_SCENE_USABLE = 0.85
STAGE_LOW_Q = 0.30      # bottom 30% of scenes by water area = "low stage"
STAGE_HIGH_Q = 0.70

# Scenes are filtered on SUN ELEVATION, not on the calendar. Low sun raises
# specular response over water until it swamps the water index; at this lake an
# October scene read the lake as 48% dry, physically impossible for 34.6 km2.
# Months were only ever a proxy for elevation and a proxy that holds at exactly
# one latitude. See shoalrun_config.MIN_SUN_ELEVATION_DEG.

# The drawdown class is DISABLED, and this is the most important comment here.
# The idea was sound: a rock that dries out at low pond is the one that takes a
# lower unit off, and stage-correlated exposure is something a whitecap cannot
# fake. It is not detectable from this data. The only radiometrically stable
# window (Jul-Aug) is exactly when a storage reservoir sits at full pond, and the
# autumn scenes where drawdown actually occurs are the ones whose radiometry
# falls apart. Measured stage spread inside the trusted window is ~1.1%, which is
# the noise floor of the water mask itself -- so anything this would emit is
# noise wearing a hazard label. Re-enable ONLY with a real stage source
# (Brookfield operating records) or winter/spring imagery with BRDF correction.
EMIT_DRAWDOWN = False

EXPOSED_MIN = 0.60      # land fraction to call it a permanently visible rock

# Above this footprint a blob is not a rock, it is a landmass OSM never mapped --
# the largest here is 96,700 m2, which is 9.7 hectares. Calling that "a rock" is
# wrong, and drawing it as a point marker scaled to its true size produced a
# 175 m radius disc that swallowed the map. Split it into its own class so it can
# be shown as an obstruction outline instead of a hazard dot.
ISLAND_MIN_M2 = 5000.0
DRAWDOWN_MIN_DELTA = 0.35   # land-at-low minus land-at-high, for drawdown class
SHOAL_Z = 1.8           # green-anomaly z-score, sustained, to call it a shoal
SHOAL_MIN_FRAC = 0.55   # fraction of scenes the anomaly must hold
NEIGHBORHOOD_PX = 25    # ~250 m box for the local water-brightness baseline
MIN_BLOB_PX = 2         # a single 10 m pixel is too easily a sensor artefact


def otsu(values, bins=OTSU_BINS):
    """Threshold maximising between-class variance. Returns a value in NDWI units."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return NDWI_FALLBACK
    hist, edges = np.histogram(finite, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    hist = hist.astype("float64")
    total = hist.sum()
    if total == 0:
        return NDWI_FALLBACK

    w0 = np.cumsum(hist)
    w1 = total - w0
    csum = np.cumsum(hist * centers)
    grand = csum[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        mu0 = csum / w0
        mu1 = (grand - csum) / w1
    between = w0 * w1 * (mu0 - mu1) ** 2
    between[~np.isfinite(between)] = -np.inf
    if not np.isfinite(between).any():
        return NDWI_FALLBACK
    return float(centers[int(np.argmax(between))])


def load():
    z = np.load(STACK, allow_pickle=False)
    gdal_tf = z["transform"]
    transform = rasterio.Affine.from_gdal(*gdal_tf)
    return z["green"], z["nir"], z["valid"], transform, str(z["crs"]), json.loads(str(z["meta"]))


def lake_masks(transform, shape_hw):
    """Rasterise the lake, and an inward-eroded version that drops shore mixels."""
    from pyproj import Transformer
    from rasterio.features import rasterize

    lake_ll = shape(json.loads(LAKE.read_text())["geometry"])
    tf = Transformer.from_crs("EPSG:4326", LAKE_CRS, always_xy=True)
    lake = shp_transform(lambda x, y: tf.transform(x, y), lake_ll)

    full = rasterize([(mapping(lake), 1)], out_shape=shape_hw, transform=transform, dtype="uint8")
    inner = rasterize(
        [(mapping(lake.buffer(-SHORE_BUFFER_M)), 1)],
        out_shape=shape_hw,
        transform=transform,
        dtype="uint8",
    )
    return full.astype(bool), inner.astype(bool), lake


def main():
    green, nir, valid, transform, crs, meta = load()
    T, H, W = green.shape
    print(f"stack: {T} scenes, {H} x {W}")

    # Undo the baseline-04.00 radiometric offset before anything else. Otsu can
    # find the water/land split within either era, but it cannot make the two
    # eras commensurable -- and the stage proxy compares scenes ACROSS years, so
    # an uncorrected +1000 DN step reads as a lake-level step and manufactures a
    # whole fake population of "drawdown" rocks.
    n_shift = 0
    for t in range(T):
        offset = meta[t].get("boa_offset")
        if offset is None:  # stacks written before this was recorded
            offset = 1000 if meta[t]["date"] >= BASELINE_0400_DATE else 0
        if offset:
            green[t] -= offset
            nir[t] -= offset
            n_shift += 1
    # Reflectance cannot be negative; clamp rather than let ratios explode.
    np.clip(green, 1.0, None, out=green)
    np.clip(nir, 1.0, None, out=nir)
    print(f"BOA offset removed from {n_shift}/{T} scenes (baseline >= 04.00)")

    import datetime as _dt
    from shapely.geometry import shape as _shape
    _c = _shape(json.loads(LAKE.read_text())["geometry"]).centroid
    keep, elevations = [], []
    for t in range(T):
        when = _dt.datetime.fromisoformat(meta[t]["date"].replace("Z", "+00:00")).replace(tzinfo=None)
        elev = solar_elevation(when, _c.y, _c.x)
        elevations.append(elev)
        if elev >= MIN_SUN_ELEVATION_DEG and meta[t].get("usable", 1.0) >= MIN_SCENE_USABLE:
            keep.append(t)
    if len(keep) < MIN_VALID_OBS:
        raise SystemExit(
            f"only {len(keep)} scenes above {MIN_SUN_ELEVATION_DEG} deg sun elevation; "
            "refusing to emit hazards from too little evidence"
        )
    dropped_q = sum(
        1 for t in range(T)
        if elevations[t] >= MIN_SUN_ELEVATION_DEG and meta[t].get("usable", 1.0) < MIN_SCENE_USABLE
    )
    print(f"sun elevation >= {MIN_SUN_ELEVATION_DEG} deg and usable >= {MIN_SCENE_USABLE:.0%}: "
          f"kept {len(keep)}/{T} (elevation range {min(elevations):.0f}-{max(elevations):.0f} deg, "
          f"{dropped_q} dropped on scene quality)")
    green, nir, valid = green[keep], nir[keep], valid[keep]
    meta = [meta[t] for t in keep]
    T = len(keep)


    lake_mask, inner_mask, lake_utm = lake_masks(transform, (H, W))
    print(f"lake pixels: {lake_mask.sum():,}   after {SHORE_BUFFER_M:g} m shore erosion: {inner_mask.sum():,}")

    # --- NDWI and per-scene water -------------------------------------------
    denom = green + nir
    with np.errstate(divide="ignore", invalid="ignore"):
        ndwi = np.where(denom > 0, (green - nir) / denom, np.nan)

    # Threshold each scene on its own histogram, over a neighbourhood of the
    # lake so both modes (water and shore) are actually present in the sample.
    thresholds = np.empty(T, dtype="float32")
    for t in range(T):
        sample = ndwi[t][valid[t] & np.isfinite(ndwi[t])]
        thresholds[t] = otsu(sample) if sample.size > 10000 else NDWI_FALLBACK
    print(f"otsu thresholds: {thresholds.min():+.3f} .. {thresholds.max():+.3f} "
          f"(median {np.median(thresholds):+.3f})")

    th = thresholds[:, None, None]
    is_water = (ndwi > th) & valid
    is_land = (ndwi <= th) & valid

    # --- stage proxy: water area inside the lake polygon, per scene ----------
    area = np.array([(is_water[t] & lake_mask).sum() for t in range(T)], dtype="float64")
    obs = np.array([(valid[t] & lake_mask).sum() for t in range(T)], dtype="float64")
    stage = area / np.maximum(obs, 1)  # fraction of observed lake that is wet

    lo_cut, hi_cut = np.quantile(stage, [STAGE_LOW_Q, STAGE_HIGH_Q])
    low_idx = np.where(stage <= lo_cut)[0]
    high_idx = np.where(stage >= hi_cut)[0]
    print(f"stage proxy: {stage.min():.4f} .. {stage.max():.4f}")
    print(f"  low-stage scenes: {len(low_idx)}   high-stage scenes: {len(high_idx)}")
    for i in low_idx[:3]:
        print(f"    LOW  {meta[i]['date'][:10]}  wet={stage[i]:.4f}")
    for i in high_idx[:3]:
        print(f"    HIGH {meta[i]['date'][:10]}  wet={stage[i]:.4f}")

    n_valid = valid.sum(axis=0)
    enough = n_valid >= MIN_VALID_OBS

    def land_frac(idx):
        v = valid[idx].sum(axis=0)
        return np.where(v > 0, is_land[idx].sum(axis=0) / np.maximum(v, 1), 0.0), v

    frac_all, _ = land_frac(np.arange(T))
    frac_low, v_low = land_frac(low_idx)
    frac_high, v_high = land_frac(high_idx)

    # --- class 1: permanently exposed rock ----------------------------------
    exposed = (frac_all >= EXPOSED_MIN) & inner_mask & enough

    # --- class 2: drawdown rock ---------------------------------------------
    # Dry when low, wet when high, with enough looks at both ends to mean it.
    delta = frac_low - frac_high
    drawdown = (
        (delta >= DRAWDOWN_MIN_DELTA)
        & (frac_high < EXPOSED_MIN)
        & (v_low >= 4)
        & (v_high >= 4)
        & inner_mask
        & enough
        & ~exposed
    )

    # --- class 3: submerged shoal from green brightness ---------------------
    # Compare each water pixel's green to the local water background. A shallow
    # bottom kicks green up; deep water is flat. Sun glint does this too, which
    # is why it must hold across most scenes, not just be bright on average.
    anomaly_hits = np.zeros((H, W), dtype="int32")
    anomaly_obs = np.zeros((H, W), dtype="int32")
    for t in range(T):
        wet = is_water[t] & lake_mask
        if wet.sum() < 1000:
            continue
        g = np.where(wet, green[t], np.nan)
        # Box mean/std over water only, via masked sums -- avoids nan-propagation.
        m = wet.astype("float32")
        gs = np.where(wet, green[t], 0.0).astype("float32")
        cnt = ndimage.uniform_filter(m, NEIGHBORHOOD_PX, mode="nearest")
        mean = ndimage.uniform_filter(gs, NEIGHBORHOOD_PX, mode="nearest")
        mean = np.where(cnt > 0.05, mean / np.maximum(cnt, 1e-6), np.nan)
        sq = ndimage.uniform_filter(np.where(wet, green[t] ** 2, 0.0).astype("float32"),
                                    NEIGHBORHOOD_PX, mode="nearest")
        sq = np.where(cnt > 0.05, sq / np.maximum(cnt, 1e-6), np.nan)
        std = np.sqrt(np.maximum(sq - mean ** 2, 1e-6))
        with np.errstate(invalid="ignore"):
            z = (g - mean) / std
        anomaly_hits += ((z > SHOAL_Z) & wet).astype("int32")
        anomaly_obs += wet.astype("int32")

    shoal_frac = np.where(anomaly_obs > 0, anomaly_hits / np.maximum(anomaly_obs, 1), 0.0)
    shoal = (
        (shoal_frac >= SHOAL_MIN_FRAC)
        & (anomaly_obs >= MIN_VALID_OBS)
        & inner_mask
        & ~exposed
        & ~drawdown
    )

    print(f"raw pixels -> exposed {exposed.sum():,}  drawdown {drawdown.sum():,}  shoal {shoal.sum():,}")

    # --- vectorise -----------------------------------------------------------
    classes = [("exposed", exposed, frac_all), ("shoal", shoal, shoal_frac)]
    if EMIT_DRAWDOWN:
        classes.insert(1, ("drawdown", drawdown, delta))
    else:
        print(f"drawdown class suppressed ({drawdown.sum():,} px) -- below noise floor, see EMIT_DRAWDOWN")

    feats = []
    for name, mask, conf in classes:
        lbl, n = ndimage.label(mask)
        if n == 0:
            continue
        sizes = ndimage.sum(mask, lbl, range(1, n + 1))
        keep_ids = np.where(sizes >= MIN_BLOB_PX)[0] + 1
        clean = np.isin(lbl, keep_ids)

        # Per-blob confidence is the mean of that class's driving statistic over
        # the blob -- reported so a human can sort by how sure the pixels were,
        # rather than treating every candidate as equally believable.
        blob_conf = ndimage.mean(conf, lbl, keep_ids)
        conf_by_id = dict(zip(keep_ids.tolist(), np.atleast_1d(blob_conf).tolist()))

        clean_lbl = np.where(clean, lbl, 0).astype("int32")
        for geom, val in shapes(clean_lbl, mask=clean, transform=transform):
            g = shape(geom)
            feats.append(
                (name, g, g.area / 100.0, g.centroid.x, g.centroid.y, conf_by_id.get(int(val), 0.0))
            )

    print(f"blobs: {len(feats)}")

    # Reproject to WGS84 for the app.
    from pyproj import Transformer

    back = Transformer.from_crs(LAKE_CRS, "EPSG:4326", always_xy=True)
    # Promote oversized "exposed" blobs to their own island class. Done after
    # vectorising because the decision is about the blob's footprint, which only
    # exists once the pixels have been grouped.
    promoted = 0
    relabelled = []
    for name, g, px, cx, cy, conf in feats:
        if name == "exposed" and px * 100 >= ISLAND_MIN_M2:
            name = "island"
            promoted += 1
        relabelled.append((name, g, px, cx, cy, conf))
    feats = relabelled
    print(f"promoted {promoted} oversized blobs to class 'island' (>= {ISLAND_MIN_M2:g} m2)")

    out = []
    for name, g, px, cx, cy, conf in feats:
        gll = shp_transform(lambda x, y: back.transform(x, y), g)
        lon, lat = back.transform(cx, cy)
        out.append(
            {
                "type": "Feature",
                "properties": {
                    "class": name,
                    "area_px": round(px, 1),
                    "area_m2": round(px * 100, 0),
                    "confidence": round(float(conf), 3),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "status": "unverified",
                },
                "geometry": mapping(gll),
            }
        )

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": out}))
    counts = {}
    for f in out:
        counts[f["properties"]["class"]] = counts.get(f["properties"]["class"], 0) + 1
    print(f"wrote {OUT}: {counts}")

    # Also dump the median-composite waterline as the edge map Nate asked for.
    med_water = (is_water.sum(axis=0) / np.maximum(valid.sum(axis=0), 1)) > 0.5
    med_water &= enough
    edge = med_water.astype("uint8")
    efeats = []
    for geom, val in shapes(edge, mask=edge.astype(bool), transform=transform):
        g = shp_transform(lambda x, y: back.transform(x, y), shape(geom))
        if g.area > 0:
            efeats.append({"type": "Feature", "properties": {"kind": "median_waterline"},
                           "geometry": mapping(g)})
    EDGE_OUT.write_text(json.dumps({"type": "FeatureCollection", "features": efeats}))
    print(f"wrote {EDGE_OUT}: {len(efeats)} polygons")


if __name__ == "__main__":
    main()
