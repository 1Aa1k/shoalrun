"""Tests for merging Maine E911 addresses into the structure layer.

The merge has one job worth testing: draw the camps OSM missed, and do not
double-draw the ones it has. Both failures are visible on the map -- a missing
camp is the gap this whole sweep was about, and a duplicate is a hollow ring
sitting on top of a traced footprint, which reads as two buildings.

The dedupe radius is in metres and the data is in degrees, so the axis scaling
is the part that actually breaks. At 45.7 N a degree of longitude is 0.70 of a
degree of latitude; a test that only moves points north would pass with the
scaling dropped entirely.
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_structures import _centroid, e911_points  # noqa: E402

LAT = 45.75
LON = -68.80
M_LAT = 1.0 / 111_320.0
M_LON = M_LAT / math.cos(math.radians(LAT))


def osm_building(lon=LON, lat=LAT):
    """A tiny square footprint, the shape build_structures emits for a way."""
    d = 5 * M_LAT
    ring = [[lon - d, lat - d], [lon + d, lat - d],
            [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]
    return {"type": "Feature", "properties": {"kind": "building"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def e911_file(tmp_path, points):
    p = tmp_path / "e911.geojson"
    p.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"ADDRESS": addr},
             "geometry": {"type": "Point", "coordinates": [lon, lat]}}
            for lon, lat, addr in points],
    }))
    return p


class TestCentroid:
    def test_a_point_is_its_own_centroid(self):
        assert _centroid({"type": "Point", "coordinates": [LON, LAT]}) == [LON, LAT]

    def test_a_polygon_uses_its_ring(self):
        cx, cy = _centroid(osm_building()["geometry"])
        assert cx == pytest.approx(LON, abs=1e-9)
        assert cy == pytest.approx(LAT, abs=1e-9)

    def test_a_pier_line_averages_its_vertices(self):
        g = {"type": "LineString", "coordinates": [[LON, LAT], [LON + 0.001, LAT]]}
        assert _centroid(g)[0] == pytest.approx(LON + 0.0005)


class TestMerge:
    def test_an_address_with_no_building_is_kept(self, tmp_path):
        out = e911_points([osm_building()],
                          e911_file(tmp_path, [(LON + 0.01, LAT + 0.01, "9 Far Rd")]))
        assert len(out) == 1
        assert out[0]["properties"]["kind"] == "address"
        assert out[0]["properties"]["name"] == "9 Far Rd"
        assert out[0]["properties"]["source"] == "maine-e911"

    def test_an_address_on_top_of_a_building_is_dropped(self, tmp_path):
        assert e911_points([osm_building()],
                           e911_file(tmp_path, [(LON, LAT, "1 Same Rd")])) == []

    def test_the_radius_is_metres_north_and_south(self, tmp_path):
        inside = (LON, LAT + 30 * M_LAT, "in")
        outside = (LON, LAT + 60 * M_LAT, "out")
        out = e911_points([osm_building()], e911_file(tmp_path, [inside, outside]))
        assert [f["properties"]["name"] for f in out] == ["out"]

    def test_the_radius_is_metres_east_and_west_too(self, tmp_path):
        """Without the cos(lat) scaling a 30 m eastward offset is 0.70 of a
        latitude degree-equivalent and lands inside a 40 m circle measured in
        raw degrees -- so this point would be dropped as a duplicate and the
        camp would vanish from the map."""
        out = e911_points([osm_building()],
                          e911_file(tmp_path, [(LON + 60 * M_LON, LAT, "out")]))
        assert [f["properties"]["name"] for f in out] == ["out"]

    def test_a_missing_e911_file_is_not_an_error(self, tmp_path):
        """The repo can be built without ever running the fetch script."""
        assert e911_points([osm_building()], tmp_path / "nope.geojson") == []

    def test_no_osm_structures_means_no_merge(self, tmp_path):
        """Nothing to dedupe against means the dedupe cannot be trusted, and
        emitting every address unchecked would double the shoreline."""
        assert e911_points([], e911_file(tmp_path, [(LON, LAT, "1 Rd")])) == []

    def test_an_address_with_no_street_still_draws(self, tmp_path):
        out = e911_points([osm_building()],
                          e911_file(tmp_path, [(LON + 0.01, LAT, None)]))
        assert len(out) == 1 and "name" not in out[0]["properties"]
