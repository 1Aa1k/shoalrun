# shoalrun

Rock and shoal candidate map for **Millinocket Lake, Maine** (the one NEOC is on),
plus an offline phone app that warns when you are heading at one.

**This is a navigation aid, not a chart.** Every candidate is satellite-derived
and unverified. Absence of a marker is not evidence of clear water. No agency
charts inland Maine lakes, which is why this exists — and also why nothing here
has been checked against a survey.

## What it found

234 candidates on a 34.5 km² lake with 99 km of shoreline and 74 islands:

| class | n | meaning |
|---|---|---|
| `exposed` | 84 | above water in most scenes — a visible ledge or boulder |
| `shoal` | 150 | never dries, but persistently brighter in green than surrounding water — shallow bottom |

Sanity check that these are geomorphology and not sun glint: `exposed` sits a
median of **40 m** from shore (96% within 150 m), `shoal` a median of 61 m (73%
within 150 m), and the deep western basin is **empty**. Glint would scatter
uniformly across open water.

## How it works

Source is **Sentinel-2 L2A** (10 m, free, properly georeferenced) via Microsoft
Planetary Computer — not scraped map tiles, which give one undated snapshot and
no way to separate a rock from a wave.

The whole method rests on time. A whitecap and a rock look identical in any
single image; they separate only across dates.

1. `build_lake.py` — stitch the OSM multipolygon into a clean shoreline, islands included.
2. `fetch_scenes.py` — 70 open-water scenes, 2019–2026, reprojected onto one fixed
   10 m grid. Identical grid is non-negotiable: sub-pixel drift between dates would
   read as a rock.
3. `detect_rocks.py` — water mask per scene, then persistence across scenes.
4. `build_app.py` — inline everything into one offline HTML file.

### Three things that had to be fixed, and are worth knowing

**Radiometric offset.** ESA processing baseline 04.00 (2022-01-25) added +1000 DN
to every band. A stack spanning that date holds two different conventions. Left
uncorrected it produced a step in apparent water area at exactly 2022 and
manufactured a fake population of "drawdown" rocks.

**Fixed thresholds don't work.** The measured NDWI water/land split moves from
−0.333 (2019) to −0.234 (2023). Otsu per scene handles both eras plus atmospheric
and sun-angle variation, and the offset cancels out of every downstream statistic.

**Only July–August are trusted.** Per-scene water fraction has sd ≈1.1% in Jul/Aug
but 8.3% in September and 14.3% in October. At 45.7°N the autumn sun gets low
enough that specular response over water swamps the water index — one October
scene read the lake as 48% dry, impossible for a lake this size. Those months are
radiometry, not hydrology, and are excluded rather than corrected.

### The class that was cut

A `drawdown` class — rock that dries out at low pond, the kind that takes a lower
unit off — is implemented but **disabled** (`EMIT_DRAWDOWN = False`). The idea is
sound: stage-correlated exposure is something a whitecap cannot fake. It is not
detectable from this data. The only radiometrically stable window is exactly when
a storage reservoir sits at full pond, and measured stage spread inside that
window (~1.1%) is the noise floor of the water mask itself. It emitted 450
features; all of them were noise wearing a hazard label.

Re-enable only with a real stage source (Brookfield operating records) or
winter/spring imagery with BRDF correction.

## Getting truth

Nothing here is verified. Ranked by what actually closes that gap:

1. **The person who lives on the lake.** Tap any candidate → "It's there" /
   "Not there". Dismissed rocks stop alerting immediately and are exported as
   labels. This is the highest-value input by a wide margin.
2. **Passive track logging.** The app records breadcrumbs whenever the boat is
   moving. Water you have driven through at speed is proven clear — negative
   evidence that costs nothing to produce and accumulates every trip.
3. **Sonar.** Any GPS fishfinder logs real measured depth. The only actually
   surveyed data available here.
4. **Maine DIFW lake survey map** and **USGS 3DEP lidar** (measures rock exposed
   at survey-time water level) as independent checks.

Export writes both tracks and verdicts as GeoJSON to feed back into the detector.

## Use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_lake.py     # shoreline
.venv/bin/python scripts/fetch_scenes.py   # ~15 min, 200 MB
.venv/bin/python scripts/detect_rocks.py   # candidates
.venv/bin/python scripts/verify.py         # stats + overview.png
.venv/bin/python scripts/build_app.py      # dist/index.html
node --test web/hazard.test.mjs            # alerting logic
```

`dist/index.html` is one 168 KB self-contained file — data, code, styles inlined,
zero network calls at runtime. Open it from the phone's local storage or add it to
the home screen. There is no cell service on that lake; anything that fetches at
runtime works in the driveway and fails on the water.

Append `?sim=1` to drive a synthetic boat through the eastern arm and watch the
alerting without leaving the dock.

### Alerting

Look-ahead corridor, not a proximity ring: position projected forward by speed ×
20 s along actual course over ground, ±35 m. A radius alarm screams while moored
and says nothing at 30 kn. Below ~3 kn it falls back to a plain radius, because
GPS course is meaningless while drifting. Ranked by time-to-contact. Alerts hold
for 4 s before clearing so a hazard at the edge of tolerance cannot strobe.

## Attribution

Shoreline © OpenStreetMap contributors (ODbL). Imagery: Copernicus Sentinel-2,
via Microsoft Planetary Computer.
