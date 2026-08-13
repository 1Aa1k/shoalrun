// Service worker: makes the app survive with zero signal.
//
// The page itself makes no network requests -- data, code and styles are all
// inlined -- so the only thing that needs caching is the shell. But without a
// service worker, "works offline" depends on browser cache eviction, which is
// exactly the kind of luck you do not want to discover 10 km up a lake with no
// signal. This makes it deterministic.
//
// Cache-first, unconditionally: a stale hazard map that opens beats a fresh one
// that cannot load. Updates land on the next visit with signal.

// Bumped whenever the shell changes shape, so the activate handler drops the
// old cache instead of serving it. This one matters more than a version bump
// usually does: cache-first means a phone that installed the previous build
// would open the three-page version of the app, from cache, indefinitely --
// looking exactly like a working app that had simply not changed.
//
// 3d.html is now a redirect into index.html#3d rather than a page, and it stays
// in the shell so a bookmark still resolves offline.
// Bump on every shipped change while this is being tested on a real phone.
// The worker is cache-first, so without a bump the fix lands one load late --
// and the person checking whether it is fixed reloads once and sees that it is
// not.
//
// v4: the bottom chrome sat behind Safari's address bar.
// v5: holding a drive-pad arrow selected the glyph instead of steering.
// v6: rotating the phone left whichever canvas was hidden sized 0x0.
const CACHE = 'shoalrun-v6';

// index.html is the app. Everything else is a nicety, and the two lists are
// separate because `cache.addAll` is all-or-nothing: one entry that 404s or
// redirects and the whole install rejects, the worker never activates, and the
// app silently loses its offline guarantee -- on the one device where that is
// the entire point.
//
// './' is the entry on GitHub Pages, where the app sits at a directory root. On
// a host that rewrites a clean URL onto the file (sproultech.com/shoalrun) the
// same request answers with a redirect instead, which addAll refuses. So it is
// optional, added on its own, and a failure is ignored.
const REQUIRED = ['./index.html'];
const OPTIONAL = ['./', './3d.html', './manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then(async (c) => {
      await c.addAll(REQUIRED);
      await Promise.all(OPTIONAL.map((u) => c.add(u).catch(() => {})));
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true }).then((hit) => {
      if (hit) {
        // Refresh in the background when there is signal; never block on it.
        fetch(e.request).then((r) => {
          if (r && r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
        }).catch(() => {});
        return hit;
      }
      return fetch(e.request).then((r) => {
        if (r && r.ok) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
        return r;
      }).catch(() => caches.match('./index.html'));
    })
  );
});
