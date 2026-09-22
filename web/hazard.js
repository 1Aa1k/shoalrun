// Hazard scanning: given a GPS fix, decide what is worth interrupting a boater
// for. The hard constraint is that a false alarm costs more than nothing -- a
// tool that cries wolf gets switched off, and then it protects nobody.

import { distToSegment } from "./geo.js";

// How far ahead to look, as seconds of travel at current speed. Below a floor
// speed we stop projecting entirely, because course over ground from GPS is
// meaningless when drifting and would swing the corridor wildly.
export const LOOKAHEAD_S = 20;

// How many seconds of warning the helm gets. Seconds rather than metres because
// the distance already scales with speed; what differs from boat to boat is how
// long it takes to react and come off plane. A pontoon at 5 kn is off the throttle
// and stopped inside 15 s; a bass boat at 40 kn needs the extra ten.
export const LEAD_PRESETS = { slow: 15, normal: LOOKAHEAD_S, fast: 30 };
export function leadSeconds(preset) {
  return LEAD_PRESETS[preset] ?? LOOKAHEAD_S;
}
export const MIN_SPEED_MS = 1.5; // ~3 kn; below this, moored/drifting
export const MIN_CORRIDOR_M = 60; // never look less than this far ahead
export const MAX_CORRIDOR_M = 600; // cap the projection at ~35 kn * 20 s

// Half-width of the danger corridor. Wider than the boat because GPS is good to
// a few metres, the rock centroid is good to ~10 m (one Sentinel pixel), and a
// boat does not turn on a dime.
export const CORRIDOR_HALF_W = 35;

// Alert tiers by class, ranked by how INVISIBLE the hazard is rather than how
// big it is. A shoal is a shallow bottom that never breaks the surface -- it
// looks like open water right up to the moment it stops being open water, so it
// outranks everything. `rock` is a sub-pixel rock that does break the surface;
// often visible, but too small for 10 m imagery to resolve as land. `exposed`
// and `island` are things you can see out the windshield.
const SEVERITY = { shoal: 4, drawdown: 3, rock: 2, exposed: 1, island: 1 };

// How much the evidence behind a candidate is worth. Aerial imagery on this
// lake was measured to carry NO depth information -- it cannot tell 10 ft of
// water from 25 ft (AUC 0.507 against 260 soundings). So a "shoal", which by
// definition means bottom seen through water, is not evidenced at all unless
// something else backs it up, and 72% of the map is in that state.
//
// Those candidates are not dropped: something did persist across six flights.
// But left at full weight they outrank every confirmed hazard on the lake, and
// 3,549 markers crying wolf teaches a boater to ignore the alert -- which is
// worse than shipping no alert. Weighting by evidence keeps them visible and
// stops them from drowning out the hazards we can actually stand behind.
const TIER_WEIGHT = { confirmed: 1.0, likely: 0.75, unverified: 0.4 };

export function severityOf(cls, tier) {
  const base = SEVERITY[cls] ?? 1;
  return base * (TIER_WEIGHT[tier] ?? TIER_WEIGHT.unverified);
}

// A candidate an independent 0.3 m sensor could not see is still worth showing,
// but it must not outrank one that was confirmed. Verified hazards always sort
// ahead of unverified ones at comparable range.
export function isConfirmed(rock) {
  return rock.verdict === "rock_confirmed" || rock.verdict === "shoal_confirmed";
}

/**
 * Scan for hazards in the projected path corridor.
 *
 * @param {{x:number,y:number}} pos     boat position, projected metres
 * @param {number} headingRad           course over ground, radians (0 = east)
 * @param {number} speedMs              speed over ground, m/s
 * @param {GridIndex} index             spatial index of rock candidates
 * @param {Set<string>} dismissed       ids the user marked "not there"
 * @param {number} [lookaheadS]         seconds of travel to project, see LEAD_PRESETS
 * @returns {{list:Array, worst:object|null}}
 */
