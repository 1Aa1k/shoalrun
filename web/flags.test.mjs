import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FLAG_STATUS,
  alertsFor,
  cluster,
  flagToHazard,
  makeFlag,
  reviewQueue,
} from "./flags.js";

const fix = (x, y, extra = {}) => ({
  x, y, lat: 45.75 + y / 111000, lon: -68.78 + x / 78000,
  accuracy: 5, speed: 2, ...extra,
});

test("a flag starts pending, never confirmed", () => {
  const f = makeFlag(fix(0, 0));
  assert.equal(f.status, FLAG_STATUS.PENDING);
  assert.equal(f.reviewedBy, null);
});

// The failure this whole project spent a night removing from the satellite
// layer: unverified marks appearing on everyone's map as if they were surveyed.
test("a pending flag alerts only the person who made it", () => {
  const f = makeFlag(fix(0, 0), "guest-a");
  assert.equal(alertsFor(f, "guest-a"), true, "the reporter is standing over it");
  assert.equal(alertsFor(f, "guest-b"), false, "must not propagate before review");
  assert.equal(alertsFor(f, "owner"), false);
});

test("a confirmed flag alerts everyone; a rejected one alerts nobody", () => {
  const c = { ...makeFlag(fix(0, 0), "guest-a"), status: FLAG_STATUS.CONFIRMED };
  const r = { ...makeFlag(fix(0, 0), "guest-a"), status: FLAG_STATUS.REJECTED };
  assert.equal(alertsFor(c, "anyone"), true);
  assert.equal(alertsFor(r, "guest-a"), false, "even the reporter stops seeing it");
});

test("nearby reports are treated as one hazard", () => {
  const g = cluster([makeFlag(fix(0, 0)), makeFlag(fix(20, 0)), makeFlag(fix(400, 0))]);
  assert.equal(g.length, 2, "two spots, not three");
  assert.equal(g[0].flags.length, 2);
});

test("repeated reports sharpen the position rather than anchoring on the first", () => {
  const g = cluster([makeFlag(fix(0, 0)), makeFlag(fix(30, 0))]);
  assert.equal(g[0].x, 15, "centroid of the reports");
});

// A spot three people hit is more urgent than one somebody tapped once at 30 mph.
test("the queue puts independently-reported spots first", () => {
  const flags = [
    makeFlag(fix(500, 0), "guest-a"),
    makeFlag(fix(0, 0), "guest-a"),
    makeFlag(fix(10, 0), "guest-b"),
    makeFlag(fix(15, 0), "guest-c"),
  ];
  const q = reviewQueue(flags);
  assert.equal(q[0].reporters, 3, "three separate people beats one");
  assert.equal(q[0].count, 3);
});

test("a reviewed flag leaves the queue", () => {
  const flags = [
    makeFlag(fix(0, 0), "guest-a"),
    { ...makeFlag(fix(500, 0), "guest-b"), status: FLAG_STATUS.CONFIRMED },
  ];
  const q = reviewQueue(flags);
  assert.equal(q.length, 1, "only pending flags need review");
});

test("a verified flag outranks anything the imagery produced", () => {
  const q = reviewQueue([makeFlag(fix(0, 0), "guest-a")]);
  const h = flagToHazard(q[0], "Nate");
  assert.equal(h.tier, "confirmed");
  assert.equal(h.verdict, "human_confirmed");
  assert.match(h.basis, /verified by Nate/);
});

test("position quality is carried so the reviewer can weigh it", () => {
  const sloppy = makeFlag(fix(0, 0, { accuracy: 25, speed: 15 }));
  assert.equal(sloppy.accuracy, 25);
  assert.equal(sloppy.speed, 15);
  const q = reviewQueue([sloppy, makeFlag(fix(8, 0, { accuracy: 4 }))]);
  assert.equal(q[0].bestAccuracy, 4, "show the best fix available for the spot");
});
