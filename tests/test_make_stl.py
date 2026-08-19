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
    crop_to_mask,
    crop_to_water,
    is_closed,
    masked_solid_triangles,
    mesh_volume_mm3,
    shore_mask,
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


class TestTrimToShore:
    """The trimmed outline is where a silent hole is most likely to get in.

    `solid_triangles` walks one rectangular perimeter and is hard to get wrong.
    The masked version raises walls per cell against an arbitrary boundary, so
    closure is a property of the algorithm rather than of the shape, and it is
    worth proving on the shapes that would break it: a ring, an island, a
    single cell, a diagonal staircase.
    """

    def test_a_trimmed_solid_is_still_closed_and_wound_outward(self):
        d = basin()
        m = build_surface(d, 25.0, 90.0, 10.0, 3.0)
        mask = shore_mask(d, grid_m=25.0, shore_m=25.0)
        tris = masked_solid_triangles(m.z, m.cell_mm, mask)
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) > 0

    def test_trimming_removes_material(self):
        """The point of the trim. If the volume did not drop it did nothing."""
        d = basin()
        m = build_surface(d, 25.0, 90.0, 10.0, 3.0)
        mask = shore_mask(d, grid_m=25.0, shore_m=0.0)
        trimmed = mesh_volume_mm3(masked_solid_triangles(m.z, m.cell_mm, mask))
        whole = mesh_volume_mm3(solid_triangles(m.z, m.cell_mm))
        assert 0 < trimmed < whole

    def test_a_full_mask_reproduces_the_rectangular_solid(self):
        """Masking everything in must agree with the mesher it replaces, or the
        two paths have drifted and only one of them is ever tested."""
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        full = np.ones(m.z.shape, bool)
        a = mesh_volume_mm3(masked_solid_triangles(m.z, m.cell_mm, full))
        b = mesh_volume_mm3(solid_triangles(m.z, m.cell_mm))
        assert a == pytest.approx(b)

    def test_a_ring_keeps_its_inner_wall(self):
        """A shape with a hole in it is the case per-cell walls exist for: the
        inner boundary needs walls too, and a perimeter walk would miss it."""
        z = np.full((9, 9), 5.0)
        mask = np.ones((9, 9), bool)
        mask[3:6, 3:6] = False
        tris = masked_solid_triangles(z, 2.0, mask)
        assert is_closed(tris)
        vol = mesh_volume_mm3(tris)
        assert vol == pytest.approx((8 * 8 - 4 * 4) * 2.0 * 2.0 * 5.0)

    def test_one_cell_is_a_box(self):
        z = np.full((2, 2), 3.0)
        mask = np.ones((2, 2), bool)
        tris = masked_solid_triangles(z, 2.0, mask)
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) == pytest.approx(2.0 * 2.0 * 3.0)

    def test_a_diagonal_staircase_stays_closed(self):
        """Every wall meets two others at a corner here, which is where an
        off-by-one in the neighbour lookup shows up as an open edge."""
        z = np.full((8, 8), 4.0)
        yy, xx = np.mgrid[0:8, 0:8]
        tris = masked_solid_triangles(z, 1.5, yy >= xx)
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) > 0

    def test_the_shell_floor_survives_the_trim(self):
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        mask = shore_mask(basin(), grid_m=25.0, shore_m=25.0)
        floor = np.clip(m.z - 1.5, 0.0, None)
        tris = masked_solid_triangles(m.z, m.cell_mm, mask, floor)
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) > 0


