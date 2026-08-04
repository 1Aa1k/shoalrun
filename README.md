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

## Depth data (added)

**Yes, machine-readable depth exists for this lake.** MDIFW surveyed it in August
1954 (revised January 1979) along east-west transects, and the state digitised
those soundings into the `LakeDpth` layer, served as 68 KMZ tiles:

- Survey sheet (PDF, sounding numbers not contours):
  https://www.maine.gov/ifw/docs/lake-survey-maps/penobscot/millinocket_lake.pdf
- Statewide sounding points (KML → 68 KMZ tiles):
  https://www.maine.gov/ifw/fishing/kml/Lake_Depths.kml
- Metadata: http://geolibportal.usm.maine.edu/geonetwork/srv/en/metadata.show?id=345

`extract_soundings.py` pulls **260 soundings inside this lake**, 2–78 ft
(surveyed max 86 ft). `make_contours.py` interpolates them to **10 ft contours**.

Sparse data (one point per ~13 ha) is rescued by treating the shoreline as a
depth-0 boundary: 2,557 synthetic zero-points densified along the 99 km shore and
all 74 island edges anchor the surface where transects do not reach.

These contours are an **interpolated surface, not a survey**. Between transects
they are an educated guess, from 1954 soundings, on a regulated lake whose level
moves. A rock does not appear in a 10 ft contour — contours are context for the
hazard layer, not a replacement.

### The survey independently corroborates the detector

MDIFW's 1954 write-up, without any satellite involved:

> "rockiness is the outstanding feature of Millinocket Lake... **Large submerged
> rocks and shoals make navigation of the south portion of the lake quite
> hazardous**" and "a large deep basin on the northern end".

Both match: the densest candidate cluster is the southern/southwestern shallows,
and the interpolated 70 ft basin sits in the north, where the detector finds
nothing.

### Island class

Blobs ≥5000 m² are reclassified `island` (20 of them) — the largest was 96,700 m²,
a 9.7 ha landmass OSM never mapped. Calling that a rock is wrong, and drawing it
as a footprint-scaled dot produced a 175 m radius disc. Markers are now capped at
18 px.

## Prior art

| project | what | licence / cost | inland US lakes? |
|---|---|---|---|
| [Garmin Quickdraw Community](https://www.garmin.com/en-US/newsroom/press-release/marine/2016-garmin-introduces-quickdraw-community-an-online-community-of-free-user-generated-map-data/) | user-recorded sonar contours, shared pool | free, Garmin account, proprietary | yes — best existing option for this lake |
| [OpenSeaMap](https://www.openseamap.org/) | OSM-based charts + crowd depth, KAP export | ODbL / free | mostly coastal + waterways |
| [OpenCPN](https://opencpn.org/) | open-source chartplotter, reads KAP/S-57 | GPLv2 | viewer only, brings no lake data |
| [ACOLITE](https://github.com/acolite/acolite) | atmospheric correction + SDB for S2/Landsat | open source | yes, general purpose |
| [S2Shores](https://www.nature.com/articles/s41597-025-06402-w) | wave-inversion bathymetry from S2 | open source | coastal (needs swell) — not lakes |
| Stumpf ratio transform | ln(B2)/ln(B3) → depth, needs 5–10 calibration points | published method | yes, ~30 m depth in lakes |

Nothing found produces a rock/shoal hazard layer for a small inland US lake from
imagery. Quickdraw is the closest, and it requires somebody to have driven every
line with a Garmin sounder.

### Obvious next step

Stumpf ratio transform for real satellite-derived bathymetry: it wants 5–10
calibration points and **we now have 260 measured soundings**, and it is quoted
good to ~30 m in lakes while this lake bottoms out at 26 m (86 ft). That would
replace interpolation-between-1954-transects with a continuous 10 m depth surface.
Needs a re-fetch to add band B02 (blue) — the current stack only carries B03/B08.

## Does it work on any lake?

**The detector: yes, anywhere on Earth.** **The depth data: Maine only.**

Two things used to pin this to one lake, and both are gone:

- **Projection.** UTM zone 19N was hardcoded in five files. A lake outside that
  zone would still "reproject successfully" and land the grid in the wrong place,
  putting the shoreline mask on forest. Now derived per lake from its own
  centroid (`shoalrun_config.utm_epsg`).
- **Season.** "Trusted months = July, August" was tuned to 45.7°N and travels
  nowhere. Months were only ever a proxy for **sun elevation**, which is the
  actual variable — low sun raises specular response over water until it swamps
  the water index. Now computed analytically (NOAA solar position) and filtered at
  ≥51°. On this lake that threshold reproduces the empirical Jul/Aug result exactly
  and correctly reinstates June, which the hand-written month list had dropped for
  no reason.

To point it at another lake: change `OSM_RELATION` and `OSM_SEARCH_BBOX` in
`scripts/shoalrun_config.py`, then rerun the pipeline.

### Real limits, which are not about code

- **Depth is Maine-only.** `extract_soundings.py` reads the MDIFW `LakeDpth`
  layer. Maine surveyed thousands of its lakes; most states published nothing
  comparable. Elsewhere you get hazards with no contours unless you find a local
  equivalent.
- **Clear water required.** Shoal detection reads the bottom through the water
  column in the green band. A turbid, silty or algal lake will produce confident
  nonsense. Millinocket is clear and oligotrophic, which is why this works here.
- **Size floor.** 10 m pixels mean a small pond has too few water pixels for the
  per-scene Otsu threshold to find two modes. Below roughly 0.5 km² expect it to
  fail, and fail loudly rather than quietly — that is what `MIN_VALID_OBS` and the
  scene-count guard are for.
- **Latitude ceiling.** Above roughly 60°N the sun never clears 51°, so the filter
  will correctly refuse to emit anything rather than trust bad radiometry.

### Scene selection now

Two independent gates, both principled rather than tuned:

| gate | value | why |
|---|---|---|
| sun elevation | ≥51° | below this, specular glare swamps the water index |
| scene usable | ≥85% | a mostly-cloud scene should not get an equal vote in a persistence statistic |

On this lake: 29 of 70 scenes survive both. The one scene at 62% usable reported
the lake 20 points drier than every neighbouring date.

## Licence

Code is **MIT**. The data is not — see [DATA-LICENSE.md](DATA-LICENSE.md). The
shoreline is OSM-derived and therefore **ODbL share-alike**, which MIT does not
override, and `dist/index.html` inlines it. The MDIFW soundings carry their own
notice: *"Data not to be used for navigation purposes."*
