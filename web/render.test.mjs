import test from "node:test";
import assert from "node:assert/strict";

import { drawnAt } from "./render.js";

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

// A tier the build has never emitted must not appear on the default map because
// nobody remembered to add it to a deny-list.
test("an unrecognised tier is not treated as evidence", () => {
  assert.equal(drawnAt({ tier: "probable" }, "verified"), false);
  assert.equal(drawnAt({}, "verified"), false);
  assert.equal(drawnAt({ tier: null }, "verified"), false);
});