class TestShoreMask:
    def test_the_band_grows_with_shore_m(self):
        d = basin()
        tight = shore_mask(d, grid_m=25.0, shore_m=0.0)
        loose = shore_mask(d, grid_m=25.0, shore_m=50.0)
        assert loose.sum() > tight.sum()
        assert (loose | tight).sum() == loose.sum()      # the band only grows

    def test_an_island_is_land_not_a_hole(self):
        """An island is surrounded by water and would otherwise be punched
        clean through the print."""
        d = basin(ny=11, nx=11, deep=10.0)
        d[5, 5] = NAN                                     # an island mid-lake
        mask = shore_mask(d, grid_m=25.0, shore_m=0.0)
        assert mask[5, 5]

    def test_a_separate_pond_is_dropped(self):
        """Two water bodies would slice as two objects, and the little one
        prints detached, gets knocked over, and sticks to the nozzle."""
        d = np.full((14, 14), NAN)
        d[2:8, 2:8] = 5.0                                 # the lake
        d[11:13, 11:13] = 2.0                             # a pond, not this lake
        mask = shore_mask(d, grid_m=25.0, shore_m=0.0)
        assert mask[3, 3]
        assert not mask[12, 12]

    def test_crop_to_mask_trims_every_field_together(self):
        mask = np.zeros((10, 12), bool)
        mask[3:7, 5:9] = True
        other = np.arange(120.0).reshape(10, 12)
        m2, o2, i0, j0 = crop_to_mask(mask, other)
        assert (i0, j0) == (3, 5)
        assert m2.shape == o2.shape == (4, 4)
        assert m2.all()
        assert o2[0, 0] == other[3, 5]


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


class TestMarkers:
    def test_a_polygon_feature_reduces_to_its_centroid(self, tmp_path):
        """Buildings arrive as rings. At 25 m per cell a 15 m footprint cannot be
        drawn, so it has to become a point -- and the point has to be inside it."""
        import make_stl

        gj = {"type": "FeatureCollection", "features": [{
            "properties": {"kind": "building"},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]},
        }]}
        path = tmp_path / "s.geojson"
        path.write_text(__import__("json").dumps(gj))
        pts = make_stl._points(path, {"building"})
        assert len(pts) == 1
        assert pts[0][0] == pytest.approx(0.8, abs=0.5)   # inside the ring
        assert pts[0][1] == pytest.approx(0.8, abs=0.5)

    def test_kinds_that_were_not_asked_for_are_skipped(self, tmp_path):
        import make_stl

        gj = {"type": "FeatureCollection", "features": [
            {"properties": {"kind": "pier"},
             "geometry": {"type": "Point", "coordinates": [1, 1]}},
            {"properties": {"kind": "building"},
             "geometry": {"type": "Point", "coordinates": [2, 2]}},
        ]}
        path = tmp_path / "s.geojson"
        path.write_text(__import__("json").dumps(gj))
        assert make_stl._points(path, {"pier"}) == [(1.0, 1.0)]

    def test_a_marker_off_the_edge_is_dropped_not_wrapped(self):
        """Negative indices would wrap to the far side of the array and put a
        camp on the opposite shore."""
        import make_stl

        m = build_surface(basin(), 25.0, 60.0, 10.0, 2.0)
        meta = {"lon0": 0.0, "lat0": 0.0, "dlon": 1.0, "dlat": 1.0}
        before = m.z.copy()
        assert make_stl.raise_markers(m, [(-5.0, -5.0), (99.0, 99.0)],
                                      meta, 1.0, 1.0) == 0
        assert np.array_equal(m.z, before)


