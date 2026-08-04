// Hazard scanning: given a GPS fix, decide what is worth interrupting a boater
// for. The hard constraint is that a false alarm costs more than nothing -- a
// tool that cries wolf gets switched off, and then it protects nobody.

import { distToSegment } from "./geo.js";

// How far ahead to look, as seconds of travel at current speed. Below a floor
// speed we stop projecting entirely, because course over ground from GPS is
// meaningless when drifting and would swing the corridor wildly.
export const LOOKAHEAD_S = 20;
export const MIN_SPEED_MS = 1.5; // ~3 kn; below this, moored/drifting
export const MIN_CORRIDOR_M = 60; // never look less than this far ahead
export const MAX_CORRIDOR_M = 600; // cap the projection at ~35 kn * 20 s

// Half-width of the danger corridor. Wider than the boat because GPS is good to
// a few metres, the rock centroid is good to ~10 m (one Sentinel pixel), and a
// boat does not turn on a dime.
export const CORRIDOR_HALF_W = 35;

// Alert tiers by class. `drawdown` outranks `exposed` deliberately: an exposed
// ledge is one your friend can see out the windshield, while a drawdown rock is
// invisible at full pond and is the one that takes a lower unit off.
const SEVERITY = { drawdown: 3, shoal: 2, exposed: 1 };

export function severityOf(cls) {
  return SEVERITY[cls] ?? 1;
}

/**
 * Scan for hazards in the projected path corridor.
 *
 * @param {{x:number,y:number}} pos     boat position, projected metres
 * @param {number} headingRad           course over ground, radians (0 = east)
 * @param {number} speedMs              speed over ground, m/s
 * @param {GridIndex} index             spatial index of rock candidates
 * @param {Set<string>} dismissed       ids the user marked "not there"
 * @returns {{list:Array, worst:object|null}}
 */
export function scan(pos, headingRad, speedMs, index, dismissed) {
  const moving = speedMs >= MIN_SPEED_MS && Number.isFinite(headingRad);

  const reach = moving
    ? Math.min(MAX_CORRIDOR_M, Math.max(MIN_CORRIDOR_M, speedMs * LOOKAHEAD_S))
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

    hits.push({ rock, offTrack: d, range, ttc, severity: severityOf(rock.cls) });
  }

  // Rank by time-to-contact first -- what you will hit soonest is what matters,
  // regardless of how bad it is in the abstract. Severity breaks ties.
  hits.sort((a, b) => a.ttc - b.ttc || b.severity - a.severity || a.range - b.range);

  return { list: hits, worst: hits[0] ?? null, reach, moving };
}

// Alert level from the top hit. Hysteresis lives in the caller; this is a pure
// function of the current scan so it is trivially testable.
export function alertLevel(worst) {
  if (!worst) return "clear";
  if (worst.ttc <= 6 || worst.range <= 40) return "danger";
  if (worst.ttc <= 15 || worst.range <= 120) return "caution";
  return "clear";
}
