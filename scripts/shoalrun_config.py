"""Per-lake configuration and the geometry helpers that make this work anywhere.

The first version of this pipeline was hardcoded to one lake in Maine: UTM zone
19N in five files, and a "trusted months = July, August" rule tuned to 45.7 N.
Neither travels. This module removes both assumptions so the detector runs on any
lake on Earth.

What still does NOT generalise, and cannot be fixed here:
  - The depth soundings are Maine DIFW only (`extract_soundings.py`). Maine has
    surveyed thousands of its lakes; most states have nothing comparable.
  - Clear water is required. Green-band shoal detection sees the bottom through
    the water column, so a turbid or algal lake will produce confident nonsense.
  - 10 m pixels need a lake big enough to have them. Below roughly 0.5 km2 there
    are too few water pixels for the per-scene Otsu threshold to find two modes.
"""

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- the lake ---------------------------------------------------------------

LAKE_NAME = "Millinocket Lake"
OSM_RELATION = 2851498          # the one NEOC sits on
OSM_SEARCH_BBOX = (45.5, -69.4, 46.2, -68.4)   # south, west, north, east

# --- projection -------------------------------------------------------------


def utm_epsg(lon, lat):
    """EPSG code for the UTM zone containing this point.

    Hardcoding a zone silently corrupts any lake outside it: reprojection still
    "succeeds", it just puts the grid in the wrong place, and the shoreline mask
    lands on forest. Derived per lake instead.
    """
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def lake_crs():
    """UTM EPSG for the configured lake, from its own centroid."""
    from shapely.geometry import shape

    geom = shape(json.loads((ROOT / "data" / "lake.geojson").read_text())["geometry"])
    c = geom.centroid
    return f"EPSG:{utm_epsg(c.x, c.y)}"


# --- sun elevation ----------------------------------------------------------

# Measured on this lake: per-scene water fraction has sd ~1.1% in Jul/Aug but
# 8.3% in September and 14.3% in October, and one October scene read the lake as
# 48% dry. The underlying cause is not the calendar, it is sun elevation -- low
# sun raises specular response over water until it swamps the water index. Months
# were a proxy that only holds at this latitude; elevation is the real variable
# and works from the equator to the Arctic.
#
# Threshold derived from this lake's own scenes. Measured elevation ranges:
#   Jun 63.8-64.6   Jul 59.9-64.0   Aug 51.9-59.0  (stable, sd ~1.1%)
#   Sep 40.1-50.6   Oct 29.2-40.1                  (sd 8.3% / 14.3%)
# August's minimum sits just above September's maximum, so 51 deg splits the
# stable scenes from the unstable ones exactly -- and it reinstates June, which
# the old hardcoded (7, 8) month list dropped for no reason other than that it
# was written by hand.
MIN_SUN_ELEVATION_DEG = 51.0


def solar_elevation(dt, lat, lon):
    """Solar elevation in degrees for a UTC datetime at a location.

    NOAA low-precision algorithm -- good to ~0.1 deg, which is far finer than the
    threshold it feeds. Computed analytically rather than read from scene
    metadata so it also works on stacks fetched before this existed.
    """
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600

    frac = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)

    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(frac)
        - 0.032077 * math.sin(frac)
        - 0.014615 * math.cos(2 * frac)
        - 0.040849 * math.sin(2 * frac)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(frac)
        + 0.070257 * math.sin(frac)
        - 0.006758 * math.cos(2 * frac)
        + 0.000907 * math.sin(2 * frac)
        - 0.002697 * math.cos(3 * frac)
        + 0.00148 * math.sin(3 * frac)
    )

    time_offset = eqtime + 4 * lon           # minutes; dt is UTC
    true_solar_min = hour * 60 + time_offset
    hour_angle = math.radians(true_solar_min / 4 - 180)

    lat_r = math.radians(lat)
    cos_zenith = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(
        hour_angle
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))