class TestSplitScales:
    """Depth and land can be exaggerated independently. Worth testing because
    the failure is silent: the object still prints, and nothing about it says
    the two halves are no longer comparable."""

    def _two_sided(self):
        d = np.full((5, 5), NAN)
        d[2, 2] = 10.0                      # 10 m of water
        land = np.zeros((5, 5))
        land[0, 0] = 10.0                   # and 10 m of hill
        return d, land

    def test_equal_exaggerations_give_equal_relief(self):
        d, land = self._two_sided()
        m = build_surface(d, 25.0, 50.0, 4.0, 1.0, land_m=land, land_exag=4.0)
        below = m.z[1, 1] - m.z[2, 2]       # waterline down to the deep cell
        above = m.z[0, 0] - m.z[1, 1]
        assert below == pytest.approx(above)

    def test_splitting_them_scales_each_side_on_its_own(self):
        d, land = self._two_sided()
        m = build_surface(d, 25.0, 50.0, 8.0, 1.0, land_m=land, land_exag=2.0)
        below = m.z[1, 1] - m.z[2, 2]
        above = m.z[0, 0] - m.z[1, 1]
        assert below == pytest.approx(above * 4)
        assert m.mm_per_m == pytest.approx(m.mm_per_m_land * 4)

    def test_land_exaggeration_left_unset_follows_the_depth_one(self):
        d, land = self._two_sided()
        m = build_surface(d, 25.0, 50.0, 7.0, 1.0, land_m=land)
        assert m.mm_per_m_land == pytest.approx(m.mm_per_m)

    def test_the_waterline_stays_put_whatever_the_scales(self):
        """Both sides are measured from the water plane, so a shoreline cell
        must land at the same height no matter how either slider moves."""
        d, land = self._two_sided()
        a = build_surface(d, 25.0, 50.0, 3.0, 1.0, land_m=land, land_exag=3.0)
        b = build_surface(d, 25.0, 50.0, 3.0, 1.0, land_m=land, land_exag=25.0)
        shore_a = a.z[1, 1] - a.z.min()      # above the deepest point, which is
        shore_b = b.z[1, 1] - b.z.min()      # what the base thickness sits under
        assert shore_a == pytest.approx(shore_b)


class TestTerracing:
    def test_no_step_leaves_the_field_alone(self):
        from make_stl import terrace
        d = np.array([1.0, 2.5, 7.3])
        assert np.array_equal(terrace(d, 0.0), d)

    def test_values_land_on_whole_steps_of_feet(self):
        from make_stl import FT_PER_M, terrace
        d = np.array([4.0, 11.0, 26.0, 70.0]) / FT_PER_M     # feet in, metres held
        out = terrace(d, 10.0) * FT_PER_M
        assert np.allclose(out, [0.0, 10.0, 20.0, 70.0])

    def test_it_floors_rather_than_rounds(self):
        """A 9 ft sounding on a 10 ft terrace would print as deeper than it was
        measured. Floor keeps every terrace a depth the water actually reaches."""
        from make_stl import FT_PER_M, terrace
        d = np.array([9.0, 9.9, 19.5]) / FT_PER_M
        out = terrace(d, 10.0) * FT_PER_M
        assert np.allclose(out, [0.0, 0.0, 10.0])

    def test_terracing_never_deepens_a_cell(self):
        from make_stl import terrace
        rng = np.random.default_rng(7)
        d = rng.uniform(0, 25, 500)
        assert np.all(terrace(d, 5.0) <= d + 1e-12)

    def test_lake_steps_do_not_terrace_the_land(self):
        """Land steps are a separate switch: bare-earth lidar is rough
        everywhere and banding it reads as noise, not as contours."""
        d = np.full((4, 4), NAN)
        d[1:3, 1:3] = 6.0
        land = np.full((4, 4), 5.0)
        land[0, 0] = 5.4
        a = build_surface(d, 25.0, 40.0, 4.0, 1.0, land_m=land, step_ft=10.0)
        b = build_surface(d, 25.0, 40.0, 4.0, 1.0, land_m=land)
        assert a.z[0, 0] - a.z[0, 1] == pytest.approx(b.z[0, 0] - b.z[0, 1])

    def test_asking_for_land_steps_does_terrace_it(self):
        d = np.full((4, 4), NAN)
        d[1:3, 1:3] = 6.0
        land = np.full((4, 4), 5.0)
        land[0, 0] = 5.4                       # under a 10 ft step above its neighbour
        m = build_surface(d, 25.0, 40.0, 4.0, 1.0, land_m=land,
                          step_ft=10.0, land_step_ft=10.0)
        assert m.z[0, 0] == pytest.approx(m.z[0, 1])


