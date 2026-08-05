import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";

// Minimal localStorage/navigator stand-ins so the module runs under node.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
// node defines navigator as a getter-only property, so it has to be redefined
// rather than assigned.
let online = true;
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  get: () => ({ onLine: online }),
});

const { isConfigured, joinLake, leaveLake, syncNow, whoAmI } = await import("./sync.js");

beforeEach(() => store.clear());

// Uploading a family's movements without being asked is not a shippable
// default. With no lake joined the module must be completely inert.
test("nothing is sent until a lake is joined", async () => {
  let called = false;
  const spy = async () => { called = true; return { ok: true, json: async () => ({}) }; };
  assert.equal(isConfigured(), false);
  assert.equal(await syncNow({ swept: {}, flags: [] }, spy), null);
  assert.equal(called, false, "must not contact any server");
});

test("a code alone is not enough to start sending", async () => {
  joinLake("MILL24", "");
  let called = false;
  const spy = async () => { called = true; return { ok: true, json: async () => ({}) }; };
  await syncNow({ swept: {}, flags: [] }, spy);
  assert.equal(called, false);
});

test("joining enables sync, leaving disables it again", async () => {
  joinLake("MILL24", "https://example.test");
  assert.equal(isConfigured(), true);
  leaveLake();
  assert.equal(isConfigured(), false);
});

// No signal is the normal case on this lake, not an error.
test("being offline is a quiet no-op", async () => {
  joinLake("MILL24", "https://example.test");
  online = false;
  let called = false;
  const spy = async () => { called = true; return { ok: true, json: async () => ({}) }; };
  assert.equal(await syncNow({ swept: {}, flags: [] }, spy), null);
  assert.equal(called, false, "must not even try");
  online = true;
});

test("a network failure never throws at the caller", async () => {
  joinLake("MILL24", "https://example.test");
  const boom = async () => { throw new Error("dead air"); };
  assert.equal(await syncNow({ swept: {}, flags: [] }, boom), null);
});

test("the device id is stable across calls", () => {
  const a = whoAmI();
  assert.equal(whoAmI(), a);
  assert.match(a, /^d[a-z0-9]+$/);
});

test("the payload carries the code and the device, so reports count as people", async () => {
  joinLake("mill24", "https://example.test");
  let seen = null;
  const spy = async (url, init) => {
    seen = { url, body: JSON.parse(init.body) };
    return { ok: true, json: async () => ({ merged: 0 }) };
  };
  await syncNow({ swept: { cells: [] }, flags: [{ id: "f1" }] }, spy);
  assert.match(seen.url, /\/sync$/);
  assert.equal(seen.body.code, "MILL24", "code is normalised");
  assert.ok(seen.body.who);
  assert.equal(seen.body.flags.length, 1);
});
