# Handoff: reading depth off the lake level — measured, and it does not work — 2026-08-08

**Short version: passive optical detection of submerged rock on this lake is closed.
Not "needs tuning" — closed. Two independent physical routes have now been measured
and both are null. The remaining honest route is sonar.**

This handoff exists so nobody spends another day on it. Everything below is a
measurement with the script that produced it, not an opinion.

## The idea, and why it looked good

Every optical method tried here asks the water to be transparent, and it is not:
satellite-derived bathymetry scored **AUC 0.507** against the 260 MDIFW soundings,
a coin flip, and the control finished it — NIR, which water absorbs almost
completely and so cannot carry depth, correlated with depth *twice as hard* as
green (`probe_sdb.py`). That is why 3,549 of 4,908 hazards sit at `unverified`.

The way around it is to stop looking through the water. This lake is regulated, so
a rock near the top of the column stands in **air** at low water and drowns at
high water. Dry rock returns near-infrared — a direct look at a surface, the one
optical measurement here that survived the depth null. Photograph the rock before
it goes under, and the depth is the difference between two lake levels. The water
never has to be transparent.

It also fixed a real defect in `detect_naip.py`, which counts how many flights a
pixel looked like land in (`naip_flights: 4.455`) and discards *which*. Six flights
are six different lake levels, not six replicates, and counting actively penalises
the most dangerous hazard there is: dry in two flights and drowned in four reads as
"inconsistent" when that inconsistency **is** the depth measurement.

## Why it fails

**The stage ladder cannot be measured, and it cannot be inferred either.**

### Measuring it: four attempts, four different answers

Water area is a monotone proxy for stage on a fixed basin, so the plan was to rank
the six NAIP flights by their own measured water area. Every attempt disagreed with
the last:

| rule | result |
|---|---|
| `NDWI > 0`, no collar | saturated — five flights within 26 ha of the polygon's own 3369 ha ceiling |
| `NDWI > 0`, 200 m collar | 2018 reads +286 ha; its extra ring has median NDWI **+0.065** — marsh, not lake (real water here is +0.75 to +0.85) |
| `NDWI > 0.40` | 2015 reads 2186 ha, **63% of the lake** — the cutoff eats its real water |
| per-flight Otsu on a shoreline band | plausible areas, ladder `2021 < 2023 < 2011 < 2013 < 2018`, 205 ha spread |

**2015 moves from lowest to highest between rules three and four.** The ranking was
a property of my threshold, not of the lake.

`stage_stability.py` sweeps the cutoff 0.15→0.45 and watches every pair. 11 of 15
pairs hold. The failures concentrate in one flight: 2015's area moves **52% of
itself** across the sweep while no other flight moves more than 8%, because it is
the contrast-3.87 flight and its water sits too low in NDWI for any defensible
cutoff. Dropping it (as a measured rule, `STAGE_SENSITIVITY_MAX`) leaves a ladder
that survives the sweep.

**That ladder is still wrong.** `exposure_stack.py --control --shuffle 1` permutes
the stage order and reruns:

```
                    monotone_pass   cand_px   blobs
real stage order           1,502       147        1
shuffled (seed 1)         13,452     6,686       49
```

