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
| `shoal` | 61 | **submerged shallow bottom — never breaks the surface.** The dangerous one |
| `rock` | 125 | rock that breaks the surface but is smaller than a 10 m pixel |
| `exposed` | 49 | large enough to resolve as land in most scenes — a visible ledge |
| `island` | 18 | landmass OSM never mapped |

### The mistake worth reading about

The first version had only `exposed` and `shoal`, and called 150 things shoals.
Nate cross-checked them against the lake and said they looked like rocks. He was
right, and the cause was a resolution limit dressed up as physics.

At 10 m, **a rock smaller than a pixel can never be classified as land** — the
pixel's reflectance is dominated by the water around it, so it can only ever
present as "brighter water", which is identical to a shallow bottom. The class
boundary was really *bigger than a pixel* vs *smaller than a pixel*, which is the
wrong axis entirely, and it inflated the scary invisible-hazard class with things
you can actually see.

NIR resolves it physically. Water absorbs near-infrared almost completely, so a
submerged rock returns the water background while any dry surface in the pixel
reflects it. Measured: **74% of the original "shoals" carried an NIR excess**
(median z 1.58, p90 3.06) that nothing underwater can produce.

Independent check that the new split is real, not just a re-partition of noise —
distance from shore, which was never tuned for:

| class | median from shore | within 150 m |
|---|---|---|
| `exposed` | 29 m | 96% |
| `island` | 55 m | 100% |
| `rock` | 82 m | 68% |
| `shoal` | **170 m** | 46% |

Reclassified shoals sit twice as far offshore as rocks, which is what genuine
mid-lake shallow bottom should do.

Sanity check that these are geomorphology and not sun glint: everything clusters
on shoreline and island structure, and the deep western basin is **empty**. Glint
would scatter uniformly across open water.

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
4. `export_depth_grid.py` — the interpolated depth surface as a compact grid,
   plus a companion grid of how far each cell is from a real 1954 sounding.
5. `build_app.py` — inline everything into one offline HTML file (map, 3D, info).

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

## Automated verification against 0.3 m aerial imagery

Candidates are checked by `verify_naip.py` against **NAIP aerial imagery at
0.3 m** — a different sensor, platform and year from the Sentinel-2 detections.
One Sentinel pixel covers ~1,100 NAIP pixels, so a rock that was sub-pixel in the
source data is fully resolved here. No human confirmation required.

| Sentinel class | n | rock confirmed | shoal confirmed | not confirmed |
|---|---|---|---|---|
| `island` | 17 | **100%** | 0% | 0% |
| `exposed` | 47 | **96%** | 0% | 4% |
| `rock` | 125 | 15% | 23% | 62% |
| `shoal` | 60 | 0% | 3% | **97%** |

**Read `exposed` and `island` first — they are the positive control.** If NAIP
were misregistered against Sentinel, or the chip sampling were off target, those
two would fail alongside everything else. They do not. That validates the method
and the alignment, and it means the sub-pixel classes failing is a real result
rather than a registration artifact.

So: **the resolved classes are solid, the sub-pixel classes are mostly false
positives** — glint and wave patterns that survived the persistence statistic
because persistence cannot distinguish "recurring artefact" from "real object"
when the object is smaller than a pixel.

Net: **112 verified hazards** (81 rock, 31 shoal) out of 253 candidates.

What this check cannot do: NAIP here is a **single date** (2023-09-01) against 29
Sentinel dates, at a different lake level, with lower September sun. One flight
cannot prove a hazard absent — something under a wave that morning reads as open
water. So `open_water` **demotes** a candidate, it never deletes it. Unconfirmed
candidates still render (faint, dashed) and still alert, they just never outrank
a confirmed one of comparable urgency.

### Ranking bug caught while wiring this in

Making verified hazards sort first seemed obviously right and was a safety bug: a
confirmed hazard 600 m away would outrank an unconfirmed one 20 m dead ahead, and
since the banner reports the top hit, it would read "clear" while the boat closed
on the near one. Time-to-contact dominates always; verification only breaks ties
between hazards of comparable urgency. Regression test covers it.

