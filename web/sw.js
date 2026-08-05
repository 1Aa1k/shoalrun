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

const CACHE = 'shoalrun-v1';
const SHELL = ['./', './index.html', './manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
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
