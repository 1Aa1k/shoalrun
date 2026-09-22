// Tests for the alerting logic. This is the part that has to be right: a miss
// is a holed hull, and a false alarm is a tool the user switches off, which is
// also a holed hull eventually.
//
// Run: node web/hazard.test.mjs

import assert from "node:assert/strict";
import test from "node:test";

import { GridIndex } from "./geo.js";
import { scan, alertLevel, severityOf, CORRIDOR_HALF_W, MIN_SPEED_MS, MIN_CORRIDOR_M } from "./hazard.js";

const EAST = 0; // heading in maths convention: 0 rad = +x = east
const NORTH = Math.PI / 2;

function indexOf(...rocks) {
  const idx = new GridIndex(200);
  for (const r of rocks) idx.insert(r.x, r.y, r);
  return idx;
}

const rock = (id, x, y, cls = "exposed") => ({ id, x, y, cls, area_m2: 500 });

test("rock dead ahead is detected", () => {
  const idx = indexOf(rock("a", 200, 0));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.ok(worst, "expected a hit");
  assert.equal(worst.rock.id, "a");
  assert.ok(Math.abs(worst.range - 200) < 1);
});

test("rock directly behind is ignored", () => {
  const idx = indexOf(rock("behind", -200, 0));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.equal(worst, null, "must not warn about water already passed");
});

test("rock abeam outside the corridor is ignored", () => {
  const idx = indexOf(rock("side", 200, CORRIDOR_HALF_W + 25));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.equal(worst, null);
});

test("rock just inside the corridor is caught", () => {
  const idx = indexOf(rock("edge", 200, CORRIDOR_HALF_W - 5));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.ok(worst);
  assert.equal(worst.rock.id, "edge");
});

test("corridor follows heading, not just distance", () => {
  const idx = indexOf(rock("north", 0, 200));
  // Heading east: the rock to the north must not fire...
  assert.equal(scan({ x: 0, y: 0 }, EAST, 10, idx, new Set()).worst, null);
  // ...but heading north, it must.
  assert.ok(scan({ x: 0, y: 0 }, NORTH, 10, idx, new Set()).worst);
});

test("look-ahead scales with speed", () => {
  const idx = indexOf(rock("far", 500, 0));
  // Slow: 3 m/s * 20 s = 60 m of reach, so a rock at 500 m is not yet relevant.
  assert.equal(scan({ x: 0, y: 0 }, EAST, 3, idx, new Set()).worst, null);
  // Fast: 30 m/s * 20 s = 600 m of reach, so it is.
  assert.ok(scan({ x: 0, y: 0 }, EAST, 30, idx, new Set()).worst);
});

test("stationary boat falls back to a radius, not a corridor", () => {
  // Drifting with a meaningless heading: a nearby rock in any direction should
  // still be reported, because course over ground cannot be trusted here.
  const idx = indexOf(rock("beside", 0, 40));
  const res = scan({ x: 0, y: 0 }, EAST, 0.2, idx, new Set());
  assert.equal(res.moving, false);
  assert.ok(res.worst, "must still report nearby rocks while drifting");
  assert.ok(res.worst.range <= MIN_CORRIDOR_M);
});

test("speed exactly at the moving threshold counts as moving", () => {
  const idx = indexOf(rock("ahead", 100, 0));
  const res = scan({ x: 0, y: 0 }, EAST, MIN_SPEED_MS, idx, new Set());
  assert.equal(res.moving, true);
});

test("dismissed rocks never alert", () => {
  const idx = indexOf(rock("gone", 100, 0));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set(["gone"]));
  assert.equal(worst, null, "a rock the user says is absent must stay silent");
});

test("nearest-in-time is ranked first", () => {
  const idx = indexOf(rock("far", 400, 0, "drawdown"), rock("near", 120, 0, "exposed"));
  const { worst, list } = scan({ x: 0, y: 0 }, EAST, 25, idx, new Set());
  assert.equal(worst.rock.id, "near", "closest in time wins over nastier-but-further");
  assert.equal(list.length, 2);
});

