// What a mark is worth, and therefore whether it gets shown.
//
// This lives in its own module because both views need it and they are built
// into separate bundles. When the 2D map kept its own copy of the rule and the
// 3D scene kept another, the two tabs disagreed about how many hazards were in
// the lake -- the header said 1,673 while the map drew 1,359 -- which is the
// kind of thing that makes somebody stop believing either number.
//
// The tiers come from the detection pipeline:
//
//   confirmed   48     above the waterline, cross-checked against 0.3 m NAIP
//   likely      1,311  returns infrared, so a dry surface breaking the water
//   unverified  3,549  persistent across six flights. Meaning unknown.
//
// That last row is 72% of the map and it is a measurement, not a disclaimer:
// aerial imagery on this lake was tested against the 260 MDIFW soundings and
// cannot separate 10 ft of water from 25 ft. There is no bottom signal in the
// photons, so a "shoal" -- bottom seen through water -- is unevidenced here
// unless something other than imagery backs it up.

// An allow-list, not a deny-list. Written the other way round -- "anything that
// is not `unverified`" -- a tier added upstream would start appearing on the
// default map without anyone deciding it should, which is the exact failure
// this filter exists to undo.
const EVIDENCED = new Set(["confirmed", "likely"]);

/**
 * Whether a candidate is shown at a given detail level.
 *
 * DRAWING ONLY. Every candidate stays in the alert index either way; the alarm
 * does not care what is on screen. Hidden from view is not hidden from the
 * alarm, and the Info tab says so.
 *
 * Takes anything carrying a `tier`, which is both the projected rock objects
 * the map holds and the raw GeoJSON properties the 3D scene reads.
 *
 * @param {{tier?: string}} rock
 * @param {"verified"|"all"} detail
 */
export function drawnAt(rock, detail) {
  if (detail === "all") return true;
  return EVIDENCED.has(rock.tier);
}

/** What both views open at. */
export const DEFAULT_DETAIL = "verified";
