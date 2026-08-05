# Handoff: aerial imagery cannot see depth on this lake — 2026-08-05

The session's headline is a negative result, and everything else follows from it.
**Aerial imagery carries no depth information on Millinocket Lake.** Measured, not
assumed: AUC 0.507 separating shallow (<=10 ft) from deep (>=25 ft) soundings — a
coin flip. That invalidated 72% of the hazard map and redirected the work toward
evidence that does not require seeing through water.

## What shipped

Branch `master`, 35 commits total, **22 unpushed**. Working tree clean.

```
df87bc9 docs: record the shape of the survey gap, and that a denser one exists
0f62135 fix: hold the screen awake while GPS is running
9a56681 feat: boat HUD says how far the nearest 1954 sounding is
60d56dc feat: a layer that shows which water the 1954 survey actually reached
6839e1d chore: stop tracking derived bathymetry grids
93444e4 fix: say the survey ran 12 lines, not "one sounding per 13 hectares"
eb708b1 feat: sync backend, and a handoff doc for giving this to someone else
0be3541 feat: one-tap reports with owner review, and automatic sync instead of files
437ecc1 feat: guest mode and boat-to-boat sharing of driven water
76950ee test: satellite-derived bathymetry does not work on this lake -- measured
f6b0c1a feat: swept-area layer -- water proven by having driven it
54a649d feat: camera sway toggle in boat mode
```

Two sessions committed to `master` in parallel all day. Both sets are interleaved
and merged; there is no branch to reconcile.

### Measurements (the durable output)

| finding | number |
|---|---|
| imagery separating shallow from deep | **AUC 0.507** (coin flip) |
| Stumpf SDB held-out prediction | RMSE 6.69 ft vs 6.56 ft baseline — worse than guessing the mean |
| cause: position error | **ruled out** — window 1 m→50 m leaves r flat at 0 |
| cause: depth limit | confirmed — r dead past 12 ft |
| naip-1m recall vs same-size random null | 97% vs 56% = **+41%** (not the +77% previously claimed) |
| offshore depth skill, naip-1m | 0.1 ft vs 3.3 ft shore-matched null |
| offshore depth skill, naip-bright-1m | 7.6 ft vs 8.8 ft — near zero, it catches glint |
| best tuned edge config | +5.7 offshore lift vs naip-1m's +6 — statistically the same |
| flight quality (whole-lake, 12 chips) | 2021 = 25.74, next best 9.24; single-chip test had ranked 2018 second, it is fifth |

### Hazard re-tiering (safety-relevant)

`data/hazards.geojson`, 4,908 features:

- **confirmed 48** — above the waterline, cross-checked at 0.3 m
- **likely 1,311** — returns NIR, so a dry surface
- **unverified 3,549** — rests on bottom-through-water, which is impossible here

`hazard.js` now weights severity by tier. Previously `shoal: 4` outranked
`island: 1`, so 3,549 unevidenced markers outranked every confirmed hazard.
Time-to-collision still dominates the sort — evidence can never displace the thing
you are about to hit.

### New capability

- **Swept area** (`web/swept.js`) — water proven by having driven it. The only
  evidence source immune to water clarity.
- **Flags + review** (`web/flags.js`) — guests tap once; a pending flag alerts
  only its reporter until someone who knows the lake confirms it.
- **Auto-sync** (`web/sync.js` + `server/worker.js`) — Cloudflare Worker, lake
  code not accounts. **Inert until a code is set; nothing leaves the device.**
- **Guest mode** — hides the unverified layer. Shows less, deliberately.

### Tests

```
$ node --test web/*.test.mjs server/*.test.mjs
# tests 61
# pass 61
# fail 0
```

All new guards mutation-checked: accepting bad GPS fixes fails 2; bridging
logging gaps fails 1; removing tier weighting fails 2; letting pending flags
alert everyone fails 1; merging by timestamp instead of evidence strength fails 1.

## What's WIP

Nothing. Working tree clean, no worktrees beyond the main checkout.

## Known issues & gotchas

**1. 22 commits unpushed; requires `--force`.** History was rewritten today with
`git filter-repo` to strip `data/sdb/{ratio,water}.npy` (357 MB) that GitHub
rejects at the 100 MB limit. Every SHA changed, so `origin/master` (`7d306c0`) is
no longer an ancestor. The force-push is blocked by the harness classifier — a
human must run it:

