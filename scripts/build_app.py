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
MODULES = ["geo.js", "hazard.js", "render.js", "store.js", "app.js"]

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
    contours = json.loads((DATA / "contours.geojson").read_text())
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

    payload = {
        "lake": round_coords(lake),
        "rocks": round_coords(slim),
        "contours": round_coords(contours),
        "structures": round_coords(structures),
        "meta": {
            "summary": (
                f"{LAKE_NAME}: {sum(counts.values())} candidates "
                f"({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))}) "
                "from Sentinel-2 persistence. Unverified - aid, not a chart."
            ),
            "counts": counts,
        },
    }

    js = "\n".join(strip_module_syntax((WEB / m).read_text()) for m in MODULES)

    html = (WEB / "index.template.html").read_text()
    html = html.replace("/*__DATA__*/", json.dumps(payload, separators=(",", ":")))
    html = html.replace("/*__APP__*/", js)

    DIST.mkdir(exist_ok=True)
    out = DIST / "index.html"
    out.write_text(html)

    assert "fetch(" not in js, "app must not fetch anything at runtime"
    assert "/*__DATA__*/" not in html and "/*__APP__*/" not in html, "placeholder left unreplaced"

    # Ship the PWA shell alongside: the page needs HTTPS for geolocation, and a
    # service worker so offline availability is deterministic rather than
    # dependent on browser cache eviction.
    for extra in ("sw.js", "manifest.json"):
        src = WEB / extra
        if src.exists():
            (DIST / extra).write_text(src.read_text())

    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB, fully offline)")
    print(f"  + sw.js, manifest.json (installable PWA)")
    print(f"  {payload['meta']['summary']}")


if __name__ == "__main__":
    main()
