#!/usr/bin/env python3
"""Pull Microsoft's building footprints for this lake.

OSM has 271 structures here. Rural Maine is thinly mapped and the camps are what
people actually navigate by, so what a volunteer happened to trace is not the
same as what is there.

Microsoft ran a model over Bing imagery for the whole country and published the
result under ODbL. It is the same job as detecting buildings in lidar, done by
people who did nothing else, on imagery rather than on a canopy that hides half
the roofs on this shoreline. Maine is 758,999 buildings and 188 MB, which is
worth streaming once to keep a few hundred.

Streamed line by line rather than parsed whole: the file is one FeatureCollection
with one feature per line, and json.load on 188 MB of it costs well over a
gigabyte of memory to end up keeping 0.05% of it.

    .venv/bin/python scripts/fetch_ms_buildings.py

Attribution is required by the licence and is recorded in DATA-LICENSE.md.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
import zipfile
from pathlib import Path

from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "data" / "structures_ms.geojson"

URL = "https://minedbuildings.z5.web.core.windows.net/legacy/usbuildings-v2/Maine.geojson.zip"

# Everything within this far of the lake polygon, in degrees. About 900 m, which
# covers the camp roads set back from the water without dragging in the town.
PAD_DEG = 0.008

# A line in this file is one building's GeoJSON. Anything far longer is not, and
# reading it into memory to find that out is the thing to avoid.
MAX_LINE = 1 << 20


def download(url: str = URL, dest: Path | None = None) -> Path:
    dest = dest or (CACHE / Path(url).name)
    if dest.exists():
        print(f"  using cached {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)                      # never leave a half file looking whole
    print(f"  got {dest.stat().st_size / 1e6:.0f} MB")
    return dest


def lake_bounds(pad: float = PAD_DEG) -> tuple[float, float, float, float]:
    gj = json.loads(LAKE.read_text())
    geom = gj["features"][0]["geometry"] if gj.get("type") == "FeatureCollection" else gj
    west, south, east, north = shape(geom).bounds
    return west - pad, south - pad, east + pad, north + pad


def clip(zip_path: Path, bounds: tuple[float, float, float, float]) -> list[dict]:
    """Every footprint inside the bounds, read a line at a time.

    Lines that do not parse are skipped rather than raised on: this is a 759,000
    line file and the last one is a closing bracket, not a building.
    """
    west, south, east, north = bounds
    keep = []
    seen = 0
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".geojson"))
        with z.open(name) as fh:
            for raw in fh:
                if len(raw) > MAX_LINE:
                    continue
                line = raw.strip().rstrip(b",")
                if not line.startswith(b'{"type":"Feature"'):
                    continue
                seen += 1
                try:
                    feat = json.loads(line)
                    ring = feat["geometry"]["coordinates"][0]
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                # Bounds test on the ring itself, before building any geometry:
                # 759,000 shapely objects is minutes, 759,000 min/max is seconds.
                lons = [p[0] for p in ring]
                lats = [p[1] for p in ring]
                if max(lons) < west or min(lons) > east:
                    continue
                if max(lats) < south or min(lats) > north:
                    continue
                keep.append(feat)
    print(f"  read {seen:,} footprints, kept {len(keep):,} in the lake's bounds")
    return keep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pad-deg", type=float, default=PAD_DEG)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    zip_path = download()
    bounds = lake_bounds(args.pad_deg)
    feats = clip(zip_path, bounds)

    out = {
        "type": "FeatureCollection",
        "meta": {
            "source": "Microsoft US Building Footprints v2 (ODbL), Maine",
            "url": URL,
            "note": "machine-generated from imagery; positions are good, "
                    "outlines are approximate",
        },
        "features": [
            {"type": "Feature",
             "properties": {"kind": "building", "detected": True,
                            "source": "ms-buildings"},
             "geometry": f["geometry"]}
            for f in feats
        ],
    }
    args.out.write_text(json.dumps(out))
    print(f"wrote {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB, "
          f"{len(feats):,} buildings)")


if __name__ == "__main__":
    main()
