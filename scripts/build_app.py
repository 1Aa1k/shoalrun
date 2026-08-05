"""Bundle the app into one self-contained HTML file.

Everything is inlined -- data, modules, styles -- because the target device is a
phone on a lake with no cell service. A build that leaves any fetch() in it is a
build that works in the driveway and fails on the water.

The ES modules are concatenated in dependency order with their import/export
lines stripped, which is enough for this handful of files and avoids pulling a
bundler into the toolchain.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shoalrun_config import LAKE_NAME

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
DIST = ROOT / "dist"

# Dependency order matters: geo defines what hazard and render consume.
MODULES = ["geo.js", "depth.js", "hazard.js", "render.js", "store.js", "app.js"]

# The 3D viewer is a separate page on purpose. An orbiting camera is the wrong
# control surface for someone at the helm, and keeping it out of the map page
# means it cannot be reached by accident under way. It shares geo and depth so
# both views read the same surface.
VIEWER_MODULES = ["geo.js", "depth.js", "viewer3d.js"]

IMPORT_RE = re.compile(r"^\s*import\s+.*?;\s*$", re.M | re.S)
EXPORT_RE = re.compile(r"^\s*export\s+(?=(const|let|var|function|class|async))", re.M)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{[^}]*\}\s*;\s*$", re.M)


def strip_module_syntax(src):
    src = IMPORT_RE.sub("", src)
    src = EXPORT_LIST_RE.sub("", src)
    src = EXPORT_RE.sub("", src)
    return src


def round_coords(obj, nd=5):
    """Trim coordinate precision. 5 dp is ~1 m at this latitude, which is finer
    than both the GPS fix and the 10 m detection grid -- carrying 14 digits just
    inflates the file the phone has to hold."""
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, list):
        return [round_coords(v, nd) for v in obj]
    if isinstance(obj, dict):
        return {k: round_coords(v, nd) for k, v in obj.items()}
    return obj


def main():
    lake = json.loads((DATA / "lake.geojson").read_text())
    # Prefer the NAIP-verified set when it exists: an independent 0.3 m sensor
    # outranks a 10 m inference about the same spot.
    # Merged layer is the authority: NAIP 1 m detections + Sentinel + the
    # human-mapped rocks that no imagery method produces on its own.
    for cand in ("hazards.geojson", "verified.geojson", "rocks.geojson"):
        path = DATA / cand
        if path.exists():
            rocks = json.loads(path.read_text())
            break
    # The depth surface ships as a grid, not as pre-cut contour lines. That puts
    # the contour interval on a slider instead of in this script, feeds the 3D
    # viewer the identical surface the 2D lines come from, and is smaller than
    # the 10 ft contours alone were.
    depth = json.loads((DATA / "depth_grid.json").read_text())
    soundings = json.loads((DATA / "soundings.geojson").read_text())
    sp = DATA / "structures.geojson"
    structures = json.loads(sp.read_text()) if sp.exists() else {"features": []}

    # Rocks ship as centroids only: the app alerts on proximity to a point, and
    # the full outlines would multiply the payload for no navigational gain.
    slim = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": f["properties"], "geometry": None}
            for f in rocks["features"]
        ],
    }

    coords = []
    def walk(c):
        if isinstance(c[0], (int, float)):
            coords.append(c)
        else:
            for x in c:
                walk(x)
    walk(lake["geometry"]["coordinates"])
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    lake["bbox"] = [min(lons), min(lats), max(lons), max(lats)]

    # Count by verified verdict, not by the unverified detector class -- the
    # summary a user reads should reflect what survived checking.
    counts = {}
    for f in rocks["features"]:
        counts[f["properties"].get("class", "?")] = counts.get(f["properties"].get("class", "?"), 0) + 1

    # Spot soundings as the chart theme draws them -- the actual 1954 numbers,
    # not the interpolated surface. On a real chart the printed sounding is the
    # measurement and the contour is the inference, and keeping that distinction
    # visible is the honest way to show data this sparse.
    spot = [
        [round(f["geometry"]["coordinates"][0], 5),
         round(f["geometry"]["coordinates"][1], 5),
         int(f["properties"]["depth_ft"])]
        for f in soundings["features"]
    ]

    payload = {
        "lake": round_coords(lake),
        "rocks": round_coords(slim),
        "depth": depth,
        "soundings": spot,
        "structures": round_coords(structures),
        "meta": {
            "summary": (
                f"{LAKE_NAME}: {sum(counts.values())} candidates "
                f"({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))}) "
                "from NAIP aerial persistence (6 flights 2011-2023, quality-weighted) "
                "plus Sentinel-2 and hand-mapped rock. Unverified - aid, not a chart."
            ),
            "counts": counts,
        },
    }

    data_json = json.dumps(payload, separators=(",", ":"))
    DIST.mkdir(exist_ok=True)

    def bundle(template, modules, dest):
        js = "\n".join(strip_module_syntax((WEB / m).read_text()) for m in modules)
        html = (WEB / template).read_text()
        html = html.replace("/*__DATA__*/", data_json)
        html = html.replace("/*__APP__*/", js)
        assert "fetch(" not in js, f"{dest} must not fetch anything at runtime"
        assert (
            "/*__DATA__*/" not in html and "/*__APP__*/" not in html
        ), f"{dest}: placeholder left unreplaced"
        path = DIST / dest
        path.write_text(html)
        return path

    out = bundle("index.template.html", MODULES, "index.html")
    out3d = bundle("viewer3d.template.html", VIEWER_MODULES, "3d.html")

    # Ship the PWA shell alongside: the page needs HTTPS for geolocation, and a
    # service worker so offline availability is deterministic rather than
    # dependent on browser cache eviction.
    for extra in ("sw.js", "manifest.json"):
        src = WEB / extra
        if src.exists():
            (DIST / extra).write_text(src.read_text())

    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB, fully offline)")
    print(f"wrote {out3d}  ({out3d.stat().st_size / 1024:.0f} KB, 3D depth viewer)")
    print(f"  + sw.js, manifest.json (installable PWA)")
    print(f"  {payload['meta']['summary']}")


if __name__ == "__main__":
    main()
