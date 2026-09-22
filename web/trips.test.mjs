import test from "node:test";
import assert from "node:assert/strict";

import { summarizeTrips, describeTrip, haversineM, MAX_LEG_MS } from "./trips.js";

// One degree of latitude is ~111 km everywhere; the check is against that, not
// against a number pulled from the function under test.
test("haversine: one degree of latitude is about 111 km", () => {
  const m = haversineM(45, -68.8, 46, -68.8);
  assert.ok(Math.abs(m - 111195) < 200, `got ${m}`);
});

const fix = (trip, t, lat, lon, spd = 5) => ({ trip, t, lat, lon, spd });

test("fixes are grouped by trip and totalled, newest trip first", () => {
  const fixes = [
    fix("a", 1000, 45.0, -68.8),
    fix("a", 2000, 45.001, -68.8, 7),
    fix("a", 3000, 45.002, -68.8, 3),
    fix("b", 90000, 45.0, -68.8),
    fix("b", 91000, 45.0, -68.801, 12),
  ];
  const trips = summarizeTrips(fixes);
  assert.equal(trips.length, 2);
  assert.equal(trips[0].trip, "b");
  assert.equal(trips[1].trip, "a");
  const a = trips[1];
  assert.ok(Math.abs(a.meters - 222.4) < 2, `got ${a.meters}`);
  assert.equal(a.maxSpeedMs, 7);
  assert.equal(a.points, 3);
  assert.equal(a.start, 1000);
  assert.equal(a.end, 3000);
});

// A phone that slept for ten minutes and woke up across the lake did not drive
// there in one leg. Summing that gap would credit a trip with water it never
// proved, which is the same lie the driven-water layer is built to avoid.
test("a gap longer than MAX_LEG_MS is not counted as distance", () => {
  const fixes = [
    fix("a", 0, 45.0, -68.8),
    fix("a", MAX_LEG_MS + 1, 45.01, -68.8),
    fix("a", MAX_LEG_MS + 1001, 45.011, -68.8),
  ];
  const [t] = summarizeTrips(fixes);
  assert.ok(t.meters < 120 && t.meters > 100, `got ${t.meters}`);
});

test("flags placed during a trip are counted into it", () => {
  const fixes = [fix("a", 1000, 45, -68.8), fix("a", 5000, 45.001, -68.8)];
  const marks = [
    { t: 2000, kind: "flag" },
    { t: 9000, kind: "flag" },
    { t: 3000, kind: "rock", verdict: "confirmed" },
  ];
  assert.equal(summarizeTrips(fixes, marks)[0].flags, 1);
});

test("bad rows are skipped rather than crashing the log", () => {
  const trips = summarizeTrips([null, { trip: "x" }, fix("a", 1, 45, -68.8)]);
  assert.equal(trips.length, 1);
  assert.equal(trips[0].points, 1);
});

test("a trip reads as one plain line", () => {
  const line = describeTrip({ start: 0, end: 95 * 60000, meters: 12340, maxSpeedMs: 10.3, flags: 2 });
  assert.equal(line, "12.3 km · 1 h 35 min · top 20 kn · 2 reports");
  const short = describeTrip({ start: 0, end: 20000, meters: 50, maxSpeedMs: 0, flags: 0 });
  assert.equal(short, "0.1 km · 1 min · top 0 kn");
});