class TestFilament:
    """The estimate exists to answer 'is this a 40 g print or a 400 g print'
    before eight hours of it happen, so its bounds have to be right."""

    def _box(self):
        z = np.full((30, 30), 20.0)
        return solid_triangles(z, 4.0), mesh_volume_mm3(solid_triangles(z, 4.0))

    def test_full_infill_uses_the_whole_volume(self):
        from make_stl import filament_estimate
        tris, vol = self._box()
        est = filament_estimate(tris, vol, infill=1.0)
        assert est["used_cm3"] == pytest.approx(vol / 1000)

    def test_zero_infill_still_prints_the_shell(self):
        from make_stl import filament_estimate
        tris, vol = self._box()
        est = filament_estimate(tris, vol, infill=0.0)
        assert 0 < est["used_cm3"] < vol / 1000
        assert est["shell_share"] == pytest.approx(1.0)

    def test_a_thin_plate_cannot_use_more_than_it_is(self):
        """Shell thickness exceeds the object, and a naive sum would claim more
        plastic than the part contains."""
        from make_stl import filament_estimate
        z = np.full((20, 20), 0.6)          # thinner than two solid skins
        tris = solid_triangles(z, 3.0)
        vol = mesh_volume_mm3(tris)
        est = filament_estimate(tris, vol, infill=0.15)
        assert est["used_cm3"] <= vol / 1000 + 1e-9

    def test_mass_and_length_agree_with_the_volume(self):
        from make_stl import filament_estimate
        tris, vol = self._box()
        est = filament_estimate(tris, vol, infill=0.2)
        assert est["grams"] == pytest.approx(est["used_cm3"] * 1.24)
        area_mm2 = np.pi * (1.75 / 2) ** 2
        assert est["metres"] == pytest.approx(est["used_cm3"] * 1000 / area_mm2 / 1000)

    def test_more_infill_never_uses_less(self):
        from make_stl import filament_estimate
        tris, vol = self._box()
        prev = 0.0
        for f in (0.0, 0.1, 0.25, 0.5, 1.0):
            g = filament_estimate(tris, vol, infill=f)["grams"]
            assert g >= prev
            prev = g


class TestHollowShell:
    def test_a_shell_is_closed_and_wound_outward(self):
        from make_stl import shell_floor
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        tris = solid_triangles(m.z, m.cell_mm, shell_floor(m.z, 1.2))
        assert is_closed(tris)
        assert mesh_volume_mm3(tris) > 0

    def test_a_floating_shell_holds_its_thickness(self):
        """Every cell is well above the bed, so the volume is simply area times
        thickness -- if it is not, the two surfaces are not parallel."""
        from make_stl import shell_floor
        z = np.full((10, 12), 20.0)
        tris = solid_triangles(z, 2.0, shell_floor(z, 1.5))
        assert mesh_volume_mm3(tris) == pytest.approx(9 * 2.0 * 11 * 2.0 * 1.5)

    def test_the_shell_lands_on_the_bed_where_the_surface_is_thin(self):
        """Otherwise the deepest part of the lake floats and prints in mid-air."""
        from make_stl import shell_floor
        z = np.array([[0.5, 4.0], [9.0, 0.9]])
        assert np.array_equal(shell_floor(z, 1.6), [[0.0, 2.4], [7.4, 0.0]])

    def test_hollowing_removes_most_of_the_volume(self):
        from make_stl import shell_floor
        m = build_surface(basin(), 25.0, 90.0, 10.0, 3.0)
        solid = mesh_volume_mm3(solid_triangles(m.z, m.cell_mm))
        hollow = mesh_volume_mm3(solid_triangles(m.z, m.cell_mm, shell_floor(m.z, 1.2)))
        assert hollow < solid * 0.5

    def test_supports_are_counted_where_the_ceiling_is_flat(self):
        """A flat underside high off the bed is the worst case and must not
        estimate as free."""
        from make_stl import support_estimate
        flat = np.full((20, 20), 30.0)
        steep = np.tile(np.arange(20) * 30.0, (20, 1))    # 30 mm per cell: near vertical
        assert support_estimate(flat, 1.0)["area_share"] == pytest.approx(1.0)
        assert support_estimate(steep, 1.0)["grams"] < support_estimate(flat, 1.0)["grams"]