```bash
git -C /home/nate/Projects/shoalrun push --force origin master
```

Full pre-rewrite history is preserved at
`/home/nate/Projects/shoalrun-backup-20260805-0207.bundle` (100 MB).

**2. Any other checkout of this repo is on orphaned SHAs.** After the push, it
needs `git fetch && git reset --hard origin/master` or its next commit forks off
dead history.

**3. `docs/` is a stale GitHub Pages publish dir.** Tracked, 1.5 MB from
2026-08-04 20:55; `dist/index.html` is now 2.47 MB. Whatever is live is missing
everything from today. Republish by copying `dist/{index.html,manifest.json,sw.js}`
into `docs/`.

**4. `.npy` files are gone from the working tree.** `filter-repo` removed them
along with the history. `data/sdb/` retains only `meta.json` and `fit.json`.
Regenerate with `scripts/derive_bathymetry.py` (~20 min) if needed — but the fit
is a documented failure, so probably do not bother.

**5. 270 MB of annotator chips in `dist/annotate/chips/`.** Gitignored,
regenerable, and effectively dead — Nate rejected the manual-annotation approach.
Safe to delete.

**6. No lake-level handling.** Every swept cell is timestamped and
`SweptGrid.staleCount()` exists, but nothing calls it. If this lake is dammed, a
track proven at full pool will keep rendering green after a drawdown. Unresolved:
whether a gauge exists for this lake.

**7. `scripts/detect_buoys.py` has never been run.** Maine DACF has zero buoys
registered on this lake, so it may return nothing.

**8. Environment.** No servers left running (the `python3 -m http.server`
instances on 8947/8952 are dead). Python work needs `.venv/bin/python`, not
system python — `planetary_computer` is not installed globally. Desktop
`desktop-sbei95r` has a stale `~/shoalrun` checkout used for the evidence-grid
run; WSL2 tears down the VM on SSH close, so long jobs there need `schtasks`, not
`nohup`.

**9. No migrations in this project.** Not applicable.

## Next session's first 3 steps

1. **Push.** `git -C /home/nate/Projects/shoalrun push --force origin master` —
   a human must run it; the classifier blocks force-pushes to master.
2. **Republish `docs/`** from `dist/` so the live page is not a day behind, then
   confirm the deployed page loads and geolocation works over HTTPS.
3. **Settle the lake-level question** — find whether a gauge or dam-operator
   reading exists for Millinocket Lake. It is the largest correctness gap in the
   swept-area layer, and it gates whether "driven water" can be trusted across
   seasons.

## Relevant file paths

**Evidence and validation**
- `scripts/derive_bathymetry.py` — Stumpf SDB fit, cross-validated. The negative.
- `scripts/diagnose_sdb.py` — separates depth limit / position error / water colour.
- `scripts/validate_depth.py` — hazards vs soundings, shore-distance-matched null.
- `scripts/recall_check.py` — recall with the null control built in.
- `scripts/tune_edge.py` — threshold sweep against the offshore depth metric.
- `scripts/rank_flights.py` → `data/flight_quality.json`

**Detection**
- `scripts/detect_naip.py` (the layer that actually performs), `detect_bright.py`,
  `detect_edge.py`, `merge_candidates.py`, `retier_hazards.py`

**App**
- `web/swept.js`, `web/flags.js`, `web/sync.js` — this session's new modules
- `web/hazard.js` — tier-weighted severity; the safety-critical sort lives here
- `web/app.js` — wake lock, swept accumulation, flag + review UI
- `server/worker.js`, `server/wrangler.toml` — sync backend, undeployed

**Docs**
- `HANDOFF.md` — for giving the tool to a business (NEOC). Leads with the
  measurement, not the deploy steps.
- `README.md` — survey structure and the search for better data

**Dead ends, kept deliberately**
- `scripts/build_annotator.py`, `web/annotate.template.html`, `filter_sections.py`,
  `rank_surprise.py`, `ingest_marks.py`, `score_marks.py` — manual annotation,
  rejected by Nate. Code retained; do not resume without a reason.

## The one thing worth carrying forward

Optical methods are at their ceiling here — that is measured, not a guess. The
swept-area layer, the flag/verify flow and the sync backend are all built to
receive **sonar**: a fishfinder with GPS logging, or a castable unit, on a boat
that is going out anyway. Sound does not care that the water is stained, which is
exactly why the optical approach failed. A drone gets sharper pixels of the same
thing and does not touch the limit.
