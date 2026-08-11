# Solar survey boat — design notes

**Status: someday build. Nothing started. Written 2026-08-09 so the reasoning
survives.**

Not a navigation aid, not a hazard product, and deliberately not part of shoalrun
— see "Keep it separate" below. This is a science-and-video project that produces
an open dataset.

## Why it exists

The only depth chart for Millinocket Lake was surveyed **with a lead line in August
1954**, revised 1979, and it is still what everyone uses. `survey_geometry.py`
recovers its shape from the data: 260 soundings in **12 east-west transects about
530 m apart**, nothing measured between them. 42% of the lake sits more than 200 m
from any real measurement. MDIFW's own survey text on the sheet:

> "rockiness is the outstanding feature of Millinocket Lake... Large submerged
> rocks and shoals make navigation of the south portion of the lake quite
> hazardous."

Everything else has been tried and measured as null (see
`docs/handoffs/2026-08-08-lake-stage-exposure-null.md`): the water is too stained
for light to reach the bottom, so satellite-derived bathymetry scored AUC 0.507 —
a coin flip — and reading depth off the lake's own level changes found no signal
either. **Sound is the only thing left that works.** That dead end is the reason
the boat has to exist, and it is the first act of the video.

## Non-goals

Stating these because each one removes real engineering:

- **Not navigation.** No completeness guarantee, no accuracy claim, no liability
  posture. "Here is where it drove and what the sounder read" is the whole product.
- **No RTK.** Standard GPS at 3–5 m is well inside a 25 m depth grid.
- **No sound-velocity profiling or datum correction.** Raw sounder depth ships as-is.
  (Note: the 1954 datum is unrecoverable anyway — `surface_elevation_ft` is 0 for all
  260 soundings and the paper sheet does not print a water-surface elevation. So the
  old chart already carries an unknown constant offset.)
- **Not the shallows.** Hard abort above 1.5 m depth. The last metre near rocks is
  where survey boats die, and skipping it removes the dominant loss risk.

## Hull

**Catamaran, ~1.8 m LOA, ~0.9 m beam.** Sealed foam-filled hulls, flat deck,
transducer on a strut in the tunnel between them.

Catamaran for three specific reasons, not style:

1. **Clean water at the transducer** — in the tunnel, ahead of the props, away from
   bubbles. Aerated water is the most common cause of garbage depth data.
2. **Roll stability** — a sounder cone is ~20° wide; 15° of roll measures a slant
   range to one side, not depth beneath. Wide beam plus mass low in the hulls fixes
   it geometrically.
3. **Effectively non-capsizable** at that beam with the battery down low.

**Length is set by waves.** Fetch across the lake is roughly 6 km. Fetch-limited
estimates:

```
wind 10 kt  ->  Hs ~0.2 m,  wavelength ~6 m
wind 20 kt  ->  Hs ~0.4 m,  wavelength ~9 m
```

Wavelength is 3–5x the hull length, which is the good regime — it contours over
waves rather than bridging and slamming. Danger is hull length ≈ wavelength
(resonant pitching), so a *longer* boat would ride worse here. Below ~1.2 m it gets
tossed and the transducer ventilates constantly.

Waves are a data-quality problem, not a capsize problem. Mitigate by mounting the
transducer deep, surveying at 1.0–1.5 m/s, and **rejecting pings by IMU attitude** —
the autopilot already logs pitch and roll, so correlate and discard.

## Power — solar is the unlock

Battery-only means ~4–6 h per sortie and 20+ trips to the launch for full coverage.
The cost of this project was never robot hours, it was *human* sorties. Solar
deletes them.

```
deck ~1.6 m2, ~1.2 m2 usable    ->  ~240 W peak
Maine July, ~5 peak-sun-hours   ->  ~0.85 kWh/day after real losses

draw: ~12 W propulsion at 1.2 m/s (3 N drag, poor small-prop efficiency)
      ~15 W autopilot + sonar + telemetry
      ~30 W total

0.85 kWh/day / 30 W  =  ~28 h of running per day of sun
```

Energy-positive with a wide margin — double the drag estimate and it still holds.
Daylight becomes the limit, not battery. At ~10 h/day running:

```
43 km/day
priority zone (~180 km)   ~4 days
whole lake    (~690 km)   ~16 days
```

