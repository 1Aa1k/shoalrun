"""Union the 1 m NAIP detections with the 10 m Sentinel ones.

Two detectors at very different resolutions disagree in informative ways, so
neither is simply thrown away:

  NAIP-only      Almost everything. Rocks too small for a 10 m pixel to ever
                 propose. This is the population that was missing.
  Both           Strongest evidence available here: two sensors, six NAIP
                 flights plus 29 Sentinel dates, independently agreeing.
  Sentinel-only  Kept but demoted. Mostly the sub-pixel false positives NAIP
                 already declined to confirm -- but a Sentinel detection is a
                 29-date persistence statistic, and NAIP is six clear-day
                 snapshots, so a real hazard that happened to be under chop on
                 all six flight days would land here. Not confident enough to
                 promote, not safe to delete.

Matching is by distance between centroids. A rock's 10 m centroid and its 1 m
centroid will not coincide exactly, so the tolerance has to exceed the coarse
pixel size without being so wide it merges genuinely separate rocks.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.strtree import STRtree
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import lake_crs

ROOT = Path(__file__).resolve().parent.parent
NAIP = ROOT / "data" / "rocks_naip.geojson"
NAIP03 = ROOT / "data" / "rocks_naip_03.geojson"
BRIGHT = ROOT / "data" / "rocks_bright.geojson"
SENT = ROOT / "data" / "verified.geojson"
REFS = ROOT / "data" / "reference_rocks.geojson"
BUOYS = ROOT / "data" / "buoy_candidates.geojson"
OUT = ROOT / "data" / "hazards.geojson"

MATCH_M = 15.0  # a bit over one Sentinel pixel
MIN_AREA_M2 = 10.0  # see the measured tradeoff in main()

# Default display cut. Hazards closer than this to shore are hidden from the map
# by default, NOT from the alerting index -- see hazard.js. A boater heading at
# the shoreline must still be warned about shoreline rock.
OFFSHORE_M = 50.0


def lake_boundary():
    from shoalrun_config import lake_crs as _lc
    crs = _lc()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lake = shp_transform(
        lambda x, y: fwd.transform(x, y),
        shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"]),
    )
    return lake.boundary


def main():
    crs = lake_crs()
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def load(path):
        """Layers arrive at different times -- a long run must not block a merge."""
        if not path.exists():
            print(f"  (skipping {path.name}: not produced yet)")
            return []
        return json.loads(path.read_text())["features"]

    # MEASURED against 32 hand-mapped rocks, each scored beside a same-size
    # random null. The null matters more than the recall: scatter 12,841 points
    # on a 34 km2 lake and half of them land within 30 m of a known rock by luck
    # alone, so only the LIFT over random means anything.
    #   naip-1m         12841 det, 97% recall, 56% random, lift +41%, err 11.5 m
    #   naip-bright-1m   8454 det, 91% recall, 47% random, lift +44%, err 13.0 m
    #   naip-0.3m        3562 det, 56% recall, 22% random, lift +34%, err 20.2 m
    # An earlier version of this comment claimed "97% recall (20% random)". That
    # 20% came from a differently-constructed null and overstated the lift badly.
    # The 1 m pass still beats the 0.3 m pass -- more DATES beat finer pixels,
    # since 0.3 m imagery only exists from 2018 so persistence had three chances
    # instead of six -- but by less than previously advertised.
    naip = load(NAIP)

    # Size floor, measured rather than guessed. Against the 32 hand-mapped rocks:
    #   all           10442 features, 97% recall (18% random baseline)
    #   area >= 10    3460  features, 88% recall (12% random)   <- kept
    #   area >= 25    906   features, 31% recall  -- collapses
    # Recall falls off a cliff above 10 m2 because the real rocks ARE small
    # (median detected blob 7 m2). Filtering harder removes the hazards, not the
    # noise, which is the opposite of what a size filter is supposed to do.
    before = len(naip)
    naip = [f for f in naip if (f["properties"].get("area_m2") or 0) >= MIN_AREA_M2]
    if before:
        print(f"  size floor {MIN_AREA_M2:g} m2: {before} -> {len(naip)} (keeps 88% recall)")
    sent = load(SENT)
    print(f"NAIP: {len(naip)}   Sentinel 10 m: {len(sent)}")

    def pt(f):
        p = f["properties"]
        return shape({"type": "Point", "coordinates": [p["lon"], p["lat"]]})

    # The brightness detector is a genuinely INDEPENDENT channel, not a rerun.
    # detect_naip.py asks "is this pixel greener than the water around it" --
    # a colour ratio, tuned for bottom seen through a water column.
    # detect_bright.py asks "is this pixel far brighter than the water around
    # it" across all four bands, which is what makes bare granite obvious to a
    # person. The two can fail independently: glint fools brightness, weed beds
    # and shallow sand fool the colour ratio. So where they AGREE is worth more
    # than either alone, and that agreement becomes a confidence tier rather
    # than a filter -- nothing is deleted for failing to corroborate, because a
    # rock only one channel can see is still a rock in the water.
    bright = load(BRIGHT)
    bright = [f for f in bright if (f["properties"].get("area_m2") or 0) >= MIN_AREA_M2]
    bright_pts = [shp_transform(lambda x, y: fwd.transform(x, y), pt(f)) for f in bright]
    bright_tree = STRtree(bright_pts) if bright_pts else None
    print(f"brightness channel: {len(bright)} (>= {MIN_AREA_M2:g} m2)")

    naip_pts = [shp_transform(lambda x, y: fwd.transform(x, y), pt(f)) for f in naip]
    tree = STRtree(naip_pts) if naip_pts else None

    out = []
    matched_naip = set()

    for f in sent:
        p = dict(f["properties"])
        sp = shp_transform(lambda x, y: fwd.transform(x, y), pt(f))
        hit = None
        if tree is not None:
            for j in tree.query(sp.buffer(MATCH_M)):
                if naip_pts[j].distance(sp) <= MATCH_M:
                    hit = int(j)
                    break
        if hit is not None:
            matched_naip.add(hit)
            np_ = naip[hit]["properties"]
            p.update({
                "class": np_["class"],
                "area_m2": np_["area_m2"],
                "evidence": "both",
                "naip_flights": np_["flights"],
                "confidence_rank": 3,
            })
        else:
            p["evidence"] = "sentinel_only"
            p["confidence_rank"] = 1
        p["source"] = "merged"
        out.append({"type": "Feature", "properties": p, "geometry": None})

    for j, f in enumerate(naip):
        if j in matched_naip:
            continue
        p = dict(f["properties"])
        p["evidence"] = "naip_only"
        p["confidence_rank"] = 2
        out.append({"type": "Feature", "properties": p, "geometry": None})

    # Corroboration pass. Every hazard so far gets asked whether the independent
    # brightness channel also saw something here; those that do are promoted.
    matched_bright = set()
    for f in out:
        pr = f["properties"]
        if pr.get("lon") is None or bright_tree is None:
            continue
        g = shp_transform(lambda x, y: fwd.transform(x, y), pt(f))
        near = [int(j) for j in bright_tree.query(g.buffer(MATCH_M))
                if bright_pts[j].distance(g) <= MATCH_M]
        if near:
            matched_bright.update(near)
            pr["bright_confirmed"] = True
            pr["confidence_rank"] = max(pr.get("confidence_rank", 1), 3)
        else:
            pr["bright_confirmed"] = False

    # Brightness-only detections. Rank 2: one channel saw it, and that channel
    # is the one keyed on how a bare rock actually looks. Not promoted to 3
    # without a second opinion, not dropped either.
    added_bright = 0
    for j, f in enumerate(bright):
        if j in matched_bright:
            continue
        p = dict(f["properties"])
        p["evidence"] = "bright_only"
        p["bright_confirmed"] = True
        p["confidence_rank"] = 2
        out.append({"type": "Feature", "properties": p, "geometry": None})
        added_bright += 1
    n_corr = sum(1 for f in out if f["properties"].get("bright_confirmed"))
    print(f"\nbrightness corroborated {n_corr} hazards; added {added_bright} it alone saw")

    # Human-mapped rocks go in as FIRST-CLASS hazards, not as a scoring rubric.
    # 25 of these 32 are statistically invisible in 0.3 m imagery -- no aerial
    # method at any resolution will ever produce them, so if they are not carried
    # through from human mapping they are simply absent from the map. They are the
    # single most valuable population in the dataset and they are also the one the
    # detector can never recover on its own.
    if REFS.exists():
        refs = json.loads(REFS.read_text())["features"]
        added = 0
        for f in refs:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            rp = shp_transform(lambda x, y: fwd.transform(x, y),
                               shape({"type": "Point", "coordinates": [lon, lat]}))
            dup = any(
                shp_transform(lambda x, y: fwd.transform(x, y),
                              shape({"type": "Point", "coordinates":
                                     [o["properties"]["lon"], o["properties"]["lat"]]})).distance(rp) <= MATCH_M
                for o in out if o["properties"].get("lon") is not None
            )
            if dup:
                continue
            out.append({"type": "Feature", "properties": {
                "class": "rock" if p.get("kind") != "reef" else "shoal",
                "lat": lat, "lon": lon,
                "area_m2": None,
                "evidence": "human_mapped",
                "confidence_rank": 3,
                "source": p.get("source", "osm"),
                "kind": p.get("kind"),
                "verdict": "human_mapped",
                "note": "mapped by a person; most of these are invisible to imagery",
            }, "geometry": None})
            added += 1
        print(f"\nadded {added} human-mapped rocks not already covered by a detection")

    if BUOYS.exists():
        bs = json.loads(BUOYS.read_text())["features"]
        for f in bs:
            lon, lat = f["geometry"]["coordinates"]
            out.append({"type": "Feature", "properties": {
                **f["properties"], "lat": lat, "lon": lon,
                "evidence": "buoy_proxy", "confidence_rank": 2,
                "verdict": "buoy_candidate",
            }, "geometry": None})
        print(f"added {len(bs)} buoy candidates (floating-object proxies for unseen rock)")

    # Distance from shore, stamped on every hazard. The known rocks sit a median
    # 2 m from shore, so a map showing everything is a solid band of markers
    # around the whole lake -- true (MDIFW 1954: "rockiness is the outstanding
    # feature") but useless, because a boater already knows to stay off the
    # shore. The offshore subset is the part that surprises you.
    shore = lake_boundary()
    for f in out:
        pr = f["properties"]
        if pr.get("lon") is None:
            continue
        g = shp_transform(lambda x, y: fwd.transform(x, y),
                          shape({"type": "Point", "coordinates": [pr["lon"], pr["lat"]]}))
        d = shore.distance(g)
        pr["shore_m"] = round(float(d), 1)
        pr["offshore"] = bool(d > OFFSHORE_M)

    n_off = sum(1 for f in out if f["properties"].get("offshore"))
    print(f"\noffshore (> {OFFSHORE_M:g} m from shore): {n_off} of {len(out)}")

    stats = defaultdict(int)
    cls = defaultdict(int)
    for f in out:
        stats[f["properties"]["evidence"]] += 1
        cls[f["properties"]["class"]] += 1

    print("\nevidence:")
    for k in ("both", "naip_only", "bright_only", "sentinel_only", "human_mapped", "buoy_proxy"):
        if stats.get(k):
            print(f"  {k:14s} {stats[k]:5d}")
    print("\nclass:")
    for k, v in sorted(cls.items(), key=lambda kv: -kv[1]):
        print(f"  {k:10s} {v:5d}")
    print(f"\ntotal {len(out)}")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": out}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
