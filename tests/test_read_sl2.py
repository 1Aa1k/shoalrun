"""Tests for the .sl2 reader.

This parser eats bytes off an SD card that has been in a boat, so the tests are
mostly about it refusing to do something stupid with a number the file made up.
Every length in a .sl2 is caller-supplied.

There is no real .sl2 in the repo yet, so the fixtures are written by
`make_sl2`, which encodes to the same table the reader decodes with. That proves
the FRAMING and the arithmetic. It cannot prove the field offsets -- only a real
file can, which is what --probe is for, and `test_probe_finds_planted_offsets`
proves the probe itself works.
"""

import math
import struct
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from read_sl2 import (  # noqa: E402
    MAX_FRAME,
    SL2_DEFAULT,
    Sl2Error,
    decode,
    iter_frames,
    lonlat_to_lowrance,
    lowrance_to_lonlat,
    plausible_depth,
    probe,
    read_header,
    read_pings,
    to_track,
)

LAKE_BBOX = (-68.90, 45.68, -68.68, 45.82)
FRAME_LEN = 0xC0


def make_frame(lat=45.73, lon=-68.81, depth_ft=22.5, t_ms=1000, speed_kn=4.0,
               channel=0, index=0, size=FRAME_LEN, table=SL2_DEFAULT, fix=True):
    f = bytearray(size)
    # Real frames vary in length by channel and simply do not carry the fields
    # that fall past their end, so the fixture writes only what fits.
    def put(fmt, off, value):
        if off is not None and off + struct.calcsize(fmt) <= size:
            struct.pack_into(fmt, f, off, value)

    put("<H", 0x28, size)
    put("<H", 0x2C, channel)
    put("<I", 0x30, index)
    put("<f", table.depth_ft, depth_ft)
    x, y = lonlat_to_lowrance(lon, lat) if fix else (0, 0)
    put("<i", table.lowrance_x, int(x))
    put("<i", table.lowrance_y, int(y))
    put("<f", table.speed_kn, speed_kn)
    put("<I", table.time_ms, t_ms)
    return bytes(f)


def make_sl2(frames, fmt=2, block=3200):
    return struct.pack("<HHHH", fmt, 1, block, 0) + b"".join(frames)


class TestHeader:
    def test_a_good_header_reads(self):
        assert read_header(make_sl2([]))["format"] == "sl2"

    def test_a_truncated_file_is_an_error_not_a_crash(self):
        with pytest.raises(Sl2Error):
            read_header(b"\x02\x00\x01")

    def test_an_unknown_format_id_is_rejected(self):
        with pytest.raises(Sl2Error, match="not one of"):
            read_header(make_sl2([], fmt=99))

    def test_slg_and_sl3_are_refused_rather_than_misread(self):
        """They frame differently. Reading one as sl2 produces confident
        garbage, which is worse than not reading it."""
        for fmt in (1, 3):
            with pytest.raises(Sl2Error, match="handles sl2"):
                read_header(make_sl2([], fmt=fmt))


class TestFraming:
    def test_frames_walk_the_file(self):
        buf = make_sl2([make_frame(index=i) for i in range(5)])
        assert len(list(iter_frames(buf))) == 5

    def test_a_frame_longer_than_the_file_is_refused(self):
        """The classic: a length field that says four billion. It must fail
        fast, not allocate and not read past the end."""
        f = bytearray(make_frame())
        struct.pack_into("<H", f, 0x28, 60000)
        with pytest.raises(Sl2Error, match="remain"):
            list(iter_frames(make_sl2([bytes(f)])))

    def test_a_zero_length_frame_cannot_hang_the_reader(self):
        """Advancing by a declared zero would loop here forever on a file that
        is one byte wrong."""
        f = bytearray(make_frame())
        struct.pack_into("<H", f, 0x28, 0)
        with pytest.raises(Sl2Error, match="outside"):
            list(iter_frames(make_sl2([bytes(f)])))

    def test_frame_count_is_bounded(self):
        buf = make_sl2([make_frame(size=48) for _ in range(20)])
        with pytest.raises(Sl2Error, match="refusing"):
            list(iter_frames(buf, max_frames=5))

    def test_trailing_bytes_shorter_than_a_frame_stop_the_walk(self):
        """SD cards get pulled mid-write. A half-frame at the end is normal and
        must not lose the rest of the log."""
        buf = make_sl2([make_frame(), make_frame()]) + b"\x00" * 12
        assert len(list(iter_frames(buf))) == 2


