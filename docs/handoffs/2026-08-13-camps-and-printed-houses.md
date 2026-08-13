# Handoff: camps sweep + houses on the printed model — 2026-08-13

Two threads ran together. The camps sweep **shipped and is live**. The printed
model is **built and waiting on hardware**. One thread is **unsolved and parked**,
and it is the interesting one — read "Known issues" before picking it up.

## What shipped

Commits (all on `master`, all pushed, nothing unpushed and nothing behind):

- `c7bfd8d` feat: find the camps OSM missed with Maine E911 addresses
- `d2c3a7d` feat: `#lat,lon,zoom` deep link so a spot on the lake is a URL
- `04365fc` feat: raise a house at every camp on the printed model

Later commits on the branch (`6776acb` … `4222672`) are another session's work on
the tab layout, phone/Safari fixes and hosting — not this one's.

### Camps
- Maine E911 addresses fetched and clipped: **144 within 400 m of the water**,
  **39 more than 75 m from anything OSM traced**. Whole roads with nothing on
  them in OSM: Evergreen Way, Rolands Way, Beech Lane, Puckerbrush Trail.
- **55** merged into the map after a 40 m dedupe, drawn as hollow rings.
- **Verified live**: `https://sproultech.com/shoalrun/` returns HTTP 200 and
  contains 55 occurrences of `"kind":"address"`.
- Structure layer went 271 → 326.

### Printed model
- `--structures` now raises a gabled house glyph per camp instead of a flat disc.
- **263 houses** (OSM buildings + camps + E911 addresses) and 38 pier pads.
- `dist/print/millinocket.stl` — 200 × 153 × 43 mm, 314,364 triangles,
  closed manifold, 243 cm³, **108 g at 15% infill**, ~12 h.
- Viewer rebuilt with the houses painted and visible by default.

### Tests

```
.venv/bin/python -m pytest -q      # 102 passed, 1 warning
node --test web/*.test.mjs         # pass 91, fail 0
```

New this session: `tests/test_build_structures.py` (10 tests on the E911 merge
and its metres-vs-degrees dedupe).

## What's WIP

- **Nothing uncommitted.** `git status -sb` is clean on `master`.
- No worktrees beyond the main checkout.
- `scripts/detect_docks.py` is committed but **has never been run** — the user
  stopped it before the first execution. Treat its output claims as unverified.

## Known issues & gotchas

### The camps are still not all found. Five detectors failed.

The user looked at the map and said houses were missing. **He is right** — an
overlay of the map's markers on 2023 NAIP shows camp after camp with a dock and
no marker. Every automated attempt to find them failed, each verified against
imagery before being believed:

| attempt | outcome |
|---|---|
| 3DEP lidar height alone | 11,431 hits, median height 10.9 m — trees |
| Microsoft US Building Footprints | 104 in bounds, only **3** OSM lacked |
| NAIP "not vegetation" (NDVI) | 1,901 hits; random 12 contained **zero** roofs |
| NDVI **and** lidar height | detections in open water; masking water left boulder fields |
| rectangularity (min rotated rect) | 99 hits; random 12 were canopy shadow gaps |

Root cause of all five: they test "is this patch bare", and a September drawdown
shoreline is full of bare things that are not buildings — exposed lakebed, bog,
boulder fields, gravel roads — while the roofs that exist sit under closed spruce
canopy. **Do not tune these further without a labelled set to score against.**

Do not re-run the lidar-height or MS-footprints routes. They are measured dead
ends, recorded in memory `reference_shoalrun_camps_e911.md`.

### Gotchas paid for this session

- **E911 points are placed off imagery and land 20–40 m from the actual roof.**
  Dedupe at 40 m, not 10, or half the shoreline double-draws.
- **The ArcGIS Hub DCAT feed 404s.** Find Maine services via the AGO search API
  (`https://www.arcgis.com/sharing/rest/search`), not the Hub catalog.
- **House glyphs must drape over each cell's own ground.** Basing the glyph on
  the highest cell under its footprint lifted houses near the shoreline cliff by
  **48 mm**; basing it on the centre cell still spiked to **26 mm** where a
  corner overhung a cliff. Measured max rise is now 1.6 mm — check that number
  after any change to `raise_houses`.
