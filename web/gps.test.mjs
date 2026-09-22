import test from "node:test";
import assert from "node:assert/strict";

import { gpsFailure, GPS_DENIED, GPS_UNAVAILABLE, GPS_TIMEOUT } from "./gps.js";

// A denied permission is the most likely first-open failure on a phone, and the
// map looks fine without it -- the lake draws, the tabs work, the depth shading
// is all there. Nothing about the screen says the one thing that matters, which
// is that it cannot warn you about anything.
test("a denied permission takes over the banner and says how to fix it", () => {
  const f = gpsFailure({ code: GPS_DENIED, message: "User denied Geolocation" });
  assert.equal(f.banner, "LOCATION OFF");
  assert.match(f.status, /allow it/i);
  assert.equal(f.level, "warn");
});

test("no position available says so at banner size", () => {
  assert.equal(gpsFailure({ code: GPS_UNAVAILABLE }).banner, "NO POSITION");
});

// A cold start under a roof times out routinely. Taking over the hazard banner
// for it would teach somebody to ignore the banner, and the banner is the one
// thing on the screen that must never be ignored.
test("a timeout does not touch the hazard banner", () => {
  const f = gpsFailure({ code: GPS_TIMEOUT });
  assert.equal(f.banner, null);
  assert.match(f.status, /still looking/i);
});

test("an unrecognised failure still says something true", () => {
  assert.equal(gpsFailure({ message: "kaboom" }).banner, null);
  assert.match(gpsFailure({ message: "kaboom" }).status, /kaboom/);
  // Must not throw on a shape nobody predicted -- this runs inside a callback
  // on a boat, where an exception is invisible.
  assert.doesNotThrow(() => gpsFailure(undefined));
  assert.doesNotThrow(() => gpsFailure({}));
  assert.match(gpsFailure(undefined).status, /unknown error/);
});

import { staleBanner, FIX_STALE_MS } from "./gps.js";

// The stream dying silently is the failure that leaves a stale "clear ahead"
// on screen. Fresh fixes must never trip it; a stall must, and must say how long.
test("a fresh fix leaves the banner alone", () => {
  assert.equal(staleBanner(1000, 1000 + FIX_STALE_MS - 1), null);
  assert.equal(staleBanner(null, 99999), null);
});

test("a stalled fix says NO FIX with its age", () => {
  assert.equal(staleBanner(0, 12000), "NO FIX 12s");
  assert.equal(staleBanner(0, 59999), "NO FIX 59s");
  assert.equal(staleBanner(0, 3 * 60000 + 5000), "NO FIX 3m");
});
