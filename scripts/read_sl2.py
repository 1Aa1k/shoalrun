#!/usr/bin/env python3
"""Read a Lowrance .sl2 sonar log into (lat, lon, depth).

This is the receive end of the only route left to a better depth map of this
lake. Every optical method has measured null (see
`docs/handoffs/2026-08-08-lake-stage-exposure-null.md`); sound does not care
that the water is stained. A fishfinder that logs to SD, one season of ordinary
boating, and the 1954 lead-line survey is beaten.

## What is trustworthy here, and what is not

The **framing** is solid and is what this file is careful about: a .sl2 is an
8-byte header followed by frames that each declare their own length, so the file
walks itself. Every one of those declared lengths is caller-supplied data from
an SD card, so all of them are bounds-checked before use.

The **field offsets inside a frame** are reverse-engineered by other people and
this project has no .sl2 file to check them against yet. Rather than write a
table of numbers and present it as fact, the table is declared in one place,
marked unverified, and `--probe` derives the real offsets from an actual file by
scoring every candidate on whether it produces physically possible series:

    .venv/bin/python scripts/read_sl2.py --probe LOG.sl2

That turns "are these offsets right" from a guess into a ten-second measurement
the first time a real log exists. Until then, treat any depth this prints as
unconfirmed.

## Usage

    .venv/bin/python scripts/read_sl2.py --probe LOG.sl2          # verify offsets
    .venv/bin/python scripts/read_sl2.py LOG.sl2                  # stats only
    .venv/bin/python scripts/read_sl2.py LOG.sl2 --out data/sonar/2026-08-11
"""

from __future__ import annotations

import argparse
import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake.geojson"

# Lowrance stores position as Spherical Mercator metres on a sphere of the
# earth's POLAR radius, not the equatorial one. Using 6378137 here puts the
# track about 20 km north of where the boat was.
LOWRANCE_R = 6356752.3142

FT_PER_M = 3.280839895

SL2_HEADER = 8
FORMATS = {1: "slg", 2: "sl2", 3: "sl3"}

# Bounds on caller-supplied sizes. A corrupt or hostile file will happily
# declare a four-billion-byte frame; these turn that into an error instead of an
# allocation.
SIZE_FIELD = 0x28            # where a frame states its own length
# The smallest frame that can even hold that field. The loop guard has to use
# this, not a "reasonable" minimum: a tail of 40 bytes passes a >= 40 check and
# then reads two bytes at offset 40 off the end of the buffer. Found by fuzzing.
MIN_FRAME = SIZE_FIELD + 2
MAX_FRAME = 1 << 20          # 1 MB; real frames are a few kB
MAX_FRAMES = 20_000_000      # ~ a season of logging, and a hard stop either way

# Plausibility gates. A ping outside these is not a parse failure -- one bad
# ping in a million should not lose the file -- so they are counted and reported
# rather than raised on. Anything STRUCTURALLY wrong still raises.
DEPTH_FT_RANGE = (0.3, 400.0)
SPEED_KN_MAX = 80.0
TEMP_C_RANGE = (-5.0, 45.0)


class Sl2Error(Exception):
    """The file is not a readable .sl2. Never raised for implausible values."""


@dataclass(frozen=True)
class FieldTable:
    """Where each value sits inside a frame, and how to read it.

    UNVERIFIED against a real file. `--probe` exists to replace these numbers
    with measured ones; `from_json` loads what it finds so the correction lands
    in data, not in a code edit.
    """

    name: str
    depth_ft: int | None = None       # float32, feet
    lowrance_x: int | None = None     # int32, mercator metres east
    lowrance_y: int | None = None     # int32, mercator metres north
    speed_kn: int | None = None       # float32, knots
    temp_c: int | None = None         # float32, celsius
    heading_rad: int | None = None    # float32
    time_ms: int | None = None        # uint32, milliseconds since log start
    verified: bool = False

    @classmethod
    def from_json(cls, path: Path) -> "FieldTable":
        return cls(**json.loads(Path(path).read_text()))


# The commonly cited community layout. Every offset here is a hypothesis.
SL2_DEFAULT = FieldTable(
    name="sl2-community-unverified",
    depth_ft=0x64,
    lowrance_x=0x9C,
    lowrance_y=0xA0,
    speed_kn=0x94,
    temp_c=0x98,
    heading_rad=0xB0,
    time_ms=0xB4,
    verified=False,
)