## Getting truth

Nothing here is verified. Ranked by what actually closes that gap:

0. **Automated: 0.3 m NAIP aerial** (`verify_naip.py`, above). Runs unattended,
   no human input, and already reclassified the whole candidate set.
1. **The person who lives on the lake.** Tap any candidate → "It's there" /
   "Not there". Optional now that NAIP does the bulk verification, but still the
   only source that knows what is under the water rather than on it.
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
.venv/bin/python scripts/export_depth_grid.py  # depth surface for the app
.venv/bin/python scripts/build_app.py      # dist/index.html
node --test web/*.test.mjs                 # alerting, depth, sync, draw rules
```

Live at **https://sproultech.com/shoalrun** (and https://1aa1k.github.io/shoalrun/,
published from `master:/docs` by the same build).

`dist/index.html` is one self-contained file — data, code, styles inlined,
zero network calls at runtime. Open it from the phone's local storage or add it to
the home screen. There is no cell service on that lake; anything that fetches at
runtime works in the driveway and fails on the water.

Append `?sim=1` to drive a synthetic boat through the eastern arm and watch the
alerting without leaving the dock. `?theme=night` opens in the dark palette.

### One page, three tabs

The map, the 3D bottom and the honesty page were three HTML files with three
sets of chrome, and moving between them felt like moving between three tools
that happened to share a lake. They are three tabs in one document now:

| tab | what it is |
|---|---|
| **Map** | the helm view — alert strip, lake, one line of legend |
| **3D** | the interpolated 1954 bottom, orbit / fly / boat |
| **Info** | what the evidence tiers are worth, export, report review, lake code |

`#map`, `#3d` and `#info` deep-link to a tab; `#lat,lon,zoom` still opens the
map at a spot. `dist/3d.html` survives as a redirect for anything bookmarked.

The 3D view is wrapped in a function at build time and stood up the first time
its tab is opened — it takes a WebGL context and builds a 156k-triangle mesh,
and neither should happen on a phone that only ever opens the map. Only the
visible view animates; the GPS watch, the alarm and the track log run in all
three.

### What the map draws by default

Verified marks only: the 48 confirmed above the waterline plus the 1,311 that
return infrared. The 3,549 unverified are one tap away under **Layers → Every
candidate**, and they alarm either way — hidden from view is not hidden from the
alarm.

This replaced two separate toggles ("guest mode" and "all rocks") that cut on
different axes and fought each other. The offshore cut in particular was
backwards: the confirmed rocks on this lake sit a median 2 m from shore, so
tidying the map by distance from shore threw away exactly the marks that hold
up. Marker size and opacity also ride the zoom, because 1,359 marks all sitting
at the minimum tappable radius merge into a solid band around the shoreline and
hide the depth shading the view is for.

### Two themes, and why the light one is the default

`chart` reproduces a NOAA paper chart — buff land, black shoreline, discrete blue
shoal tints over white, magenta danger symbols (cross-in-dotted-circle for a
submerged rock, asterisk for one that breaks the surface), and the measured 1954
soundings printed as numbers. `night` is the dark screen-native palette with a
continuous depth ramp.

Chart is the default because this gets used outdoors in daylight, where a dark
screen is unreadable and a light one is not. The conventions are borrowed on
purpose: anyone who has read a chart already knows magenta means danger and that
the number on the water is a measurement, not an interpolation. Which is exactly
the distinction that matters here — **the printed soundings are the 260 real
1954 measurements; every contour between them is inference.**

### The depth surface ships as a grid, not as contours

Contours used to be cut at build time at a fixed 10 ft interval, so seeing a
different interval meant a rebuild. `export_depth_grid.py` ships the interpolated
surface itself as a 404×299 uint8 grid at 25 m (158 KB base64, less than the
253 KB the 10 ft contours alone cost), and the app runs marching squares on it.
The interval became a slider, the depth shading and the contour lines now come
from the same numbers, and the 3D viewer reads the identical surface — so the
mesh and the lines cannot disagree about where 20 ft is.

### 3D viewer — the 3D tab

Hand-rolled WebGL, no library, because a CDN script tag is a runtime network
dependency wearing a hat. Three camera modes:

| mode | what it is for |
|---|---|
| `orbit` | the whole basin, spun around its centre — reading overall shape |
| `fly` | free camera, WASD + mouse look — getting in close to one shoal |
| `boat` | eye height above the waterline, driving, with depth/speed/nearest-hazard HUD |

Hazards draw as vertical stems from the bottom to the surface: a dot floating at
depth gives you no sense of distance, while a stem shows both where it sits and
how much water is over it.

Two deliberate choices. **Depth is quantised to 3 ft steps by default** rather
than rendered as a smooth surface — a smooth shaded basin looks far more
authoritative than 260 soundings from 1954 deserve, and the terracing is a
standing reminder of how coarse the input is. Snapping uses `floor`, so every
terrace is at or *shallower* than the interpolated depth; on a boat, that is the
only direction it is safe to round. **Boat mode ignores the exaggeration
slider** and renders at true 1x — from 1.8 m above the water, 8x turns a 20 ft
bottom into a canyon and puts every shallow at eye level, and the one view that
pretends you are on the lake is the one where the geometry has to be honest.

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

Sparse data is rescued by treating the shoreline as a depth-0 boundary: 2,557
synthetic zero-points densified along the 99 km shore and all 74 island edges
anchor the surface where transects do not reach.

These contours are an **interpolated surface, not a survey**. Between transects
they are an educated guess, from 1954 soundings, on a regulated lake whose level
moves. A rock does not appear in a 10 ft contour — contours are context for the
hazard layer, not a replacement.

### How sparse, exactly — the shape of the gap

"260 soundings, one per 13 hectares" was the old wording here and in the app. It
is arithmetically true and it misleads, because it implies even coverage. Page 2
of the survey sheet shows what actually happened: the depths sit in straight
east-west rows. A boat ran transects and sounded along them, and nothing between
them was measured at all.

`survey_geometry.py` recovers that structure from the data and is stable across
any grouping threshold from 50 to 120 m:

```
260 soundings -> 12 real transects
gap between transects: mean 533 m, min 430 m, max 662 m
```

`export_depth_grid.py` then ships a companion grid of metres-to-the-nearest-
sounding, so the app can draw the difference instead of describing it. The
**Survey reach** layer fogs water in proportion: clear where it was sounded,
opaque where it was guessed, and the twelve transects show through as stripes.
The boat HUD carries the same number next to the depth, because "18 ft" a metre
off a transect and "18 ft" 600 m from anything are not the same claim.

The numbers are worse than the transect spacing suggests, because the eastern
arm and the outlet were barely sounded at all:

```
42% of water cells sit more than 200 m from any measurement
furthest water from a sounding: 1859 m
```

### A denser survey of this lake exists — it is just not public

The i-Boating web viewer (`fishing-app.gpsnauticalcharts.com`) renders this lake
with full contours and spot soundings. Nineteen of its soundings were read off
the render, converted to lat/lon, and checked against ours after validating the
projection against their shoreline (8/9 land-water probes agree): **none landed
within 40 m of any of our 260, and the depths disagree by ~9 ft on average.** It
is an independent survey, not a redraw of the 1954 sheet.

It is proprietary and cannot be shipped here. Two honest routes to better depth
remain: ask MDIFW whether they hold a newer digital survey, or run transects with
a logging fishfinder. Note also that Maine's `LakeDpth` holds 3,717
GPS/depthfinder-surveyed points statewide — and zero of them on this lake. Every
point in this bbox is `depthmap: meifw`, digitised off the paper sheet.

Satellite-derived bathymetry was tested as a shortcut and **does not work on this
lake** — see `probe_sdb.py`, which records the measurement and the control that
settles it.

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
