# Handoff: survey reach — how much of the depth surface was actually measured — 2026-08-05

Session started as "make shoalrun prettier, add a contour slider, build a 3D viewer"
and ended somewhere more useful: establishing what the 1954 depth data actually is,
and building the app layer that shows it. **Rock detection was off-limits all session
— a parallel session owns it.** Nothing here touches it.

## What shipped

Commits, newest first (`git log --oneline`):

```
df87bc9 docs: record the shape of the survey gap, and that a denser one exists
0f62135 fix: hold the screen awake while GPS is running     <- PARALLEL SESSION, not mine
9a56681 feat: boat HUD says how far the nearest 1954 sounding is
60d56dc feat: a layer that shows which water the 1954 survey actually reached
6839e1d chore: stop tracking derived bathymetry grids        <- filter-repo cleanup
93444e4 fix: say the survey ran 12 lines, not "one sounding per 13 hectares"
76950ee test: satellite-derived bathymetry does not work on this lake -- measured
```

### The finding the rest hangs off

Page 2 of `data/millinocket_survey.pdf` shows the 1954 soundings sitting in straight
east-west rows. A boat ran transects and sounded along them; nothing between them was
measured. `scripts/survey_geometry.py` recovers that from the data:

```
260 soundings -> 12 real transects
gap between transects: mean 533 m, min 430 m, max 662 m
stable at grouping thresholds 50/70/90/120 m (11 at 150 m)
```

"260 soundings, one per 13 hectares" — the old wording in the UI and README — is
arithmetically true and implies even coverage the survey never had. Replaced everywhere.

### Survey reach layer (`60d56dc`)

`export_depth_grid.py` now ships a companion grid of metres-to-nearest-sounding
alongside the depth grid. `depth.js:reachCanvas()` renders it as a veil; **Survey reach**
toggle in the layers panel. Clear where sounded, opaque where guessed — the twelve
transects read as stripes and the eastern arm greys out almost entirely.

```
42% of 55,178 water cells sit >200 m from any measurement
furthest water from a sounding: 1859 m
```

### Boat HUD (`9a56681`)

3D viewer boat mode now shows "N m to nearest 1954 sounding" under the depth. Amber
past 250 m. Verified live at two spawns: NEOC 8 ft / 216 m, deep basin 45 ft / 32 m.

### SDB null (`76950ee`)

Satellite-derived bathymetry measured and rejected. Held-out R² is **negative at every
depth cut**, and the control settles it: NIR — which water absorbs almost completely and
so cannot carry depth — correlates with depth twice as hard as green (+0.178 vs +0.090).
Tannic lake; no signal in the photons. `scripts/probe_sdb.py` carries the numbers.

## Tests

```
$ node --test web/*.test.mjs
# tests 53
# pass 53
# fail 0
# skipped 0
```

Per file: `flags` 9, `hazard` 22, `swept` 15, `sync` 7.

**Correction worth carrying forward:** I reported "22/22 pass" repeatedly this session.
That was `hazard.test.mjs` alone. The real suite is four files and 53 tests. Run the
glob, not the single file.

There is **no Python test suite** — `.venv/bin/python -m pytest` reports
`No module named pytest`. Python scripts are verified by their own asserts and printed
output, not by tests.

## What's WIP

- **Working tree is clean.** `git status --short` returns nothing.
- **Branch:** `master`. One worktree only (`/home/nate/Projects/shoalrun`).
- **22 commits unpushed.** See the push gotcha below — this is the one blocking item.

## Known issues & gotchas

### Push is blocked and needs a human, and NOT with --force

A `git filter-repo` run (someone else, this session) rewrote history to drop
`data/ratio.npy` / `data/water.npy`. The note handed to me said to push with `--force`
because "nothing was ever pushed" and "every SHA changed". **Both are wrong:**

- `origin/master` had 13 commits at `7d306c0`.
- `7d306c0` and its 12 ancestors kept their SHAs — filter-repo only rewrote commits
  above the point where the `.npy` files entered.
- `git merge-base --is-ancestor origin/master master` returns true.
- `git push --dry-run origin master` shows `7d306c0..<tip>` with no `+` — a plain
  fast-forward.

So force is unnecessary here, and the stated reason it was "safe" was false. Push
normally. My own push attempt was refused by the permission classifier, so it needs to
be run by hand:

```
! git -C /home/nate/Projects/shoalrun push origin master
```

### A parallel session is live in this repo

`0f62135` (screen wake lock) landed mid-session from another Claude. Consequences:

