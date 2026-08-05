import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CELL_M,
  MAX_ACCURACY_M,
  PLANING_MS,
  SweptGrid,
  coverageStats,
  sweptFromFixes,
} from "./swept.js";

test("a good fix marks its cell", () => {
  const g = new SweptGrid();
  assert.equal(g.addFix(10, 10, 4, 3, 1000), true);
  assert.ok(g.has(10, 10));
  assert.equal(g.size, 1);
});

// A wrong "proven" mark is worse than a missing one: it tells the user water is
// safe on the strength of a fix that could be 30 m off.
test("a poor fix is rejected outright", () => {
  const g = new SweptGrid();
  assert.equal(g.addFix(10, 10, MAX_ACCURACY_M + 0.1, 3, 1000), false);
  assert.equal(g.size, 0);
  assert.equal(g.has(10, 10), false);
});

test("a fix with unknown accuracy is rejected", () => {
  const g = new SweptGrid();
  assert.equal(g.addFix(10, 10, undefined, 3, 1000), false);
  assert.equal(g.addFix(10, 10, NaN, 3, 1000), false);
  assert.equal(g.size, 0);
});

test("a leg fills the cells between two fixes", () => {
  const g = new SweptGrid();
  g.addLeg(0, 0, 100, 0, 5, 10, 1000);
  // 100 m at 5 m cells: every cell along the run, not a dotted line.
  assert.ok(g.size >= 20, `expected the run to be filled, got ${g.size}`);
  for (let x = 2; x < 100; x += CELL_M) assert.ok(g.has(x, 0), `gap at ${x}`);
});

// At 12 m/s with 1 Hz fixes the gaps are wider than a cell, so without leg
// filling most of the water actually driven would read as unknown.
test("fast running does not leave gaps", () => {
  const fixes = [];
  for (let i = 0; i < 10; i++) {
    fixes.push({ x: i * 12, y: 0, accuracy: 5, speed: 12, t: 1000 + i * 1000 });
  }
  const g = sweptFromFixes(fixes);
  for (let x = 2; x < 108; x += CELL_M) assert.ok(g.has(x, 0), `gap at ${x}`);
});

// A logging gap is not a leg. Filling it would invent a straight run the boat
// may never have made -- the phone could have been asleep in a pocket ashore.
test("a long jump between fixes is not filled in", () => {
  const g = new SweptGrid();
  assert.equal(g.addLeg(0, 0, 5000, 0, 5, 10, 1000), 0);
  assert.equal(g.size, 0);
});

test("a time gap breaks the leg rather than bridging it", () => {
  const g = sweptFromFixes([
    { x: 0, y: 0, accuracy: 5, speed: 5, t: 0 },
    { x: 60, y: 0, accuracy: 5, speed: 5, t: 600000 },
  ]);
  assert.ok(g.has(0, 0));
  assert.ok(g.has(60, 0));
  assert.equal(g.has(30, 0), false, "must not claim water between two trips");
});

// On plane the boat draws less, so a fast pass proves less depth. Keeping the
// slow pass preserves the stronger claim.
test("a slow pass outranks a later fast one over the same cell", () => {
  const g = new SweptGrid();
  g.addFix(10, 10, 5, PLANING_MS + 4, 1000);
  assert.equal(g.get(10, 10).planing, true);
  g.addFix(10, 10, 5, 1.0, 2000);
  assert.equal(g.get(10, 10).planing, false, "slow pass must win");
  g.addFix(10, 10, 5, PLANING_MS + 4, 3000);
  assert.equal(g.get(10, 10).planing, false, "fast pass must not overwrite it");
});

test("staleness can be counted against a date", () => {
  const g = new SweptGrid();
  g.addFix(0, 0, 5, 2, 1000);
  g.addFix(50, 50, 5, 2, 9000);
  assert.equal(g.staleCount(5000), 1);
});

test("a grid survives a round trip through JSON", () => {
  const g = new SweptGrid();
  g.addLeg(0, 0, 40, 0, 5, 3, 1000);
  const back = SweptGrid.fromJSON(JSON.parse(JSON.stringify(g)));
  assert.equal(back.size, g.size);
  assert.ok(back.has(20, 0));
});

test("coverage reports driving distance, not just area", () => {
  const g = new SweptGrid();
  g.addLeg(0, 0, 100, 0, 5, 3, 1000);
  const s = coverageStats(g, 34.5e6);
  assert.ok(s.provenM2 > 0);
  assert.ok(s.pctOfLake < 1, "one 100 m run is a rounding error on this lake");
  assert.ok(s.kmToFinish > 1000, "the honest number is thousands of km");
});
