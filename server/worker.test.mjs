import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.js";

// Minimal KV stand-in.
function kv() {
  const m = new Map();
  return {
    store: m,
    get: async (k, t) => (m.has(k) ? (t === "json" ? JSON.parse(m.get(k)) : m.get(k)) : null),
    put: async (k, v) => m.set(k, v),
  };
}
const env = () => ({ SHOALRUN: kv() });
const post = (body, e) =>
  worker.fetch(new Request("https://x.test/sync", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }), e);

test("a lake code is required and validated", async () => {
  const e = env();
  assert.equal((await post({ code: "" }, e)).status, 400);
  assert.equal((await post({ code: "no spaces!" }, e)).status, 400);
  assert.equal((await post({ code: "MILL24" }, e)).status, 200);
});

test("coverage from two boats is unioned", async () => {
  const e = env();
  await post({ code: "MILL24", swept: { cell: 5, cells: [["1,1", { t: 1, planing: false }]] } }, e);
  const r = await post({ code: "MILL24", swept: { cell: 5, cells: [["2,2", { t: 1, planing: false }]] } }, e);
  const o = await r.json();
  assert.equal(o.swept.cells.length, 2, "both boats' water");
});

// A slow pass proves more depth. Merging by timestamp alone would let a later
// fast pass silently weaken a claim already earned.
test("a later planing pass does not overwrite a slow one", async () => {
  const e = env();
  await post({ code: "MILL24", swept: { cells: [["1,1", { t: 1, planing: false }]] } }, e);
  const r = await post({ code: "MILL24", swept: { cells: [["1,1", { t: 99, planing: true }]] } }, e);
  const o = await r.json();
  assert.equal(o.swept.cells[0][1].planing, false, "the stronger claim survives");
});

// A stale phone re-uploading its pending copy must not undo a review.
test("a review is not reverted by a stale pending copy", async () => {
  const e = env();
  await post({ code: "MILL24", flags: [{ id: "f1", status: "pending" }] }, e);
  await post({ code: "MILL24", flags: [{ id: "f1", status: "confirmed", reviewedT: 500 }] }, e);
  const r = await post({ code: "MILL24", flags: [{ id: "f1", status: "pending" }] }, e);
  const o = await r.json();
  assert.equal(o.flags[0].status, "confirmed");
});

test("lakes are isolated from each other", async () => {
  const e = env();
  await post({ code: "MILL24", swept: { cells: [["1,1", { t: 1 }]] } }, e);
  const r = await post({ code: "OTHER1", swept: { cells: [] } }, e);
  assert.equal((await r.json()).swept.cells.length, 0);
});

// Rejecting beats truncating: silently dropping half a boat's coverage would
// leave the user believing water was shared when it was not.
test("oversized payloads are rejected, not truncated", async () => {
  const e = env();
  const cells = Array.from({ length: 400_001 }, (_, i) => [`${i},0`, { t: 1 }]);
  const r = await post({ code: "MILL24", swept: { cells } }, e);
  assert.equal(r.status, 413);
  assert.equal(e.SHOALRUN.store.size, 0, "nothing written");
});

test("garbage shapes are rejected rather than crashing", async () => {
  const e = env();
  assert.equal((await post({ code: "MILL24", swept: { cells: "nope" } }, e)).status, 413);
  assert.equal((await post({ code: "MILL24", flags: { not: "an array" } }, e)).status, 413);
});

test("only POST /sync is served", async () => {
  const e = env();
  const r = await worker.fetch(new Request("https://x.test/other", { method: "POST" }), e);
  assert.equal(r.status, 404);
  const g = await worker.fetch(new Request("https://x.test/sync"), e);
  assert.equal(g.status, 405);
});
