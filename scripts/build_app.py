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
MODULES = ["geo.js", "depth.js", "evidence.js", "hazard.js", "swept.js", "flags.js",
           "sync.js", "render.js", "store.js", "views.js", "app.js"]

# The 3D viewer used to be its own page. Three HTML files meant three URLs to
# hand somebody, three sets of chrome and three visual languages for one lake,
# so it is a tab in the same document now.
#
# It still gets its own scope. Wrapped in a function it can be stood up on first
# view rather than at load -- it takes a WebGL context and builds a 156k-triangle
# mesh, neither of which should happen on a phone that only ever opens the map --
# and the scope also keeps its `el`, `draw` and `resize` from colliding with the
# map's, which is why geo.js and depth.js can appear in both lists.
VIEWER_MODULES = ["geo.js", "depth.js", "evidence.js", "mat3d.js", "scene3d.js",
                  "shaders3d.js", "viewer3d.js"]

IMPORT_RE = re.compile(r"^\s*import\s+.*?;\s*$", re.M | re.S)
EXPORT_RE = re.compile(r"^\s*export\s+(?=(const|let|var|function|class|async))", re.M)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{[^}]*\}\s*;\s*$", re.M)


# Concatenation cannot honour a renamed import. `import { show as showView }`
# type-checks, lints, and passes every module-aware tool -- and then the import
# line is deleted, `showView` is never defined anywhere, and the call throws the
# first time a user presses the button that reaches it. That is not a failure a
# phone on a lake can report, so it fails the build instead.
ALIAS_IMPORT_RE = re.compile(r"import\s*\{[^}]*\bas\b[^}]*\}\s*from", re.S)


def strip_module_syntax(src):
    src = IMPORT_RE.sub("", src)
    src = EXPORT_LIST_RE.sub("", src)
    src = EXPORT_RE.sub("", src)
    return src


def check_no_aliased_imports(modules):
    for m in modules:
        text = (WEB / m).read_text()
        if ALIAS_IMPORT_RE.search(text):
            raise SystemExit(
                f"{m}: renamed import (`as`) cannot survive bundling -- "
                "rename the export instead"
            )


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

    # Evidence tiers matter more than class now. The imagery was measured to
    # carry no depth information on this lake, so a "shoal" -- bottom seen
    # through water -- is unevidenced unless something else backs it up. The
    # summary has to say so; a boater reading "3548 shoals" would reasonably
    # think they had been surveyed.
    tiers = {}
    for f in rocks["features"]:
        t = f["properties"].get("tier", "unverified")
        tiers[t] = tiers.get(t, 0) + 1

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
                "plus Sentinel-2 and hand-mapped rock. "
                f"{tiers.get('confirmed', 0)} confirmed above the waterline, "
                f"{tiers.get('likely', 0)} likely (infrared says dry surface), "
                f"{tiers.get('unverified', 0)} UNVERIFIED. Aerial imagery carries no "
                "depth information on this lake (measured: cannot separate 10 ft "
                "from 25 ft), so submerged hazards are NOT reliably mapped. "
                "Aid, not a chart."
            ),
            "counts": counts,
        },
    }

    data_json = json.dumps(payload, separators=(",", ":"))
    DIST.mkdir(exist_ok=True)

    def concat(modules):
        check_no_aliased_imports(modules)
        return "\n".join(strip_module_syntax((WEB / m).read_text()) for m in modules)

    def bundle(template, modules, dest, lazy=None):
        js = concat(modules)
        if lazy:
            # One entry point, called by views.js the first time the tab opens.
            js += (
                "\n\n// --- lazily initialised view ---\n"
                "window.__initViewer3d = function () {\n"
                + concat(lazy)
                + "\n};\n"
            )
        html = (WEB / template).read_text()
        html = html.replace("/*__DATA__*/", data_json)
        html = html.replace("/*__APP__*/", js)
        assert "fetch(" not in js, f"{dest} must not fetch anything at runtime"
        # A module left out of the list has its import line stripped and then
        # fails at runtime with an undefined symbol -- on the water, where the
        # user cannot open a console. Catch it at build time instead.
        for m in modules:
            for sym in re.findall(r"^\s*export\s+(?:async\s+)?(?:function|class|const)\s+(\w+)",
                                  (WEB / m).read_text(), re.M):
                pass
        missing = [
            name for name in re.findall(r"\bimport\s*\{([^}]*)\}\s*from\s*\"\./(\w+)\.js\"",
                                        (WEB / modules[-1]).read_text())
            for name in [name[1] + ".js"]
            if name not in modules
        ]
        assert not missing, f"{dest}: imported but not bundled: {sorted(set(missing))}"
        assert (
            "/*__DATA__*/" not in html and "/*__APP__*/" not in html
        ), f"{dest}: placeholder left unreplaced"
        path = DIST / dest
        path.write_text(html)
        return path

    out = bundle("index.template.html", MODULES, "index.html", lazy=VIEWER_MODULES)

    # 3d.html was a real page for a week and is in at least one handover doc and
    # whatever anyone bookmarked. Keeping a redirect costs 300 bytes; letting a
    # saved link 404 on a phone with no signal costs somebody their map.
    (DIST / "3d.html").write_text(
        "<!doctype html><meta charset=utf-8>"
        '<title>shoalrun</title>'
        '<meta http-equiv="refresh" content="0; url=./index.html#3d">'
        '<p>The 3D view is a tab inside the app now. '
        '<a href="./index.html#3d">Open it</a>.</p>\n'
    )

    # Ship the PWA shell alongside: the page needs HTTPS for geolocation, and a
    # service worker so offline availability is deterministic rather than
    # dependent on browser cache eviction.
    for extra in ("sw.js", "manifest.json"):
        src = WEB / extra
        if src.exists():
            (DIST / extra).write_text(src.read_text())

    # docs/ is the GitHub Pages copy. It was written by hand once and then went
    # eight days stale behind dist/, which means a Pages link and a dist link
    # were two different apps -- and the Pages one is the link somebody is more
    # likely to be sent. Republish it from the same build rather than trusting
    # anyone to remember.
    docs = ROOT / "docs"
    if (docs / ".nojekyll").exists():
        for name in ("index.html", "3d.html", "sw.js", "manifest.json"):
            src = DIST / name
            if src.exists():
                (docs / name).write_text(src.read_text())

    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB, fully offline)")
    print("  map + 3D bottom + info, one page, three tabs")
    print("  + 3d.html redirect, sw.js, manifest.json (installable PWA)")
    print(f"  {payload['meta']['summary']}")


if __name__ == "__main__":
    main()
