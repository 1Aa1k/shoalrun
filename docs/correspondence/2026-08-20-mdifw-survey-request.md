# MDIFW bathymetry request — SENT 2026-08-20

Gmail message ID `1a01d621be49a796`, from nathanielsproul04@gmail.com to
Kevin.Gallant@maine.gov and zachary.glidden@maine.gov. No reply yet.

If nothing comes back inside two weeks, try Region E (Greenville) rather than
chasing Region F: the lake straddles the line and Piscataquis is a fair claim.

**To:** Kevin.Gallant@maine.gov, zachary.glidden@maine.gov
Region F (Penobscot), 16 Cobb Road, Enfield, ME 04493, (207) 794-1003
Kevin Gallant, Fisheries Resource Supervisor · Zachary Glidden, Resource Biologist

**If Region F redirects:** Region E (Greenville), PO Box 551, Greenville, ME 04441,
(207) 695-3756 press 2. Jeff.Bagley@maine.gov (Fisheries Resource Supervisor),
Stephen.Seeback@maine.gov (Resource Biologist).

The lake straddles the line: the survey sheet lists T1 R8 / T2 R8 Penobscot Co.
and T1 R9 / T2 R9 Piscataquis Co. Region F is the Penobscot region, so it is the
better first ask. Both sets of contacts verified against the department directory
on 2026-08-20.

---

**Subject:** Millinocket Lake (T1 R8) bathymetry, is there anything newer than the 1954 survey?

Hello,

I'm a Maine resident and I've built a free offline hazard map for Millinocket
Lake, the 8,960 acre one in T1 R8 and T2 R8 Penobscot County. It's at
sproultech.com/shoalrun. No accounts, no ads, and it works with no cell service,
which is most of that lake.

It is built on your own survey sheet, August 1954 revised January 1979. I
digitized the 260 soundings from the state's depth-map tiles and interpolated
them into contours. The sheet is credited in the app.

My question: does the department hold a newer or denser digital survey of this
lake, and if so, is it something you can share or license?

Two things led me to ask rather than assume.

First, a denser independent survey clearly exists somewhere. The i-Boating
charts render this lake with full contours, and when I checked 19 of their spot
soundings against ours, not one landed within 40 m of any of our 260, and the
depths disagreed by about 9 feet on average. That is not a redraw of the 1954
sheet, it is separate measurement by somebody.

Second, I checked the state's own modern data first. Maine's LakeDpth layer
holds 3,717 GPS depthfinder points statewide and has zero on this lake. All 260
points here are the digitized 1954 sheet.

The 1954 survey ran twelve east-west transects. Between those lines the map is
interpolation rather than measurement, and this lake is rocky enough that the
gap matters to anyone running a prop.

If there is a newer dataset, I would use it under whatever terms you set and
credit the department. If there isn't one, I would rather know that than keep
guessing, and I'll say so plainly in the app.

Happy to share what I've built back to you either way.

Thanks for your time,

Nate Sproul
nathanielsproul04@gmail.com

---

## Fact check, 2026-08-20

Every number verified against the repo or the source document before sending.

| claim | status | source |
|---|---|---|
| 8,960 acres | verified | the 1954 sheet's own "Area" field |
| T1 R8 / T2 R8 Penobscot, T1 R9 / T2 R9 Piscataquis | verified | sheet header |
| August 1954, revised January 1979 | verified | `export_depth_grid.py`, sheet |
| 260 soundings | verified | `data/soundings.geojson`, 260 features |
| all 260 are `depthmap: meifw` | verified | feature properties |
| twelve east-west transects | verified | `priority_mask.py`, `web/depth.js` |
| i-Boating: 19 spot soundings, 0 within 40 m, ~9 ft disagreement | verified | `docs/handoffs/2026-08-05-survey-reach.md` |
| LakeDpth 3,717 points statewide, zero here | verified | same handoff |
| app live, free, offline, no ads | verified | HTTP 200, no ad code in `dist/index.html` |
| sheet credited in the app | verified | "MDIFW soundings Aug 1954 (rev Jan 1979)" in `dist/index.html` |

### Three things corrected out of the first draft

1. **"below Ambajejus, with the Great Northern pump house landing."** Cut. The
   1954 sheet says access is by a road from the Baxter State Park road at
   Ambajejus Lake to the Great Northern Paper Company pump house. It does not say
   the lake sits below Ambajejus, and I had inferred that. Great Northern Paper
   is also long defunct, so describing a landing by its name in 2026 reads as
   somebody working off a seventy year old document, which undercuts the ask.
   Replaced with the township, which is both verified and disambiguating: Maine
   has more than one Millinocket Lake, and naming the wrong one wastes the
   biologist's time and yours.

2. **"roughly 34 km2."** That was my own figure off the OSM polygon (34.48 km2,
   8,521 acres). Swapped for the sheet's own 8,960 acres. Their number, their
   units, and it removes a discrepancy they would otherwise have to reconcile.

3. **"renders at a resolution the 1954 sheet cannot support."** True but soft.
   Replaced with the actual measurement, which is far harder to wave off: 19 spot
   soundings, none within 40 m of any of ours, about 9 feet apart on average.

### One thing added

The LakeDpth check. It preempts the obvious first reply, which is to point you
at the state's existing depthfinder layer, and it shows you did the homework
before writing. Costs one sentence.

### Deliberately not in it

It does not ask them to hand over third-party proprietary data. It asks what the
department holds and on what terms. The i-Boating figures are offered as evidence
that better data was collected, not as a request for i-Boating's data.
