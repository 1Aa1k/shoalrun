import test from "node:test";
import assert from "node:assert/strict";

import { baseDir, widestScope, installPwa } from "./basepath.js";

// This app is served from three shapes of URL and relative links resolve
// against the document, not against anything the app knows about itself. Get
// this wrong and nothing visible breaks -- the lake draws, the GPS works, and
// the offline guarantee is quietly gone until somebody is out of signal.

test("a clean URL names a directory", () => {
  // sproultech.com/shoalrun -- the URL that gets texted to somebody.
  assert.equal(baseDir("/shoalrun"), "/shoalrun/");
});

test("a directory URL is already a directory", () => {
  // 1aa1k.github.io/shoalrun/
  assert.equal(baseDir("/shoalrun/"), "/shoalrun/");
});

test("a file URL drops the file", () => {
  assert.equal(baseDir("/shoalrun/index.html"), "/shoalrun/");
  assert.equal(baseDir("/index.html"), "/");
  assert.equal(baseDir("/deep/nest/3d.htm"), "/deep/nest/");
});

test("a root serves from the root", () => {
  assert.equal(baseDir("/"), "/");
  assert.equal(baseDir(""), "/");
});

test("the widest scope covers the slashless URL, and never widens a root", () => {
  assert.equal(widestScope("/shoalrun/"), "/shoalrun");
  // A worker at the origin root must not be handed something that is not a
  // path at all.
  assert.equal(widestScope("/"), "/");
});

// The registration itself. A stub navigator rather than a browser, because what
// is worth pinning down is the URL it asks for and what it does when the wide
// scope is refused -- both of which are decisions, not browser behaviour.
function stubs(pathname, { registerFails = false, protocol = "https:" } = {}) {
  const calls = [];
  const nav = {
    serviceWorker: {
      register(url, opts) {
        calls.push({ url, scope: opts && opts.scope });
        return registerFails && opts && opts.scope
          ? Promise.reject(new Error("scope not allowed"))
          : Promise.resolve({ scope: (opts && opts.scope) || url });
      },
    },
  };
  const link = { rel: "manifest", href: "./manifest.json" };
  const doc = { querySelector: () => link };
  return { calls, nav, doc, link, loc: { pathname, protocol } };
}

test("the manifest is pointed at the directory the app is served from", async () => {
  const s = stubs("/shoalrun");
  await installPwa(s.loc, s.nav, s.doc);
  assert.equal(s.link.href, "/shoalrun/manifest.json");
});

test("the worker is asked for the scope that covers the clean URL", async () => {
  const s = stubs("/shoalrun");
  await installPwa(s.loc, s.nav, s.doc);
  assert.deepEqual(s.calls, [{ url: "/shoalrun/sw.js", scope: "/shoalrun" }]);
});

test("a refused scope falls back instead of losing the worker entirely", async () => {
  const s = stubs("/shoalrun", { registerFails: true });
  await installPwa(s.loc, s.nav, s.doc);
  assert.deepEqual(s.calls, [
    { url: "/shoalrun/sw.js", scope: "/shoalrun" },
    { url: "/shoalrun/sw.js", scope: undefined },
  ]);
});

// http:// and file:// have no service worker and no geolocation. The page still
// renders there, which is useful for development and is not how this gets used
// on the water.
test("nothing is registered outside a secure context", async () => {
  const s = stubs("/shoalrun", { protocol: "http:" });
  assert.equal(await installPwa(s.loc, s.nav, s.doc), null);
  assert.deepEqual(s.calls, []);
  // The manifest is still corrected -- that part costs nothing and is right
  // either way.
  assert.equal(s.link.href, "/shoalrun/manifest.json");
});
