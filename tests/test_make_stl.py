"""Tests for the printable-solid export.

A bathymetry STL fails silently: a hole in the mesh or an inside-out winding
still opens in a slicer and still prints, just wrong, and the first evidence is
eight hours of filament. So the geometry invariants are tested here rather than
eyeballed in a preview.
"""

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from make_stl import (  # noqa: E402
    build_surface,
    crop_to_water,
    is_closed,
    mesh_volume_mm3,
    solid_triangles,
    write_binary_stl,
)

NAN = float("nan")


def basin(ny=7, nx=9, deep=10.0):
    """A little bowl: land around the edge, deepening toward the middle."""
    d = np.full((ny, nx), NAN)
    yy, xx = np.mgrid[0:ny, 0:nx]
    r = np.hypot(yy - (ny - 1) / 2, xx - (nx - 1) / 2)
    inside = r < min(ny, nx) / 2 - 0.5
    d[inside] = deep * (1 - r[inside] / (min(ny, nx) / 2))
    return d


class TestSurface:
    def test_land_sits_at_the_plane_and_water_hangs_below_it(self):
        m = build_surface(basin(), grid_m=25.0, width_mm=90.0, exag=10.0, base_mm=3.0)
        plane = m.z.max()
        assert m.z.min() == pytest.approx(3.0)          # base under the deepest point
        assert m.z[0, 0] == pytest.approx(plane)        # corner is land

    def test_a_deeper_cell_prints_lower(self):
        d = np.array([[NAN, NAN, NAN], [NAN, 2.0, 8.0], [NAN, NAN, NAN]])
        m = build_surface(d, grid_m=25.0, width_mm=30.0, exag=5.0, base_mm=1.0)
        assert m.z[1, 2] < m.z[1, 1] < m.z[0, 0]

    def test_exaggeration_scales_only_the_vertical(self):
        d = basin()
        a = build_surface(d, 25.0, 90.0, exag=1.0, base_mm=2.0)
        b = build_surface(d, 25.0, 90.0, exag=20.0, base_mm=2.0)
        assert a.cell_mm == pytest.approx(b.cell_mm)
        assert b.mm_per_m == pytest.approx(a.mm_per_m * 20)

    def test_step_changes_resolution_not_scale(self):
        """--step is a file-size knob. If it moved the scale it would quietly
        print a differently-sized lake, which is the one thing a map must not do."""
        d = basin(ny=21, nx=21)
        fine = build_surface(d, 25.0, 100.0, 10.0, 2.0, step=1)
        coarse = build_surface(d, 25.0, 100.0, 10.0, 2.0, step=2)
        span = lambda m: (m.z.shape[1] - 1) * m.cell_mm
        assert span(coarse) == pytest.approx(span(fine), abs=coarse.cell_mm)
        assert coarse.scale_denom == pytest.approx(fine.scale_denom)
        assert coarse.z.size < fine.z.size


class TestSolid:
    def test_the_mesh_is_closed(self):
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        assert is_closed(solid_triangles(m.z, m.cell_mm))

    def test_the_mesh_is_wound_outward(self):
        """Signed volume is positive exactly when every face points out. An
        inside-out solid is closed too, so closure alone does not catch it."""
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        assert mesh_volume_mm3(solid_triangles(m.z, m.cell_mm)) > 0

    def test_volume_is_bounded_by_the_slab_it_is_carved_from(self):
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        tris = solid_triangles(m.z, m.cell_mm)
        ny, nx = m.z.shape
        slab = (nx - 1) * m.cell_mm * (ny - 1) * m.cell_mm * m.z.max()
        vol = mesh_volume_mm3(tris)
        assert 0.5 * slab < vol < slab       # carved, but not carved away

    def test_a_flat_field_is_exactly_a_box(self):
        z = np.full((5, 6), 4.0)
        tris = solid_triangles(z, 2.0)
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) == pytest.approx(5 * 2.0 * 4 * 2.0 * 4.0)

    def test_holes_are_detected(self):
        """The closure check has to be able to fail, or it is decoration."""
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        tris = solid_triangles(m.z, m.cell_mm)
        assert not is_closed(np.delete(tris, 5, axis=0))


class TestStlFile:
    def test_binary_stl_round_trips(self, tmp_path):
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        tris = solid_triangles(m.z, m.cell_mm)
        out = tmp_path / "x.stl"
        write_binary_stl(out, tris, "shoalrun test")

        raw = out.read_bytes()
        assert raw[:13] == b"shoalrun test"
        (count,) = struct.unpack("<I", raw[80:84])
        assert count == len(tris)
        assert len(raw) == 84 + 50 * count

    def test_normals_are_unit_length_and_point_up_on_the_top_face(self, tmp_path):
        z = np.full((4, 4), 5.0)
        out = tmp_path / "flat.stl"
        write_binary_stl(out, solid_triangles(z, 3.0))
        raw = out.read_bytes()
        (count,) = struct.unpack("<I", raw[80:84])
        normals = np.array([
            struct.unpack("<3f", raw[84 + 50 * i: 96 + 50 * i]) for i in range(count)
        ])
        assert np.allclose(np.linalg.norm(normals, axis=1), 1.0)
        # A flat top means the first triangles are the top face, facing +Z.
        assert normals[0][2] == pytest.approx(1.0)


class TestCrop:
    def test_dead_land_margin_is_trimmed_and_the_origin_reported(self):
        d = np.full((10, 12), NAN)
        d[4:6, 5:8] = 3.0
        out, r0, c0 = crop_to_water(d, margin_cells=1)
        assert out.shape == (4, 5)
        assert (r0, c0) == (3, 4)

    def test_a_grid_with_no_water_is_left_alone(self):
        d = np.full((4, 4), NAN)
        out, r0, c0 = crop_to_water(d, margin_cells=2)
        assert out.shape == (4, 4) and (r0, c0) == (0, 0)


class TestSoundingPins:
    def test_a_pin_never_stands_above_the_water_plane(self, tmp_path, monkeypatch):
        """In two feet of water a 0.7 mm pin would poke through the surface and
        read as an island -- the opposite of what the pins are for."""
        import make_stl

        gj = {"type": "FeatureCollection", "features": [
            {"geometry": {"type": "Point", "coordinates": [3.0, 3.0]}},
        ]}
        path = tmp_path / "soundings.geojson"
        path.write_text(__import__("json").dumps(gj))
        monkeypatch.setattr(make_stl, "SOUNDINGS", path)

        d = np.full((6, 6), NAN)
        d[2:4, 2:4] = 0.05                       # ankle-deep
        m = build_surface(d, 25.0, 60.0, 30.0, 2.0)
        plane = float(m.z.max())
        meta = {"lon0": 0.0, "lat0": 0.0, "dlon": 1.0, "dlat": 1.0}
        assert make_stl.mark_soundings(m, meta) == 1
        assert m.z.max() == pytest.approx(plane)
