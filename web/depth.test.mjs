import test from "node:test";
import assert from "node:assert/strict";
import { sampleDepth } from "./depth.js";

const NODATA = 255;

// The renderer's grid, reduced to what sampleDepth touches.
function grid(rows) {
  const ny = rows.length, nx = rows[0].length;
  return { nx, ny, d: Uint8Array.from(rows.flat()) };
}

test("an exact cell returns that cell", () => {
  const g = grid([[10, 20], [30, 40]]);
  assert.equal(sampleDepth(g, 0, 0), 10);
  assert.equal(sampleDepth(g, 1, 1), 40);
});

test("halfway between two cells is their average", () => {
  const g = grid([[10, 20], [10, 20]]);
  assert.equal(sampleDepth(g, 0.5, 0), 15);
});

test("the centre of four cells is their average", () => {
  const g = grid([[0, 10], [20, 30]]);
  assert.equal(sampleDepth(g, 0.5, 0.5), 15);
});

test("land neighbours are excluded rather than counted as zero depth", () => {
  // Counting NODATA as 0 would drag every shoreline cell shallow and paint a
  // band of false shallows all the way round the lake.
  const g = grid([[20, NODATA], [20, NODATA]]);
  assert.equal(sampleDepth(g, 0.25, 0), 20);
});

test("a point with almost no water under it is not water", () => {
  const g = grid([[20, NODATA], [NODATA, NODATA]]);
  assert.equal(sampleDepth(g, 0.9, 0.9), null);
});

test("all land is null, not zero", () => {
  const g = grid([[NODATA, NODATA], [NODATA, NODATA]]);
  assert.equal(sampleDepth(g, 0.5, 0.5), null);
});

test("sampling past the edge clamps instead of wrapping", () => {
  // Wrapping would fold the west shore onto the east one, which looks like
  // plausible bathymetry and is not.
  const g = grid([[10, 20, 30], [10, 20, 30]]);
  assert.equal(sampleDepth(g, 2.6, 0), 30);
  assert.equal(sampleDepth(g, -0.4, 0), 10);
});

test("interpolation never invents a depth outside its neighbours", () => {
  const g = grid([[5, 40], [12, 33]]);
  for (let i = 0; i <= 20; i++) {
    for (let j = 0; j <= 20; j++) {
      const v = sampleDepth(g, i / 20, j / 20);
      assert.ok(v >= 5 && v <= 40, `${v} outside 5..40`);
    }
  }
});
