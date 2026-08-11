import test from "node:test";
import assert from "node:assert/strict";
import { modelYaw, forwardOf, multiply } from "./mat3d.js";

const close = (a, b, eps = 1e-12) =>
  assert.ok(Math.abs(a - b) < eps, `${a} !== ${b}`);

test("a model yawed to a heading points its bow along that heading", () => {
  // Every quadrant, because the transpose of this matrix agrees at yaw 0 and
  // yaw PI and disagrees everywhere else -- testing only north proves nothing.
  for (const yaw of [0, 0.4, 1.2, Math.PI / 2, 2.6, Math.PI, -0.9, -2.4]) {
    const m = modelYaw(yaw);
    const bow = [-m[8], -m[9], -m[10]];   // image of local -Z
    const want = forwardOf(yaw, 0);
    for (let i = 0; i < 3; i++) close(bow[i], want[i], 1e-12);
  }
});

test("starboard is to the right of the bow", () => {
  for (const yaw of [0, 1.0, -2.0]) {
    const m = modelYaw(yaw);
    const stbd = [m[0], m[1], m[2]];      // image of local +X
    const bow = forwardOf(yaw, 0);
    // right = forward x up for a right-handed frame with up = +Y
    const right = [-bow[2], 0, bow[0]];
    for (let i = 0; i < 3; i++) close(stbd[i], right[i], 1e-12);
  }
});

test("yaw composes with roll and pitch without changing the heading", () => {
  const yaw = 1.1, p = 0.15, r = -0.2;
  const cp = Math.cos(p), sp = Math.sin(p), cr = Math.cos(r), sr = Math.sin(r);
  const roll = [cr, sr, 0, 0, -sr, cr, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  const pitch = [1, 0, 0, 0, 0, cp, sp, 0, 0, -sp, cp, 0, 0, 0, 0, 1];
  const m = multiply(roll, multiply(pitch, modelYaw(yaw)));
  const bow = [-m[8], -m[9], -m[10]];
  const want = forwardOf(yaw, 0);
  // Pitch tilts the bow up or down; its compass bearing must not move.
  close(Math.atan2(bow[0], -bow[2]), Math.atan2(want[0], -want[2]), 1e-12);
  assert.ok(bow[1] > 0, "positive pitch should raise the bow");
});