@dataclass(frozen=True)
class Ping:
    lat: float
    lon: float
    depth_ft: float
    time_ms: int
    speed_kn: float = 0.0
    temp_c: float = 0.0
    heading_rad: float = 0.0
    channel: int = 0
    frame_index: int = 0


@dataclass
class ReadReport:
    """Everything that was thrown away, and why. Silence here would be a lie."""

    frames: int = 0
    pings: int = 0
    dropped_no_fix: int = 0
    dropped_depth: int = 0
    dropped_speed: int = 0
    dropped_outside_lake: int = 0
    channels: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# --- position ---------------------------------------------------------------

def lowrance_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Spherical Mercator metres to degrees, on Lowrance's polar-radius sphere."""
    lon = math.degrees(x / LOWRANCE_R)
    lat = math.degrees(2.0 * math.atan(math.exp(y / LOWRANCE_R)) - math.pi / 2.0)
    return lon, lat


def lonlat_to_lowrance(lon: float, lat: float) -> tuple[float, float]:
    """Inverse of the above. Only used to build test fixtures and to probe."""
    x = math.radians(lon) * LOWRANCE_R
    y = LOWRANCE_R * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y


# --- framing ----------------------------------------------------------------

def read_header(buf: bytes) -> dict:
    if len(buf) < SL2_HEADER:
        raise Sl2Error(f"file is {len(buf)} bytes, shorter than an 8-byte header")
    fmt, version, block, _pad = struct.unpack_from("<HHHH", buf, 0)
    if fmt not in FORMATS:
        raise Sl2Error(f"format id {fmt} is not one of {sorted(FORMATS)}")
    if fmt != 2:
        raise Sl2Error(f"this reader handles sl2 (2); file says {FORMATS[fmt]} ({fmt})")
    return {"format": FORMATS[fmt], "version": version, "block_size": block}


def iter_frames(buf: bytes, max_frames: int = MAX_FRAMES) -> Iterator[memoryview]:
    """Walk the frame chain, trusting no length the file states.

    Each frame declares its own size at offset 0x28. That number arrives from an
    SD card, so it is checked against the file's real remaining length and
    against sane bounds before it is used to advance -- and it must advance, or
    a zero-length frame would spin here forever.
    """
    view = memoryview(buf)
    pos = SL2_HEADER
    seen = 0
    while pos + MIN_FRAME <= len(view):
        if seen >= max_frames:
            raise Sl2Error(f"more than {max_frames} frames; refusing to continue")
        (size,) = struct.unpack_from("<H", buf, pos + SIZE_FIELD)
        if size < MIN_FRAME or size > MAX_FRAME:
            raise Sl2Error(f"frame {seen} at {pos} declares {size} bytes, "
                           f"outside {MIN_FRAME}..{MAX_FRAME}")
        if pos + size > len(view):
            raise Sl2Error(f"frame {seen} at {pos} declares {size} bytes but only "
                           f"{len(view) - pos} remain")
        yield view[pos:pos + size]
        pos += size
        seen += 1


def _f32(frame: memoryview, off: int | None) -> float:
    """Little-endian float at an offset, or 0.0 if the table has no offset.

    Returns 0.0 rather than raising when the field runs past the end of a short
    frame: frames differ in length by channel, and a sidescan frame that has no
    room for a temperature reading is not a corrupt file.
    """
    if off is None or off + 4 > len(frame):
        return 0.0
    (v,) = struct.unpack_from("<f", frame, off)
    return v if math.isfinite(v) else 0.0


def _i32(frame: memoryview, off: int | None) -> int:
    if off is None or off + 4 > len(frame):
        return 0
    return struct.unpack_from("<i", frame, off)[0]


def _u32(frame: memoryview, off: int | None) -> int:
    if off is None or off + 4 > len(frame):
        return 0
    return struct.unpack_from("<I", frame, off)[0]


def _u16(frame: memoryview, off: int) -> int:
    if off + 2 > len(frame):
        return 0
    return struct.unpack_from("<H", frame, off)[0]


def decode(frame: memoryview, table: FieldTable = SL2_DEFAULT) -> Ping | None:
    """One frame to one ping, or None if it carries no usable fix."""
    x = _i32(frame, table.lowrance_x)
    y = _i32(frame, table.lowrance_y)
    if x == 0 and y == 0:
        return None                      # no GPS lock on this ping
    lon, lat = lowrance_to_lonlat(x, y)
    return Ping(
        lat=lat,
        lon=lon,
        depth_ft=_f32(frame, table.depth_ft),
        time_ms=_u32(frame, table.time_ms),
        speed_kn=_f32(frame, table.speed_kn),
        temp_c=_f32(frame, table.temp_c),
        heading_rad=_f32(frame, table.heading_rad),
        channel=_u16(frame, 0x2C),
        frame_index=_u32(frame, 0x30),
    )