test("null heading does not throw and is treated as not moving", () => {
  const idx = indexOf(rock("a", 30, 0));
  const res = scan({ x: 0, y: 0 }, null, 10, idx, new Set());
  assert.equal(res.moving, false);
  assert.ok(res.worst);
});

test("alertLevel escalates with time-to-contact", () => {
  assert.equal(alertLevel(null), "clear");
  assert.equal(alertLevel({ ttc: 3, range: 300 }), "danger");
  assert.equal(alertLevel({ ttc: 12, range: 300 }), "caution");
  assert.equal(alertLevel({ ttc: 60, range: 900 }), "clear");
});

test("alertLevel escalates on proximity even when slow", () => {
  // Creeping up on a rock at idle speed still has to warn.
  assert.equal(alertLevel({ ttc: Infinity, range: 30 }), "danger");
  assert.equal(alertLevel({ ttc: Infinity, range: 100 }), "caution");
});

test("an unseen shoal outranks a visible rock at equal time-to-contact", () => {
  // Both the same distance dead ahead: the one you cannot see must be reported,
  // because the one you can see is one you are probably already avoiding.
  const idx = indexOf(rock("r", 200, 10, "rock"), rock("s", 200, -10, "shoal"));
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.equal(worst.rock.id, "s");
});

test("severity ranks invisibility, not size", () => {
  assert.ok(severityOf("shoal") > severityOf("rock"));
  assert.ok(severityOf("rock") > severityOf("exposed"));
  assert.equal(severityOf("island"), severityOf("exposed"));
});

test("a close unconfirmed hazard outranks a distant confirmed one", () => {
  // Regression guard. Ranking verified-first would report the far hazard and
  // leave the banner clear while the boat closes on the near one.
  const near = { ...rock("near", 60, 0, "rock"), verdict: "open_water" };
  const far = { ...rock("far", 500, 0, "shoal"), verdict: "rock_confirmed" };
  const { worst } = scan({ x: 0, y: 0 }, EAST, 25, indexOf(near, far), new Set());
  assert.equal(worst.rock.id, "near", "closest in time must always win");
});

test("verification breaks ties between equally urgent hazards", () => {
  const a = { ...rock("unconf", 200, 5, "rock"), verdict: "open_water" };
  const b = { ...rock("conf", 200, -5, "rock"), verdict: "rock_confirmed" };
  const { worst } = scan({ x: 0, y: 0 }, EAST, 10, indexOf(a, b), new Set());
  assert.equal(worst.rock.id, "conf");
});

test("empty index is safe", () => {
  const { worst, list } = scan({ x: 0, y: 0 }, EAST, 10, new GridIndex(200), new Set());
  assert.equal(worst, null);
  assert.equal(list.length, 0);
});

// --- evidence weighting ----------------------------------------------------
// The imagery on this lake cannot tell 10 ft of water from 25 ft (AUC 0.507
// against the soundings), so a "shoal" -- bottom seen through water -- is
// unevidenced unless something else backs it up. 72% of the map is in that
// state. Left at full weight those candidates outrank every confirmed hazard,
// and thousands of false alarms train the user to ignore the app.

test("an unverified shoal does not outrank a confirmed rock at the same range", () => {
  assert.ok(
    severityOf("shoal", "unverified") < severityOf("rock", "confirmed"),
    "unverified shoal must rank below a confirmed rock",
  );
});

test("evidence never reorders hazards ahead of time-to-collision", () => {
  // The safety-critical invariant: whatever the evidence, the thing you are
  // about to hit is reported first. A confirmed hazard 600 m away must never
  // displace an unverified one 20 m dead ahead.
  const idx = indexOf(
    { id: "near", cls: "shoal", tier: "unverified", x: 20, y: 0, area_m2: 500 },
    { id: "far", cls: "island", tier: "confirmed", x: 300, y: 0, area_m2: 500 },
  );
  const { list } = scan({ x: 0, y: 0 }, EAST, 10, idx, new Set());
  assert.equal(list[0].rock.id, "near", "nearest hazard must be reported first");
});

