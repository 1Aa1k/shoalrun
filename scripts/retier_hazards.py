"""Sort hazards by how much the evidence actually supports them.

Forced by a measurement. The imagery cannot distinguish 10 ft of water from
25 ft on this lake -- AUC 0.507 against the soundings, a coin flip (see
diagnose_sdb.py). Bottom is not visible, so any detection whose meaning depends
on seeing bottom through water cannot mean what it claims.

That is most of the map. 3,545 of 4,908 hazards are classed "shoal", and a shoal
detection is by definition submerged bottom showing through. Those markers were
being drawn with the same authority as an island.

Rather than delete them -- something did persist across six flights at those
spots, and deleting it would throw away the one thing aerial imagery is good for
-- each hazard is tiered by what kind of evidence stands behind it:

  confirmed   Seen directly, above the waterline, and independently checked.
              Islands, exposed rock, and rocks people mapped by hand. NAIP 0.3 m
              verification put these at 96-100%.

  likely      A dry-surface signature: returns near-infrared, which water
              absorbs. That is a direct look at something breaking the surface,
              not an inference through a water column, so the depth finding does
              not undermine it.

  unverified  Everything resting on bottom-through-water. Persistent across
              flights, so not nothing -- but the physics that would make it a
              shoal has been measured and is absent. Could be glint, weed, a
              bottom feature, or a real rock.

The app must not draw the third tier like the first. A boater trusting an
unverified marker as a charted hazard is the failure mode that matters here.
"""

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAZ = ROOT / "data" / "hazards.geojson"

# NIR excess over local water. Water absorbs near-infrared almost completely, so
# a return means a dry surface -- the one optical measurement on this lake that
# survives the depth result intact.
DRY_MIN = 0.5


def tier_of(p):
    cls = p.get("class")
    if cls in ("island", "exposed") or p.get("evidence") == "human_mapped":
        return "confirmed"
    if (p.get("dry_frac") or 0) >= DRY_MIN:
        return "likely"
    # The NAIP detector's own rock class is keyed on the same NIR-dry test, so
    # it carries the same standing even where dry_frac was not recorded.
    if cls == "rock" and p.get("evidence") in ("naip_only", "both", "bright_only"):
        return "likely"
    return "unverified"


def main():
    d = json.loads(HAZ.read_text())
    counts = Counter()
    for f in d["features"]:
        p = f["properties"]
        t = tier_of(p)
        p["tier"] = t
        # Say plainly what the marker is claiming, so the app never has to guess.
        p["basis"] = {
            "confirmed": "seen above the waterline and cross-checked",
            "likely": "reflects infrared, so a dry surface breaking the water",
            "unverified": "persistent in imagery; bottom is NOT visible on this "
                          "lake, so its depth meaning is unproven",
        }[t]
        counts[t] += 1

    d["meta"] = {
        "tiers": dict(counts),
        "note": ("Aerial imagery on this lake carries no depth information "
                 "(AUC 0.507 shallow vs deep against 260 soundings). Only "
                 "surface-breaking hazards are directly evidenced."),
    }
    HAZ.write_text(json.dumps(d))
    total = sum(counts.values())
    for t in ("confirmed", "likely", "unverified"):
        print(f"  {t:11s} {counts[t]:5d}  ({counts[t]/total*100:.0f}%)")
    print(f"\ntotal {total}\nwrote {HAZ}")


if __name__ == "__main__":
    main()