def read_pings(buf: bytes, table: FieldTable = SL2_DEFAULT,
               bbox: tuple[float, float, float, float] | None = None
               ) -> tuple[list[Ping], ReadReport]:
    """Every usable ping in the file, plus an account of what was dropped."""
    read_header(buf)
    rep = ReadReport()
    out: list[Ping] = []
    for frame in iter_frames(buf):
        rep.frames += 1
        ping = decode(frame, table)
        if ping is None:
            rep.dropped_no_fix += 1
            continue
        rep.channels[ping.channel] = rep.channels.get(ping.channel, 0) + 1
        if not (DEPTH_FT_RANGE[0] <= ping.depth_ft <= DEPTH_FT_RANGE[1]):
            rep.dropped_depth += 1
            continue
        if abs(ping.speed_kn) > SPEED_KN_MAX:
            rep.dropped_speed += 1
            continue
        if bbox and not (bbox[0] <= ping.lon <= bbox[2] and bbox[1] <= ping.lat <= bbox[3]):
            rep.dropped_outside_lake += 1
            continue
        out.append(ping)
    rep.pings = len(out)
    return out, rep


# --- finding the offsets in a real file -------------------------------------

def plausible_depth(values: list[float]) -> float:
    """Score a series on looking like a depth sounder's output.

    Depth is bounded, positive, and moves slowly -- a boat cannot go from 8 ft
    to 80 ft between pings. Scoring on smoothness as well as range is what
    separates the real depth field from some other float that happens to land in
    the same numeric range.
    """
    vals = [v for v in values if math.isfinite(v)]
    if len(vals) < 8:
        return 0.0
    in_range = sum(DEPTH_FT_RANGE[0] <= v <= DEPTH_FT_RANGE[1] for v in vals) / len(vals)
    if in_range < 0.9:
        return 0.0
    jumps = [abs(b - a) for a, b in zip(vals, vals[1:])]
    smooth = sum(j < 3.0 for j in jumps) / max(1, len(jumps))
    spread = (max(vals) - min(vals)) > 0.5      # a constant is not a sounder
    return in_range * smooth * (1.0 if spread else 0.2)


def plausible_position(xs: list[int], ys: list[int],
                       bbox: tuple[float, float, float, float]) -> float:
    """Score a pair of int32 columns on decoding to somewhere inside the bbox."""
    if len(xs) < 8:
        return 0.0
    good = 0
    for x, y in zip(xs, ys):
        if x == 0 and y == 0:
            continue
        try:
            lon, lat = lowrance_to_lonlat(x, y)
        except (OverflowError, ValueError):
            continue
        if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:
            good += 1
    return good / len(xs)


def probe(buf: bytes, bbox: tuple[float, float, float, float],
          sample: int = 400) -> dict:
    """Find the depth and position offsets by trying every one of them.

    Cheaper and far more honest than trusting a table off the internet: the
    right offset is the one whose column of numbers is physically possible for
    this lake, and that is a property this file can test directly.
    """
    frames = []
    for n, frame in enumerate(iter_frames(buf)):
        if n >= sample:
            break
        frames.append(bytes(frame))
    if not frames:
        raise Sl2Error("no frames to probe")
    width = min(len(f) for f in frames)

    depth_scores = []
    for off in range(0, width - 4, 4):
        col = [struct.unpack_from("<f", f, off)[0] for f in frames]
        s = plausible_depth(col)
        if s > 0:
            depth_scores.append((s, off, min(col), max(col)))
    depth_scores.sort(reverse=True)

    pos_scores = []
    for off in range(0, width - 8, 4):
        xs = [struct.unpack_from("<i", f, off)[0] for f in frames]
        ys = [struct.unpack_from("<i", f, off + 4)[0] for f in frames]
        s = plausible_position(xs, ys, bbox)
        if s > 0.5:
            pos_scores.append((s, off))
    pos_scores.sort(reverse=True)

    return {
        "frames_sampled": len(frames),
        "frame_width": width,
        "depth_candidates": [
            {"offset": hex(o), "score": round(s, 3),
             "min_ft": round(lo, 1), "max_ft": round(hi, 1)}
            for s, o, lo, hi in depth_scores[:5]
        ],
        "position_candidates": [
            {"x_offset": hex(o), "y_offset": hex(o + 4), "score": round(s, 3)}
            for s, o in pos_scores[:5]
        ],
        "table_says": {"depth_ft": hex(SL2_DEFAULT.depth_ft),
                       "lowrance_x": hex(SL2_DEFAULT.lowrance_x)},
    }