test("same class ranks by evidence", () => {
  assert.ok(severityOf("shoal", "confirmed") > severityOf("shoal", "likely"));
  assert.ok(severityOf("shoal", "likely") > severityOf("shoal", "unverified"));
});

test("a missing tier is treated as unverified, not as trusted", () => {
  assert.equal(severityOf("shoal", undefined), severityOf("shoal", "unverified"));
});

// --- lead presets and clock bearing ---------------------------------------

import { clockBearing, leadSeconds, LEAD_PRESETS } from "./hazard.js";

test("a longer lead projects the corridor further at the same speed", () => {
  const idx = indexOf(rock("far", 500, 0));
  const normal = scan({ x: 0, y: 0 }, EAST, 18, idx, new Set(), LEAD_PRESETS.normal);
  const fast = scan({ x: 0, y: 0 }, EAST, 18, idx, new Set(), LEAD_PRESETS.fast);
  assert.equal(normal.worst, null, "360 m corridor must not reach a rock at 500 m");
  assert.equal(fast.worst.rock.id, "far", "540 m corridor must");
});

test("alert tiers move out with the lead, range floors do not", () => {
  const hit = { ttc: 8, range: 300 };
  assert.equal(alertLevel(hit, LEAD_PRESETS.normal), "caution");
  assert.equal(alertLevel(hit, LEAD_PRESETS.fast), "danger");
  assert.equal(alertLevel({ ttc: 100, range: 35 }, LEAD_PRESETS.slow), "danger");
});

test("the default lead is unchanged from what it always was", () => {
  assert.equal(leadSeconds("normal"), 20);
  assert.equal(leadSeconds("nonsense"), 20);
  assert.equal(alertLevel({ ttc: 6, range: 500 }), "danger");
  assert.equal(alertLevel({ ttc: 15, range: 500 }), "caution");
  assert.equal(alertLevel({ ttc: 16, range: 500 }), "clear");
});

test("clock bearing: dead ahead is 12, starboard beam is 3, port bow is 11", () => {
  const me = { x: 0, y: 0 };
  assert.equal(clockBearing(EAST, me, { x: 100, y: 0 }), "12 o'clock");
  assert.equal(clockBearing(EAST, me, { x: 0, y: -100 }), "3 o'clock");
  assert.equal(clockBearing(EAST, me, { x: 0, y: 100 }), "9 o'clock");
  assert.equal(clockBearing(EAST, me, { x: 100, y: 27 }), "11 o'clock");
  assert.equal(clockBearing(EAST, me, { x: -100, y: 0 }), "6 o'clock");
});

test("clock bearing follows the heading, not the map", () => {
  const me = { x: 0, y: 0 };
  assert.equal(clockBearing(NORTH, me, { x: 0, y: 100 }), "12 o'clock");
  assert.equal(clockBearing(NORTH, me, { x: 100, y: 0 }), "3 o'clock");
  assert.equal(clockBearing(null, me, { x: 100, y: 0 }), null);
});

// Drifting, every ttc is Infinity and Infinity - Infinity is NaN, which sorts
// as "equal" and lets severity decide. A shoal 55 m off must not outrank a
// rock 10 m off when the boat has no course: closest wins, and it is danger.
test("drifting ranks by distance, not severity", () => {
  const idx = indexOf(rock("far", 55, 0, "shoal"), rock("near", 10, 0, "rock"));
  const r = scan({ x: 0, y: 0 }, NaN, 0, idx, new Set());
  assert.equal(r.worst.rock.id, "near");
  assert.equal(alertLevel(r.worst), "danger");
});
