"""Extract MDIFW lake-depth soundings for Millinocket Lake from the state KMZ tiles.

This is the first genuinely *measured* data in the project. Everything else here
is inferred from reflectance; these are numbers somebody got by dropping a line
through the ice or off the side of a boat. MDIFW surveyed this lake in August
1954 (revised January 1979) along east-west transects, and the state later
digitised those soundings into the LakeDpth layer.

Depths are in FEET below the surface at time of survey. That reference matters:
this lake is regulated, so the water level on any given day is not the level the
soundings were taken at. Treated as approximate, which is what it is.
"""

import json
import re
import zipfile
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent
KMZ_DIR = ROOT / "data" / "kmz"
LAKE = ROOT / "data" / "lake.geojson"
OUT = ROOT / "data" / "soundings.geojson"

# Pad the lake polygon slightly: soundings sit near shore and the 1954 survey's
# georeferencing will not agree with OSM's shoreline to the metre.
PAD_DEG = 0.002

COORD_RE = re.compile(r"<coordinates>\s*([-\d.]+),([-\d.]+)(?:,([-\d.]+))?\s*</coordinates>")
NAME_RE = re.compile(r"<name>(.*?)</name>", re.S)
DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)

# The popup HTML carries attributes as a two-column table with human labels
# ("Depth (ft):", "Data Source:"), not machine field names -- so the label side
# has to accept spaces, parentheses and the trailing colon.
FIELD_RE = re.compile(r"<td>\s*([^<]+?)\s*</td>\s*<td>\s*([^<]*?)\s*</td>", re.S)


def norm(label):
    """'Depth (ft):' -> 'depth_ft'"""
    s = label.strip().rstrip(":").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def placemarks(kml_text):
    for chunk in kml_text.split("<Placemark")[1:]:
        m = COORD_RE.search(chunk)
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        fields = {norm(k): v for k, v in FIELD_RE.findall(chunk)}
        yield lon, lat, fields, chunk


def main():
    lake = shape(json.loads(LAKE.read_text())["geometry"])
    region = prep(lake.buffer(PAD_DEG))
    minx, miny, maxx, maxy = lake.buffer(PAD_DEG).bounds

    found = []
    scanned = 0
    for kmz in sorted(KMZ_DIR.glob("*.kmz")):
        if not zipfile.is_zipfile(kmz):
            continue  # a couple of tile URLs return the site's 404 page
        scanned += 1
        with zipfile.ZipFile(kmz) as z:
            for name in z.namelist():
                if not name.lower().endswith(".kml"):
                    continue
                text = z.read(name).decode("utf-8", "replace")
                for lon, lat, fields, chunk in placemarks(text):
                    if not (minx <= lon <= maxx and miny <= lat <= maxy):
                        continue
                    if not region.contains(Point(lon, lat)):
                        continue
                    found.append((lon, lat, fields, chunk))

    print(f"scanned {scanned} KMZ tiles; {len(found)} placemarks inside the lake")
    if not found:
        raise SystemExit("no soundings found for this lake")

    keys = {}
    for _, _, f, _ in found[:200]:
        for k in f:
            keys[k] = keys.get(k, 0) + 1
    print(f"attribute keys seen: {sorted(keys)}")

    feats = []
    depths = []
    for lon, lat, f, chunk in found:
        raw = f.get("depth_ft")
        try:
            depth = float(raw)
        except (TypeError, ValueError):
            continue
        # Guard against sentinel values; this lake's surveyed max is 86 ft.
        if depth < 0 or depth > 300:
            continue
        depths.append(depth)
        props = {"depth_ft": depth}
        for extra in ("surface_elevation_ft", "data_source"):
            if f.get(extra):
                props[extra] = f[extra]
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            }
        )

    if not feats:
        # Dump one placemark so the attribute layout can be inspected instead of
        # guessing at field names.
        print("\nno DEPTH field parsed. sample placemark:\n")
        print(found[0][3][:1500])
        raise SystemExit("could not parse depth attribute")

    import statistics

    print(f"\nparsed {len(feats)} soundings")
    print(f"  depth ft: min={min(depths):.0f} median={statistics.median(depths):.0f} max={max(depths):.0f}")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