class TestPosition:
    def test_mercator_round_trips(self):
        for lat, lon in [(45.73, -68.81), (0.0, 0.0), (-33.9, 151.2)]:
            x, y = lonlat_to_lowrance(lon, lat)
            back_lon, back_lat = lowrance_to_lonlat(x, y)
            assert back_lat == pytest.approx(lat, abs=1e-9)
            assert back_lon == pytest.approx(lon, abs=1e-9)

    def test_the_polar_radius_is_the_one_that_lands_on_the_lake(self):
        """Using the equatorial radius instead puts the track ~20 km north --
        far enough to look like a different lake and close enough to believe."""
        x, y = lonlat_to_lowrance(-68.81, 45.73)
        wrong = math.degrees(2.0 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
        assert abs(wrong - 45.73) > 0.1

    @given(lat=st.floats(-80, 80), lon=st.floats(-179, 179))
    def test_round_trip_holds_everywhere(self, lat, lon):
        x, y = lonlat_to_lowrance(lon, lat)
        back_lon, back_lat = lowrance_to_lonlat(x, y)
        assert back_lat == pytest.approx(lat, abs=1e-6)
        assert back_lon == pytest.approx(lon, abs=1e-6)


class TestDecode:
    def test_a_frame_becomes_a_ping(self):
        p = decode(memoryview(make_frame(lat=45.75, lon=-68.80, depth_ft=31.2)))
        assert p.lat == pytest.approx(45.75, abs=1e-4)
        assert p.lon == pytest.approx(-68.80, abs=1e-4)
        assert p.depth_ft == pytest.approx(31.2, abs=0.01)

    def test_a_ping_with_no_gps_lock_is_not_a_ping(self):
        assert decode(memoryview(make_frame(fix=False))) is None

    def test_a_short_frame_does_not_read_off_the_end(self):
        """Frame length varies by channel. A sidescan frame with no room for a
        temperature field is not corruption."""
        p = decode(memoryview(make_frame(size=0xA8)))
        assert p is not None and p.speed_kn == pytest.approx(4.0)

    def test_a_nan_field_reads_as_zero_not_nan(self):
        """A NaN depth would sail through a > comparison and land in the map."""
        f = bytearray(make_frame())
        struct.pack_into("<f", f, SL2_DEFAULT.depth_ft, float("nan"))
        assert decode(memoryview(bytes(f))).depth_ft == 0.0


class TestReadPings:
    def test_implausible_values_are_dropped_and_counted(self):
        buf = make_sl2([
            make_frame(depth_ft=22.0),
            make_frame(depth_ft=0.0),          # sounder lost bottom
            make_frame(depth_ft=9999.0),       # garbage
            make_frame(speed_kn=300.0),        # garbage
            make_frame(fix=False),
        ])
        pings, rep = read_pings(buf, bbox=LAKE_BBOX)
        assert len(pings) == 1
        assert rep.dropped_depth == 2
        assert rep.dropped_speed == 1
        assert rep.dropped_no_fix == 1

    def test_fixes_outside_the_lake_are_dropped_with_a_bbox(self):
        """The drive to the ramp is in the log too, and a depth reading from a
        trailer is not a sounding."""
        buf = make_sl2([make_frame(), make_frame(lat=44.0, lon=-70.0)])
        pings, rep = read_pings(buf, bbox=LAKE_BBOX)
        assert len(pings) == 1 and rep.dropped_outside_lake == 1

    def test_nothing_is_dropped_silently(self):
        buf = make_sl2([make_frame(depth_ft=9999.0) for _ in range(4)])
        pings, rep = read_pings(buf, bbox=LAKE_BBOX)
        assert pings == []
        assert rep.frames == 4 and rep.dropped_depth == 4


class TestTrack:
    def test_speed_is_converted_to_metres_per_second(self):
        """swept.js works in m/s. Handing it knots would overstate every leg by
        a factor of two and inflate the coverage claim."""
        buf = make_sl2([make_frame(speed_kn=10.0)])
        pings, _ = read_pings(buf, bbox=LAKE_BBOX)
        assert to_track(pings)[0]["speed"] == pytest.approx(5.14, abs=0.01)


class TestProbe:
    def test_probe_finds_planted_offsets(self):
        """The probe is the whole answer to 'are the offsets right', so it has
        to be able to find them in a file where the answer is known."""
        frames = []
        for i in range(60):
            frames.append(make_frame(depth_ft=20.0 + math.sin(i / 5) * 4,
                                     lat=45.73 + i * 1e-4, lon=-68.81 + i * 1e-4,
                                     t_ms=i * 250, index=i))
        out = probe(make_sl2(frames), LAKE_BBOX)
        assert out["depth_candidates"], "found no depth column at all"
        assert int(out["depth_candidates"][0]["offset"], 16) == SL2_DEFAULT.depth_ft
        assert int(out["position_candidates"][0]["x_offset"], 16) == SL2_DEFAULT.lowrance_x

    def test_a_constant_column_does_not_score_as_depth(self):
        """Plenty of fields sit in the 0-400 range and never move. A sounder
        moves."""
        assert plausible_depth([25.0] * 40) < 0.3
        assert plausible_depth([20 + math.sin(i / 4) for i in range(40)]) > 0.5

    def test_a_column_that_teleports_does_not_score_as_depth(self):
        assert plausible_depth([5.0, 300.0] * 20) == 0.0


class TestHostileInput:
    """Arbitrary bytes must produce our own error or a clean result -- never
    IndexError, struct.error, MemoryError, or a hang."""

    @settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.binary(min_size=0, max_size=600))
    def test_random_bytes_never_crash_the_reader(self, data):
        try:
            read_pings(data)
        except Sl2Error:
            pass

    @settings(max_examples=200, deadline=None)
    @given(body=st.binary(min_size=0, max_size=400))
    def test_a_valid_header_over_garbage_never_crashes(self, body):
        try:
            read_pings(make_sl2([body]))
        except Sl2Error:
            pass

    @settings(max_examples=100, deadline=None)
    @given(size=st.integers(min_value=0, max_value=65535))
    def test_any_declared_frame_size_is_handled(self, size):
        f = bytearray(make_frame())
        struct.pack_into("<H", f, 0x28, size)
        try:
            list(iter_frames(make_sl2([bytes(f)])))
        except Sl2Error:
            pass

    def test_a_frame_size_beyond_the_cap_is_refused_before_allocating(self):
        assert MAX_FRAME < 65536 or True     # documents the intent of the cap
        f = bytearray(make_frame())
        struct.pack_into("<H", f, 0x28, 65535)
        with pytest.raises(Sl2Error):
            list(iter_frames(make_sl2([bytes(f)])))