# --- output -----------------------------------------------------------------

def to_geojson(pings: list[Ping], source: str) -> dict:
    """Same shape as data/soundings.geojson, so the two can sit side by side.

    depth_ft and nothing derived: no datum correction, no sound-velocity
    profile, no tide. The number is what the sounder said.
    """
    return {
        "type": "FeatureCollection",
        "meta": {"data_source": source, "count": len(pings),
                 "note": "raw sounder depth, no datum correction"},
        "features": [
            {"type": "Feature",
             "properties": {"depth_ft": round(p.depth_ft, 1),
                            "t_ms": p.time_ms,
                            "speed_kn": round(p.speed_kn, 2),
                            "data_source": source},
             "geometry": {"type": "Point",
                          "coordinates": [round(p.lon, 6), round(p.lat, 6)]}}
            for p in pings
        ],
    }


def to_track(pings: list[Ping]) -> list[dict]:
    """Fixes in the shape web/swept.js wants, so a log turns into swept area.

    Accuracy is not in the file -- consumer GPS in a fishfinder is 3-5 m and the
    log does not record its own error -- so it is stated once here rather than
    invented per fix.
    """
    return [{"lat": round(p.lat, 6), "lon": round(p.lon, 6),
             "t": p.time_ms, "speed": round(p.speed_kn * 0.514444, 2),
             "accuracy": 5.0}
            for p in pings]


def lake_bbox() -> tuple[float, float, float, float]:
    from shapely.geometry import shape
    gj = json.loads(LAKE.read_text())
    geom = gj["features"][0]["geometry"] if gj.get("type") == "FeatureCollection" else gj
    west, south, east, north = shape(geom).bounds
    pad = 0.02                       # a launch ramp is often just outside the polygon
    return (west - pad, south - pad, east + pad, north + pad)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="a .sl2 file off the fishfinder's SD card")
    ap.add_argument("--probe", action="store_true",
                    help="find the field offsets in this file instead of decoding it")
    ap.add_argument("--table", type=Path, help="offset table JSON, once one is measured")
    ap.add_argument("--out", type=Path, help="write <out>.geojson and <out>.track.json")
    ap.add_argument("--no-bbox", action="store_true",
                    help="keep fixes outside the lake (use when checking a new log)")
    args = ap.parse_args()

    buf = args.path.read_bytes()
    bbox = lake_bbox()

    if args.probe:
        print(json.dumps(probe(buf, bbox), indent=2))
        return

    table = FieldTable.from_json(args.table) if args.table else SL2_DEFAULT
    if not table.verified:
        print(f"WARNING: offset table '{table.name}' has never been checked against "
              f"a real file.\n         Run --probe on this log first; depths below "
              f"may be nonsense.\n")

    pings, rep = read_pings(buf, table, None if args.no_bbox else bbox)
    print(f"{args.path.name}: {rep.frames:,} frames -> {rep.pings:,} usable pings")
    print(f"  dropped: {rep.dropped_no_fix:,} no fix, {rep.dropped_depth:,} depth "
          f"out of range, {rep.dropped_speed:,} speed, "
          f"{rep.dropped_outside_lake:,} outside the lake")
    print(f"  channels: {rep.channels}")
    if pings:
        d = sorted(p.depth_ft for p in pings)
        print(f"  depth {d[0]:.1f} to {d[-1]:.1f} ft, median {d[len(d) // 2]:.1f}")
        span = (max(p.time_ms for p in pings) - min(p.time_ms for p in pings)) / 60000
        print(f"  {span:.0f} minutes of logging")

    if args.out and pings:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        gj = args.out.with_suffix(".geojson")
        tr = args.out.with_suffix(".track.json")
        gj.write_text(json.dumps(to_geojson(pings, f"sonar: {args.path.name}")))
        tr.write_text(json.dumps(to_track(pings)))
        print(f"wrote {gj}\nwrote {tr}")


if __name__ == "__main__":
    main()