Four unattended days for the water that matters.

**The cost of solar is that it is now unattended overnight in weather nobody
approved.** That is a materially harder project, and it lands entirely on the
safety section.

## Safety — the boat is lost by drifting, not by wind

Wind cannot overpower it. Frontal area ~0.15 m² gives 10–20 N of drag at 20 kt
against 100+ N from a modest thruster — 5–10x margin. It will hold station in
anything worth launching in.

What actually loses it is losing power or control and then drifting:

- **Independent recovery beacon.** Own battery, own LTE/LoRa link, reports position
  regardless of the main system. The one non-negotiable part; everything else may fail.
- **Geofence + RTL on every failsafe** (comms loss, low battery, GPS loss). ArduPilot
  native.
- **Positive buoyancy fully flooded.** A swamped boat that floats is recoverable.
- **Hard battery reserve** sized to punch home against design wind, not calm.
- **Weather abort** — run to a chosen shelter cove on forecast wind.
- **Depth abort at 1.5 m** — back out along the inbound track. The payload is the
  safety sensor.
- **Visibility** — orange, retroreflective tape, flag on a mast. Name and phone
  number on the deck. It shares the lake with fishermen.

## Sensing and autonomy

**Do not build sonar.** Signal processing is a research project. Buy a fishfinder
that logs: Lowrance HOOK/Elite class records `.sl2` to SD with depth plus GPS per
ping, the format is reverse-engineered, and Python readers exist. That yields a
clean (lat, lon, depth) stream.

**Autopilot: ArduPilot Rover, boat frame.** Mission Planner generates lawnmower
survey grids natively. Pixhawk-class FC, two thrusters, differential steer (no
rudder), 915 MHz telemetry.

**The smart part: information-gain planning.** Fit a Gaussian process to soundings
as they arrive. That gives a depth estimate *and* an honest uncertainty everywhere.
Plan the next leg toward the highest reachable uncertainty; as coverage accumulates,
uncertainty collapses locally and the boat re-targets itself. It spends its time
where nothing is known.

Three things this fixes at once:

1. `priority_mask.py` becomes the mission planner — 237 tiles already ranked by
   where the 1954 survey never reached. Built to aim imagery; better suited to
   aiming a boat.
2. It repairs the chart's real defect. `make_contours.py:build_surface()` runs
   `griddata` isotropically, interpolating across the 530 m gaps with the same
   confidence it has along a transect. A GP does not — uncertainty grows with
   distance from data.
3. The "survey reach" layer becomes real. Today it shows metres-to-nearest-1954-
   sounding as a proxy for confidence; a GP shows actual posterior variance.

It also makes the video. The uncertainty map clearing behind the boat as it picks
its own next leg is the shot.

Scales to two boats without modification — hand out the top-N uncertainty targets.

## Keep it separate

Own repo, own framing. If this feeds shoalrun it inherits shoalrun's navigational
posture whether or not that is wanted, and shoalrun is careful about that for good
reason (see its `HANDOFF.md`). As its own thing it is cleaner, more interesting, and
easier to open source. `DATA-LICENSE.md` is a starting point for the dataset terms.

## Build order

1. Fishfinder on a friend's boat. Prove the `.sl2` → (lat, lon, depth) → map
   pipeline with zero autonomy. Real data this season if wanted.
   **The receive side is built** (2026-08-11): `scripts/read_sl2.py` reads the
   log to soundings plus a track in the shape `web/swept.js` wants. Field
   offsets inside a frame are unverified until a real file exists —
   `--probe LOG.sl2` measures them off the file itself rather than trusting a
   table, so step one is: log ten minutes, run `--probe`, check that the depth
   column it picks is the one the community table claims.
2. Hull + manual RC. Prove it survives the lake and the transducer reads clean under
   way.
3. ArduPilot + lawnmower grid, supervised, line of sight.
4. Solar + failsafes + beacon. Unattended.
5. GP planner. The interesting part, and the one worth filming.

## Open questions

- Where does it live between sorties — trailer, dock, mooring?
- Weed and prop fouling: weedless, ducted, or jet?
- Winter storage and Maine ice-out timing (early May to late November is the window).
- Is the video the deliverable, the dataset, or the boat design? Changes what gets
  polished.