An ordering that loses **49 to 1** against a shuffle is not noisy, it is wrong. The
mechanism is per-flight radiometry: 2021 is the outlier of the six (contrast 25.74
against 2015's 3.87, water median 47.3 against 90.6), so a cutoff calibrated on the
others eats its real water, reports a smaller lake, and files a high-water year as
low. **The stability sweep can never catch this** — it moves one cutoff across all
flights together, so a per-flight bias passes straight through. Do not re-run the
sweep expecting it to find this class of error.

### Inferring it: no ordering information exists

`infer_stage_order.py` inverts the problem. A pixel is lawful under an ordering
exactly when its set of dry years is a **prefix** of that ordering, so counting how
many pixels have each dry-set turns all 120 orderings into lookups — and hands over
the null in closed form, since a pixel dry in k of n years is a prefix of
1/C(n,k) of them. No sampling.

```
120 tiles at 1 m -> 248 qualifying pixels

{2011,2013,2021,2023}   165   66.5%   <- dry in everything EXCEPT 2018
{2023}                   43   17.3%
{2013}                   25   10.1%
{2011}                    9    3.6%

best ordering  4.31x random expectation, 1.00x its runner-up
top five       211, 210, 209, 208, 208   (indistinguishable)
nesting        50% (chance)
```

Two independent failures:

1. **No ordering is preferred.** Best-of-120 always crowns a winner; this one beats
   its runner-up by 1.00x. The top five agree only on "2023 first, 2018 last" and
   are arbitrary in the middle three.
2. **The one apparent signal is a known artifact.** Two thirds of qualifying pixels
   are dry in every flight *except* 2018 — and 2018 is the flight already caught
   with a 288 ha wet-ground halo at NDWI +0.065. It is the rainy flight, not the
   high-water one. The remaining sets are singletons: pixels dry in exactly one
   year, which is what glint produces.
3. **Dry-sets do not nest.** This is the test that matters and it is structural, not
   statistical. A falling lake exposes everything the level above it exposed *and
   more*, so real dry-sets form a chain — {a} ⊂ {a,b} ⊂ {a,b,c}. Weather produces
   overlapping sets that refuse to nest, and no single ordering can make both {a,b}
   and {b,c} a prefix. Nesting came out at 50%, which is chance.

**Not a data-starvation artifact.** Relaxing the gates 3x
(`SHOALRUN_DRY_SIGMA=2 SHOALRUN_WET_SIGMA=8`) multiplies qualifying pixels ~5x and
changes nothing: best ordering 4.46x expected, **1.02x its runner-up**, top five
within 6% of each other. More pixels, same non-result.

### The physical reason, which was findable in advance

Six NAIP flights, all July–September. A regulated lake is held near full pool
through the recreation season and drawn down in autumn and winter. **Nobody flies
NAIP at low water.** The 417 ha swing Sentinel sees across 50 summer scenes is real,
but it happens on days that have no high-resolution imagery. Neither dataset has
both the resolution to resolve a rock and the stage sampling to expose one:

- **NAIP** 0.3–1 m, resolves a boulder, but six dates clustered at full pool.
- **Sentinel-2** 10 m, samples the full stage range across 70 scenes, but cannot
  resolve a rock — measured directly early on: known rocks fall in the wet/dry band
  42% of the time against a 54% control, i.e. *worse* than random.

## What is worth keeping

- **`priority_mask.py`** — stands on its own and is unaffected by any of this.
  Answers "where should resolution go" with distance measured to the **mainland
  ring only**, so water off an island still counts as open water (74 islands are
  interior rings in the lake polygon). 1,039 tiles: 237 at 0.3 m (915 ha, 27% —
  open water the 1954 survey never sounded), 591 at 0.6 m, 211 at 1.0 m.
- **`stage_stability.py`** — reusable. "Does this answer depend on my threshold?"
  is the question that caught the whole problem.
- **`infer_stage_order.py`** — the dry-set/prefix trick and the closed-form null
  are general. If high-resolution imagery at genuinely different stages ever
  exists, this is the analysis to run on it, unchanged.
- **28 tests** (`.venv/bin/python -m pytest tests/`). They caught three real bugs
  that each produce a confident wrong map with no visible symptom: a flat NDWI
  series scoring a perfect +1 through tied argsort ranks (open water reported as
  rock); ladder gaps costing a correctly-ordered pixel its 1.0; and Spearman being
  the wrong statistic outright — there is no bottom signal, so a rock does not
  fade as it drowns, it steps, and a planted synthetic rock scored 0.54 against a
  0.80 gate.

`exposure_stack.py` is correct and tested. Its inputs cannot support it. Leave it.

## Do not

- Re-run the threshold sweep hoping to catch the 2021 bias. It cannot, by
  construction — see above.
- Loosen `DRY_SIGMA`/`WET_SIGMA` until something passes. Already tried at 3x; the
  structure does not change, and tuning to make the control pass is fitting noise.
- Add more optical sources. Google and Apple imagery is RGB — no NIR band, and NIR
  is the entire basis of the land/water discrimination here. They are strictly
  worse for this, before any terms-of-service question.
- Ship the exposure candidates. 41 tiles containing 32 known rocks produced **1**
  detection, and the shuffled control produced 49.

## What would actually work

1. **Sonar.** A fishfinder with GPS logging, or a castable unit. Sound does not care
   that the water is stained, which is the exact reason every optical route has
   failed here. One season of ordinary boating produces a better depth map than the
   1954 lead-line survey, and the system to receive it is already built
   (swept-area layer, sync backend).
2. **3DEP lidar `ME_EasternME_2017_A17`.** Untouched. Near-IR topographic, so no
   bottom returns — but it gives measured elevations in **feet** for every rock
   exposed in 2017, plus the water-surface elevation that day. That is the only
   route here that yields real depths rather than ranks, and it needs no threshold.
3. **Ask MDIFW for a newer digital survey.** A denser independent survey of this
   lake demonstrably exists — i-Boating renders full contours, and 19 of its spot
   soundings landed 0 within 40 m of any of our 260, disagreeing ~9 ft on average.
   It is proprietary and cannot be shipped, but it proves better data was collected.

## Reproduce

```bash
.venv/bin/python scripts/priority_mask.py                      # tiles + target res
.venv/bin/python scripts/stage_stability.py                    # threshold sweep
.venv/bin/python scripts/exposure_stack.py --stage             # ladder (vetted)
.venv/bin/python scripts/infer_stage_order.py --limit 120      # the null
.venv/bin/python scripts/exposure_stack.py --control           # 41 tiles, 32 rocks
.venv/bin/python scripts/exposure_stack.py --control --shuffle 1   # 49 vs 1
```

`data/lake_stage.json` (the 40-year Landsat series) was **not** produced — that
script has a fix applied but was never re-run and its last output was wrong
(scenes reporting 0 ha, and areas above the lake's own size). Treat it as untested.