- **The print viewer defaulted markers to OFF** behind a button labelled "Show
  measurements", and a 1.6 mm house on a 43 mm model is a rounding error in an
  elevation colour ramp. Both fixed; if houses "disappear" again, check the
  `mk` mask in the payload and the `aMark` attribute binding.
- **Background tasks do not survive session turnover.** The local preview server
  died three times. Restart with
  `cd dist && python3 -m http.server 8584 --bind 127.0.0.1`.
- **Large data is gitignored**: `data/hag_2m.npz` (42 MB) and `data/cache/`
  (Microsoft's 188 MB state zip). Both regenerable; commands are in the scripts'
  docstrings.

### Hardware — this is the actual blocker

- **The OctoPi Pi is not on the network.** Swept all 254 addresses on
  `192.168.1.x`: ten hosts up, no Raspberry Pi MAC prefix, nothing answering
  OctoPrint's API, `octopi.local` does not resolve, `.55` (its old DHCP address)
  is dead.
- Ruled out: network is fine (gateway up), SSID unchanged (laptop is on
  `Linksys444`, what the Pi was configured for), Tapo camera at `192.168.1.52`
  is up so the printer corner has power and coverage.
- Prime suspect is the Pi's PSU. Setup notes recorded
  `vcgencmd get_throttled = 0x50000` — under-voltage and throttling had already
  occurred — and recommended a proper 5 V/3 A USB-C supply before long prints.
- **Do not print this over USB.** The CR-10's FT232R drops off the bus mid-print
  and the board does not reset; a previous print died at 32% with the nozzle
  parked on the part at 205 °C for four minutes. See memory
  `reference_cr10_usb_direct_printing.md`.

## Next session's first 3 steps

1. **Check whether the Pi is up**: `ping -c2 octopi.local`. If it answers,
   reconnect the printer —
   `POST http://octopi.local/api/connection {"command":"connect","port":"/dev/ttyUSB0","baudrate":115200}`
   with header `X-Api-Key` (read it off the Pi:
   `ssh octopi 'grep -i apikey ~/.octoprint/config.yaml'`; never write the value
   into a doc). Confirm the Tapo feed:
   `http://octopi.local:1984/api/frame.jpeg?src=tapo`.
2. **Print a 2 mm first-layer test before the 12 h job.** Same 200 × 153 mm
   footprint, same corners, ~25 min. Warping on a flat slab with 704 mm of
   perimeter is the failure mode this shape invites, and it shows in the first
   three layers or not at all. Only then push the full
   `dist/print/millinocket.stl`.
3. **Decide the camps thread.** `scripts/detect_docks.py` is written and unrun:
   docks sit on open water where no canopy can hide them, and OSM's 38 traced
   piers are a labelled set to score recall against. Run it, score it, and if it
   misses those 38, say plainly that this needs a person tracing from the aerial
   rather than a sixth classifier.

## Relevant file paths

Written this session:
- `scripts/fetch_e911.py` — Maine E911 address points, paged, clipped to the lake
- `scripts/detect_docks.py` — **unrun**; the sixth idea, scored against OSM piers
- `scripts/detect_roofs_naip.py` — the failed rectangularity detector; its
  docstring is the record of what each attempt actually measured
- `scripts/detect_structures.py`, `scripts/fetch_ms_buildings.py` — the lidar and
  Microsoft routes, both dead ends, kept for the evidence
- `tests/test_build_structures.py`

Modified:
- `scripts/build_structures.py` — merges E911 into `data/structures.geojson`
- `scripts/make_stl.py` — `raise_houses()`, `mark_structures()`, `--house-mm`
- `scripts/make_print_viewer.py` — ships the `mk` marker mask and `markNoun`
- `web/printview.template.html` — `aMark` attribute, painted houses, markers on
- `web/render.js` — theme-coloured structure labels, collision declutter
- `web/app.js` — `#lat,lon,zoom` deep link
- `DATA-LICENSE.md` — E911, Microsoft (ODbL) and 3DEP attribution

Artifacts:
- `dist/print/millinocket.stl` + `.txt` (the notes say the houses are a glyph)
- `dist/print/viewer.html`

Memory:
- `reference_shoalrun_camps_e911.md` — why lidar and imagery are blind here
- `reference_cr10_usb_direct_printing.md` — the FT232R dropout
- `project_3dprint_octoprint.md` — the Pi, its API, and its marginal PSU
