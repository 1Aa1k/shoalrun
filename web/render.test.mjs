import test from "node:test";
import assert from "node:assert/strict";

import { MapView } from "./render.js";
import { drawnAt } from "./evidence.js";

// The rule these tests pin down is the reason the app opens the way it does.
// Aerial imagery on this lake was measured to carry no depth information, so
// 3,549 of the 4,908 candidates persisted across six flights and mean nothing
// beyond that. Drawing them all put a band of magenta around the entire
// shoreline and buried the 48 that were cross-checked at 0.3 m.

test("the default hides candidates with nothing behind them", () => {
  assert.equal(drawnAt({ tier: "unverified" }, "verified"), false);
});

test("the default keeps every tier that has evidence behind it", () => {
  assert.equal(drawnAt({ tier: "confirmed" }, "verified"), true);
  assert.equal(drawnAt({ tier: "likely" }, "verified"), true);
});

test("asking for everything means everything", () => {
  for (const tier of ["confirmed", "likely", "unverified"]) {
    assert.equal(drawnAt({ tier }, "all"), true, tier);
  }
});

// Distance from shore used to be a second, independent filter, and it was the
// wrong axis: the confirmed rocks on this lake sit a median 2 m from shore, so
// tidying the map by distance threw away exactly the marks that hold up.
test("distance from shore no longer decides what is drawn", () => {
  assert.equal(drawnAt({ tier: "confirmed", offshore: false }, "verified"), true);
  assert.equal(drawnAt({ tier: "likely", offshore: false }, "verified"), true);
});

// What is tappable has to be what is visible. Tapping empty water and getting a
// detail sheet for an unverified candidate the map never drew -- then being
// asked to confirm or dismiss it -- is worse than no hit at all.
// Called on a stub rather than a real MapView, which would want a canvas. The
// only thing hitTest needs from `this` is the projection, so a 1:1 one stands
// in and the tap coordinates are the world coordinates.
const hitTest = (sx, sy, rocks, detail) =>
  MapView.prototype.hitTest.call({ toScreen: (x, y) => [x, y] }, sx, sy, rocks, detail);

test("a mark the map is not drawing cannot be tapped", () => {
  const hidden = { id: "h", tier: "unverified", x: 0, y: 0 };
  assert.equal(hitTest(0, 0, [hidden], "verified"), null);
  assert.equal(hitTest(0, 0, [hidden], "all"), hidden);
});

test("a tap lands on the nearest mark that is actually drawn", () => {
  const near = { id: "near", tier: "unverified", x: 2, y: 0 };
  const far = { id: "far", tier: "confirmed", x: 9, y: 0 };
  assert.equal(hitTest(0, 0, [near, far], "all"), near);
  // With the unverified one hidden, the tap reaches past it rather than
  // returning nothing or returning the mark that is not on the screen.
  assert.equal(hitTest(0, 0, [near, far], "verified"), far);
});

test("a tap in open water hits nothing", () => {
  assert.equal(hitTest(0, 0, [{ id: "x", tier: "confirmed", x: 400, y: 400 }], "all"), null);
});

// A tier the build has never emitted must not appear on the default map because
// nobody remembered to add it to a deny-list.
test("an unrecognised tier is not treated as evidence", () => {
  assert.equal(drawnAt({ tier: "probable" }, "verified"), false);
  assert.equal(drawnAt({}, "verified"), false);
  assert.equal(drawnAt({ tier: null }, "verified"), false);
});
