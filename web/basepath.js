// Where this copy of the app lives, and the PWA wiring that depends on it.
//
// The app is one file that gets served from three shapes of URL, and relative
// links resolve against the document URL, not against anything the app knows
// about itself:
//
//   https://sproultech.com/shoalrun         a clean URL, no trailing slash
//   https://1aa1k.github.io/shoalrun/       a directory
//   http://localhost:9147/index.html        a file at a root
//
// At the first of those, `./sw.js` resolves to /sw.js -- the site root -- and
// 404s, and `./manifest.json` goes with it. Nothing visible breaks: the page
// loads, the lake draws, the GPS works. The only casualty is the offline
// guarantee, which is discovered 10 km up a lake with no signal, by which point
// it is not fixable. So the base is computed rather than assumed.

/**
 * The directory the app is served from, always with a trailing slash.
 *
 * A path is treated as naming a directory unless it ends in .htm(l) -- a clean
 * URL like /shoalrun is a page, but everything beside it lives under
 * /shoalrun/, which is what relative links need.
 *
 * @param {string} pathname
 */
export function baseDir(pathname) {
  const p = pathname || "/";
  if (p.endsWith("/")) return p;
  if (/\.html?$/i.test(p)) return p.replace(/[^/]*$/, "");
  return p + "/";
}

/**
 * The widest scope a worker in `dir` could want.
 *
 * A service worker may only claim its own directory and below, so one at
 * /shoalrun/sw.js does not control /shoalrun -- the exact URL somebody is sent
 * and adds to a home screen. Dropping the trailing slash covers both, and needs
 * the host to allow it; callers fall back when it is refused.
 */
export function widestScope(dir) {
  return dir.length > 1 ? dir.slice(0, -1) : dir;
}

/**
 * Point the manifest link at the right file and register the worker.
 *
 * Registration happens only over HTTPS. From file:// there is no service worker
 * and no geolocation -- the page still renders, but that is not the way to use
 * this on the water.
 */
export function installPwa(loc = location, nav = navigator, doc = document) {
  const dir = baseDir(loc.pathname);

  const link = doc.querySelector("link[rel=manifest]");
  if (link) link.href = dir + "manifest.json";

  if (!("serviceWorker" in nav) || loc.protocol !== "https:") return null;

  return nav.serviceWorker
    .register(dir + "sw.js", { scope: widestScope(dir) })
    .catch(() => nav.serviceWorker.register(dir + "sw.js").catch(() => {}));
}