export function scan(pos, headingRad, speedMs, index, dismissed, lookaheadS = LOOKAHEAD_S) {
  const moving = speedMs >= MIN_SPEED_MS && Number.isFinite(headingRad);

  const reach = moving
    ? Math.min(MAX_CORRIDOR_M, Math.max(MIN_CORRIDOR_M, speedMs * lookaheadS))
    : MIN_CORRIDOR_M;

  // When stopped or drifting we fall back to a plain radius, because there is
  // no trustworthy direction to project along.
  const ax = pos.x;
  const ay = pos.y;
  const bx = moving ? ax + Math.cos(headingRad) * reach : ax;
  const by = moving ? ay + Math.sin(headingRad) * reach : ay;

  const searchR = reach + CORRIDOR_HALF_W;
  const near = index.query(ax, ay, searchR);

  const hits = [];
  for (const rock of near) {
    if (dismissed.has(rock.id)) continue;

    const d = moving
      ? distToSegment(rock.x, rock.y, ax, ay, bx, by)
      : Math.hypot(rock.x - ax, rock.y - ay);

    const limit = moving ? CORRIDOR_HALF_W : MIN_CORRIDOR_M;
    if (d > limit) continue;

    // Straight-line range to the rock, and time to reach it at current speed.
    const range = Math.hypot(rock.x - ax, rock.y - ay);
    const ttc = moving && speedMs > 0 ? range / speedMs : Infinity;

    hits.push({ rock, offTrack: d, range, ttc, severity: severityOf(rock.cls, rock.tier), confirmed: isConfirmed(rock) });
  }

  // Rank by time-to-contact first -- what you will hit soonest is what matters,
  // regardless of how bad it is in the abstract. Severity breaks ties.
  // Time-to-contact dominates, ALWAYS. Sorting confirmed-first would let a
  // verified hazard 600 m away outrank an unverified one 20 m dead ahead, and
  // since the banner reports the top hit that would read "clear" while the boat
  // is about to hit something. Verification is a tiebreaker between hazards of
  // comparable urgency, never a reason to look past a closer one.
  // Drifting, every ttc is Infinity and `a.ttc - b.ttc` is NaN, which is
  // falsy -- so severity decided, and a shoal 55 m off outranked a rock 10 m
  // off and downgraded danger to caution. With no time to rank on, distance
  // is the only thing that matters.
  hits.sort(
    (a, b) =>
      (moving ? a.ttc - b.ttc : a.range - b.range) ||
      b.severity - a.severity ||
      Number(b.confirmed) - Number(a.confirmed) ||
      a.range - b.range
  );

  return { list: hits, worst: hits[0] ?? null, reach, moving };
}

// Alert level from the top hit. Hysteresis lives in the caller; this is a pure
// function of the current scan so it is trivially testable.
//
// The time thresholds are fractions of the lookahead so a longer lead moves
// both tiers out together: at the default 20 s that is danger inside 6 s and
// caution inside 15 s, which is where they always were. The range floors do not
// scale -- 40 m is close whatever the boat.
export function alertLevel(worst, lookaheadS = LOOKAHEAD_S) {
  if (!worst) return "clear";
  if (worst.ttc <= lookaheadS * 0.3 || worst.range <= 40) return "danger";
  if (worst.ttc <= lookaheadS * 0.75 || worst.range <= 120) return "caution";
  return "clear";
}

// Where the hazard is relative to the bow, as a clock position. "1 o'clock" is
// what a helm reads in one glance; a bearing in degrees is arithmetic, and a
// magenta dot on a rotating map is a search. 12 is dead ahead, 3 is off the
// starboard beam. Not meaningful while drifting, so null when there is no
// course to measure from.
export function clockBearing(headingRad, from, to) {
  if (!Number.isFinite(headingRad)) return null;
  const abs = Math.atan2(to.y - from.y, to.x - from.x);
  // Maths angles run counter-clockwise, so a positive offset is to port.
  let rel = abs - headingRad;
  rel = Math.atan2(Math.sin(rel), Math.cos(rel));
  const hour = ((Math.round(-rel / (Math.PI / 6)) % 12) + 12) % 12;
  return `${hour === 0 ? 12 : hour} o'clock`;
}
