// Flags: one-tap "something here" reports, and the review queue that turns them
// into hazards.
//
// The trust model Nate described, made explicit. Guests are not a source of
// rock data -- they are a source of REPORTS. A guest who bumps something, or
// sees water boiling where it should not, taps once. That report is worth
// having and it is not worth trusting, and the app must never confuse the two.
//
// So a flag is inert until somebody who knows the lake reviews it. It alerts
// the person who made it, because they are standing over it right now, but it
// does not become a hazard on anybody else's map until it is confirmed.
//
// One tap is the entire interaction budget. A guest is at the helm, probably at
// speed, possibly rattled from having just hit something. Anything requiring
// typing, a category, or a menu will not get used, and an unused report is
// worth exactly as much as no report.

export const FLAG_STATUS = {
  PENDING: "pending",     // reported, nobody has looked
  CONFIRMED: "confirmed", // reviewed by someone who knows the lake: it is real
  REJECTED: "rejected",   // reviewed: nothing there
};

// How close two reports must be to be treated as the same thing. Generous,
// because the report is made from a moving boat -- the flag lands where the
// boat was, not where the rock is, and at 10 m/s a second of reaction time is
// 10 m of offset on its own.
export const SAME_THING_M = 40;

let seq = 0;

/**
 * Build a flag from the current fix. Nothing else is asked of the user.
 *
 * `speed` and `accuracy` are carried because they say how much the position is
 * worth: a flag dropped at 15 m/s with a 20 m fix is a much vaguer statement
 * about location than one dropped at idle with a 4 m fix, and the reviewer
 * should be able to see that rather than guess.
 */
export function makeFlag(fix, reporter = "guest") {
  return {
    id: `f${Date.now()}-${seq++}`,
    lat: fix.lat,
    lon: fix.lon,
    x: fix.x,
    y: fix.y,
    accuracy: fix.accuracy ?? null,
    speed: fix.speed ?? null,
    t: Date.now(),
    reporter,
    status: FLAG_STATUS.PENDING,
    // Set when reviewed, so the queue can show who decided and when.
    reviewedBy: null,
    reviewedT: null,
  };
}

/**
 * Group flags that are probably the same hazard.
 *
 * Three guests bumping the same rock should read as one rock reported three
 * times, not three rocks. That distinction matters for review -- repeated
 * independent reports at one spot are the strongest signal a crowd can produce,
 * and splitting them apart would throw that away.
 */
export function cluster(flags, radius = SAME_THING_M) {
  const out = [];
  for (const f of flags) {
    let hit = null;
    for (const c of out) {
      if (Math.hypot(c.x - f.x, c.y - f.y) <= radius) {
        hit = c;
        break;
      }
    }
    if (hit) {
      hit.flags.push(f);
      // Centroid, so repeated reports sharpen the position instead of the
      // first one anchoring it.
      hit.x = hit.flags.reduce((s, g) => s + g.x, 0) / hit.flags.length;
      hit.y = hit.flags.reduce((s, g) => s + g.y, 0) / hit.flags.length;
      hit.lat = hit.flags.reduce((s, g) => s + g.lat, 0) / hit.flags.length;
      hit.lon = hit.flags.reduce((s, g) => s + g.lon, 0) / hit.flags.length;
    } else {
      out.push({ x: f.x, y: f.y, lat: f.lat, lon: f.lon, flags: [f] });
    }
  }
  return out;
}

/**
 * Which flags belong in front of a reviewer, most worth looking at first.
 *
 * Ordered by how many independent people reported the same spot, then by how
 * good the positions are. A spot three people hit is more urgent than one
 * somebody tapped once while doing 30.
 */
export function reviewQueue(flags, radius = SAME_THING_M) {
  const pending = flags.filter((f) => f.status === FLAG_STATUS.PENDING);
  const groups = cluster(pending, radius);
  for (const g of groups) {
    g.count = g.flags.length;
    g.reporters = new Set(g.flags.map((f) => f.reporter)).size;
    const accs = g.flags.map((f) => f.accuracy).filter((a) => a != null);
    g.bestAccuracy = accs.length ? Math.min(...accs) : null;
    g.newest = Math.max(...g.flags.map((f) => f.t));
  }
  groups.sort(
    (a, b) =>
      b.reporters - a.reporters ||
      b.count - a.count ||
      (a.bestAccuracy ?? 999) - (b.bestAccuracy ?? 999) ||
      b.newest - a.newest,
  );
  return groups;
}

/**
 * A confirmed flag, as a hazard.
 *
 * Tier is "confirmed" because a person who knows this lake looked at it -- the
 * same standing as a hand-mapped rock, and higher than anything the imagery
 * produces, since the imagery was measured to carry no depth information here.
 */
export function flagToHazard(group, reviewer) {
  return {
    class: "rock",
    lat: group.lat,
    lon: group.lon,
    tier: "confirmed",
    evidence: "reported_and_verified",
    basis: `reported by ${group.reporters} ${
      group.reporters === 1 ? "person" : "people"
    }, verified by ${reviewer}`,
    reports: group.count,
    verdict: "human_confirmed",
    offshore: true,
  };
}

/**
 * Does an unreviewed flag alert anyone?
 *
 * Only the person who made it. They are standing over it and already know. It
 * must not propagate to other boats before review, or the crowd becomes a way
 * to put unverified marks on everybody's map -- which is the failure this whole
 * project just spent a night removing from the satellite layer.
 */
export function alertsFor(flag, viewerId) {
  if (flag.status === FLAG_STATUS.CONFIRMED) return true;
  if (flag.status === FLAG_STATUS.REJECTED) return false;
  return flag.reporter === viewerId;
}
