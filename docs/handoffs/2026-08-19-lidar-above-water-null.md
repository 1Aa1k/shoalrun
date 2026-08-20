# Handoff: lidar cannot find rock on this lake either — 2026-08-19

Third remote-sensing route measured on Millinocket Lake, third null. Airborne
lidar returns from inside the lake polygon do **not** find the rocks people have
already mapped. Against a shore-matched null the detector is worse than random,
in every height band, and it fires on known-empty water two to three times more
often than chance.

**Do not ship `data/rocks_lidar.geojson` as a hazard layer.**

## Why it looked like it would work

The two optical nulls share one cause: no photon comes back off the bottom
through tannic water. Lidar at 1064 nm does not penetrate water either, and that
is the appeal — a return from inside the lake polygon is a return off something
solid that was above the surface on the day. No inference, no water column.

Two supporting measurements held up and are still true:

| finding | number |
|---|---|
| in-lake voids preserved in the DSM | **3.19%** — gridded returns, not an interpolated skin |
| water surface at flight time | **145.668 m, sd 3.2 cm** = 477.9 ft |
| published full pool | 483 ft |
| so the 2017 collect caught the lake | **~5 ft down, in drawdown** |
| local surface deviation from the global plane | −6.2 to +5.7 cm (p1–p99) |

That drawdown is the part worth remembering. All six NAIP flights are
July–September on a regulated lake held near full pool, which is exactly why the
lake-stage exposure route died. The lidar saw this lake lower than any aerial
photograph of it ever has.

## What actually killed it

Canopy. The same thing that killed the camps sweep on this same shoreline, where
3DEP HAG returned 11,431 detections at a median 10.9 m and they were trees.

Spruce leaning off the bank returns lidar from inside the lake polygon, metres
above the water. No shore buffer can exclude it and still keep rock: **the
reference rocks sit a median 1.6 m from the shoreline.** The first run used a
10 m buffer, which deleted the entire population it was then scored against.

## The measurements

32 OSM reference rocks, 30 m match radius, 21 trials. The `open_water` column is
the 137 marks in `hazards.geojson` where somebody looked and found nothing — a
detector should stay quiet there.

| band (m) | n | recall | shore-matched null | lift | fires on open water | null |
|---|---|---|---|---|---|---|
| 0.15–0.5 | 1309 | 28.1% | 31.2% | **−3.1%** | 42.3% | 15.3% |
| 0.15–1.0 | 1829 | 31.2% | 43.8% | **−12.5%** | 51.1% | 19.0% |
| 0.5–1.0 | 520 | 15.6% | 18.8% | **−3.1%** | 24.8% | 6.6% |
| 1.0–2.0 | 287 | 6.2% | 12.5% | **−6.2%** | 6.6% | 3.6% |
| 2.0–99 | 506 | 9.4% | 31.2% | **−21.9%** | 6.6% | 6.6% |
| all | 2622 | 40.6% | 59.4% | **−18.8%** | 56.2% | 25.5% |

Every band negative. Every band over-fires on water that was checked and empty.
There is no threshold, no height band, and no area filter that rescues this.

## The control is the whole story

Against a **uniform** null — points scattered anywhere in the lake, which is what
`recall_check.py` uses — the full detector scores **40.6% vs 15.6%, a +25% lift**,
and reads as a success.

Against a **shore-matched** null — points drawn to match the detections' own
distribution of distance from shore — it scores **40.6% vs 59.4%, a −18.8% lift**.

Same detections, same references, opposite conclusion. Rocks hug the shoreline
and so does every tree, dock and moored boat that returns lidar near the
waterline, so a uniform null spreads its points over open water where no
reference rock is and hands the detector lift it did not earn.

This is the same failure as the threshold sweep that nearly shipped in the
lake-stage work: a control that looks like a check and cannot catch the error.
**Match the null to the detector's own spatial habit, or the null proves
nothing.**

## Reproduce

```bash
.venv/bin/python scripts/fetch_terrain.py --collection 3dep-lidar-dsm \
    --res-m 2 --pad-m 0 --out data/dsm_2m.npz     # 52 MB, gitignored
.venv/bin/python scripts/detect_lidar.py
.venv/bin/python scripts/score_lidar.py
```

Run on `desktop-sbei95r` (WSL Ubuntu-22.04). The repo pins a 3.11+ toolchain and
that box is 3.10, so its `.venv` has the same packages installed unpinned; none
of this depends on a version.

## What is left

Unchanged from 2026-08-08, minus one more option:

- **Sonar.** A GPS-logging fishfinder. One season beats the 1954 lead-line
  survey, and the receive side is already built (`read_sl2.py`).
- **Ask MDIFW for a newer digital survey.** A denser one demonstrably exists —
  i-Boating renders it — but it is proprietary.

The synthetic-training-data detector idea rested on lidar supplying free labels
to train and validate against. It does not supply them. Anything built that way
now has nothing on this lake to check itself against, which is the same position
the unverified 72% of the hazard map is already in.
