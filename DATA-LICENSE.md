# Data licensing and disclaimers

The code is MIT. The data is not all MIT, and one source carries a disclaimer
that anyone redistributing this needs to read. Publishing without this file
would pass along obligations the next person does not know they have.

## Shoreline — OpenStreetMap, ODbL 1.0 (share-alike)

`data/lake.geojson` is derived from OSM relation 2851498. That makes it a
Derivative Database under the [Open Database License][odbl], so:

- Attribution required: **© OpenStreetMap contributors**.
- Share-alike: if you publish a modified shoreline, publish it under ODbL too.
- The MIT licence on the code does **not** relicense this. Anything that embeds
  the shoreline — including `dist/index.html`, which inlines it — carries ODbL
  obligations for that portion.

[odbl]: https://opendatacommons.org/licenses/odbl/1-0/

## Imagery — Copernicus Sentinel-2

Free and open under the [Copernicus data policy][cop], accessed via Microsoft
Planetary Computer. Attribution: **Contains modified Copernicus Sentinel data
2019–2026**. The derived rock/shoal candidates are our own work product.

[cop]: https://sentinels.copernicus.eu/web/sentinel/terms-conditions

## Depth soundings — Maine DIFW

`data/soundings.geojson` comes from the state `LakeDpth` layer (Maine Department
of Inland Fisheries and Wildlife), surveyed August 1954, revised January 1979.
Maine public records.

**The source data carries this notice, verbatim, on every point:**

> Data not to be used for navigation purposes.

That disclaimer travels with the derived contours in `data/contours.geojson`,
and it is not softened by interpolation. If anything, interpolating 260 sparse
1954 soundings makes it *more* true, not less.

## Camp locations — Maine E911 NG addresses

The `address` points in `data/structures.geojson` come from the Maine Office of
GIS [Maine E911 Addresses][me911] layer. Maine public records; no licence
condition beyond crediting the source.

These are the locations dispatchers send ambulances to. They are placed off
aerial imagery and are good to a few tens of metres — the map draws them hollow
for that reason — and they say a camp exists without saying what shape it is.

[me911]: https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services/Maine_E911_Addresses_Feature/FeatureServer

## Building footprints — Microsoft, ODbL 1.0 (share-alike)

`data/structures_ms.geojson` is clipped from [Microsoft US Building
Footprints][msbf], published under ODbL 1.0 — the same share-alike terms as the
shoreline above. Attribution: **© Microsoft, ODbL 1.0**. Machine-generated from
Bing imagery: positions are good, outlines are approximate. Kept in the repo as
a cross-check; not currently shipped in `dist/`.

[msbf]: https://github.com/microsoft/USBuildingFootprints

## Terrain and structure detections — USGS 3DEP lidar

`data/terrain.npz` and `data/hag_2m.npz` are 3DEP `ME_Eastern_B1_2017`, US
Government public domain, via Microsoft Planetary Computer. The detections in
`data/structures_lidar.geojson` are our own work product and are **detections,
not a survey** — the file's own metadata says so.

## What this project is, in plain terms

A **navigation aid built from data whose own publisher says not to navigate
with it.** That is not a reason to abandon it — no agency charts inland Maine
lakes, so the alternative is nothing at all — but it is a reason to be blunt in
every place a user can see:

- Candidates are satellite-derived and **unverified**. None has been confirmed
  on the water.
- **Absence of a marker is not evidence of clear water.** The detector finds
  what it finds; it makes no claim about anywhere it stayed quiet.
- Contours are an interpolated surface from 1954 soundings on a **regulated
  lake** whose level moves. They are context, not clearance.
- A rock does not appear in a 10 ft contour.

Anyone forking this for another lake: keep those four statements in the UI. They
are the difference between a useful tool and a liability.

## Attribution block for redistribution

```
Shoreline and structures © OpenStreetMap contributors, ODbL 1.0
Contains modified Copernicus Sentinel-2 data 2019-2026
Depth soundings: Maine Dept of Inland Fisheries & Wildlife (1954, rev. 1979)
  -- "Data not to be used for navigation purposes."
Camp locations: Maine Office of GIS, Maine E911 addresses
Elevation: USGS 3DEP lidar ME_Eastern_B1_2017 (public domain)
Building footprints (repo only, not shipped): © Microsoft, ODbL 1.0
shoalrun code: MIT
```