- I amended `9a56681` while they were active. Their commit is a child of it, so nothing
  orphaned — but do not amend the tip again without checking `git log` first.
- `git add -A` will sweep up their in-progress edits. It nearly did; check
  `git status` before staging broadly.

### Regenerating dropped data

`data/ratio.npy` and `data/water.npy` are gone from the working tree. Regenerate with
`scripts/derive_bathymetry.py` (~20 min) only if needed. `meta.json` and `fit.json`
survived at **`data/sdb/`** (not `data/`), so the fit results are still on disk.

### Two bugs I made, and the shape of them

- **`rm -f dist/foo.html` silently did nothing.** The Bash tool resets cwd to
  `/home/nate/Projects` between calls, so `dist/` resolved to a path that does not
  exist and `-f` swallowed it. 4.8 MB of scratch previews got committed as a result.
  Cleaned, and `dist/_*.html` is now gitignored. **Use absolute paths in cleanup.**
- **Headless screenshots verified the wrong thing twice.** A white veil over the chart
  theme was invisible (chart deep water is already white), and a squared opacity falloff
  hid the transects it existed to show. Neither was visible in the code; both were
  obvious in the render. Look at the picture.

### Environment

- A `python3 -m http.server 8891` is still running from this session (pid 25703,
  cwd `dist/`). Kill it or reuse it.
- Build with `.venv/bin/python scripts/build_app.py` — the system python lacks pyproj,
  scipy, shapely.

## Next session's first 3 steps

1. **Push.** Run the `!` command above. Nothing else should happen until origin matches.
2. **Verify the night-theme veil.** It is the one thing shipped unlooked-at. Open the
   map, switch theme to Night, enable **Survey reach**, and confirm the near-black veil
   (`depth.js`, `[4, 7, 10, 0.7]`) reads against dark water the way the warm grey reads
   against the chart. Five minutes.
3. **Decide on anisotropic interpolation.** `make_contours.py:build_surface()` runs
   `griddata` isotropically, which interpolates across the 530 m north-south gaps with
   exactly the confidence it uses along a transect. That is the actual defect; the reach
   layer only describes it. It moves the surface that contours, the 3D mesh, and
   `validate_depth.py` all read from, so it wants its own session and a before/after.

Not urgent: tint the 3D bottom mesh by reach, the way the 2D map does.

## Relevant file paths

Touched this session:

- `scripts/survey_geometry.py` — **new.** Measures the transect structure.
- `scripts/probe_sdb.py` — **new.** SDB feasibility, with the result recorded in-file.
- `scripts/export_depth_grid.py` — emits the reach grid; guard trips if a cell exceeds
  the encoding range (it did, at a 4 m quantum — step is 8 m now).
- `web/depth.js` — `reachAt`/`reachXY`, `reachCanvas`.
- `web/render.js` — `_drawSurveyReach`, `_blitGrid` (extracted from `_drawDepthRaster`).
- `web/app.js` — `btnReach` toggle, `showReach`/`reachNearM` state. **Also contains the
  parallel session's wake-lock work.**
- `web/viewer3d.js` — HUD reach readout.
- `web/index.template.html`, `web/viewer3d.template.html` — toggle, legend, caveats.
- `README.md` — "Depth data" section rewritten.
- `.gitignore` — `dist/_*.html`.

Reference:

- `data/millinocket_survey.pdf` page 2 — the transect rows, visible by eye.
- `data/depth_grid.json` — `reach_step_m: 8.0`, `reach_max_m: 1859`.
- `docs/handoffs/2026-08-05-imagery-cannot-see-depth.md` — companion finding.

## Where better depth actually is

Searched properly this session so nobody repeats it:

- **A denser independent survey of this lake exists.** i-Boating
  (`fishing-app.gpsnauticalcharts.com`) renders it with full contours. 19 of its spot
  soundings, converted to lat/lon and checked after validating the projection against
  their shoreline (8/9 land-water probes agree), landed **0 within 40 m** of any of our
  260, with depths disagreeing ~9 ft on average. Not a redraw of the 1954 sheet.
  Proprietary; cannot ship.
- **Maine `LakeDpth` holds 3,717 GPS/depthfinder points statewide — zero here.** All 260
  in this bbox are `depthmap: meifw`, digitised off the paper sheet. Our extract is
  complete; nothing was dropped.
- **USGS inland bathymetry inventory: 0 features** over this bbox.
- **3DEP lidar `ME_EasternME_2017_A17`** covers the lake but is near-IR topographic —
  water surface only, no bottom.
- Remaining honest routes: ask MDIFW for a newer digital survey, or run transects with a
  logging fishfinder.
