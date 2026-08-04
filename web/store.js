// Local persistence. Everything lives on the phone: the lake has no signal, so
// there is no server to sync to mid-trip, and a boating tool that needs a
// network to work is a boating tool that fails exactly when it matters.
//
// Two stores, both feeding the ground-truth loop:
//   tracks      GPS breadcrumbs. Water the boat has actually driven through is
//               proof that water is clear -- passive negative evidence that
//               costs the user nothing to produce.
//   marks       Explicit human verdicts on candidates, plus rocks the user adds
//               from their own knowledge. Positive evidence.

const DB_NAME = "shoalrun";
const DB_VERSION = 1;

let dbp = null;

function open() {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("tracks")) {
        const s = db.createObjectStore("tracks", { keyPath: "t" });
        s.createIndex("trip", "trip");
      }
      if (!db.objectStoreNames.contains("marks")) {
        db.createObjectStore("marks", { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

function tx(store, mode, fn) {
  return open().then(
    (db) =>
      new Promise((resolve, reject) => {
        const t = db.transaction(store, mode);
        const req = fn(t.objectStore(store));
        t.oncomplete = () => resolve(req && req.result);
        t.onerror = () => reject(t.error);
      })
  );
}

// --- track logging ---------------------------------------------------------

// Breadcrumbs are only worth keeping when the boat is actually moving and the
// fix is decent. Logging a drifting boat at the dock all afternoon would bury
// the useful data and imply "safe water" where the boat never really went.
export const TRACK_MIN_SPEED_MS = 1.0;
export const TRACK_MAX_ACC_M = 25;

export async function logFix(trip, fix) {
  if (fix.speed == null || fix.speed < TRACK_MIN_SPEED_MS) return false;
  if (fix.accuracy != null && fix.accuracy > TRACK_MAX_ACC_M) return false;
  await tx("tracks", "readwrite", (s) =>
    s.put({
      t: fix.t,
      trip,
      lat: +fix.lat.toFixed(6),
      lon: +fix.lon.toFixed(6),
      spd: +fix.speed.toFixed(2),
      acc: fix.accuracy == null ? null : Math.round(fix.accuracy),
    })
  );
  return true;
}

export function allTracks() {
  return tx("tracks", "readonly", (s) => s.getAll());
}

export function trackCount() {
  return tx("tracks", "readonly", (s) => s.count());
}

// --- human verdicts --------------------------------------------------------

// `verdict` is one of: confirmed | absent | unsure. `absent` is what suppresses
// a candidate from alerting; it is the user overriding the detector, which is
// the whole point of having a person who lives on the lake in the loop.
export async function setMark(id, verdict, note, extra = {}) {
  await tx("marks", "readwrite", (s) =>
    s.put({ id, verdict, note: note || "", t: Date.now(), ...extra })
  );
}

export function allMarks() {
  return tx("marks", "readonly", (s) => s.getAll());
}

export async function clearMark(id) {
  await tx("marks", "readwrite", (s) => s.delete(id));
}

// --- export ----------------------------------------------------------------

// The export is the handoff back into the offline pipeline: tracks retrain the
// false-positive filter, marks become labels. Plain GeoJSON so it opens in
// anything.
export async function exportAll() {
  const [tracks, marks] = await Promise.all([allTracks(), allMarks()]);

  const byTrip = new Map();
  for (const p of tracks) {
    if (!byTrip.has(p.trip)) byTrip.set(p.trip, []);
    byTrip.get(p.trip).push(p);
  }

  const features = [];
  for (const [trip, pts] of byTrip) {
    pts.sort((a, b) => a.t - b.t);
    if (pts.length < 2) continue;
    features.push({
      type: "Feature",
      properties: {
        kind: "track",
        trip,
        points: pts.length,
        start: new Date(pts[0].t).toISOString(),
        end: new Date(pts[pts.length - 1].t).toISOString(),
      },
      geometry: { type: "LineString", coordinates: pts.map((p) => [p.lon, p.lat]) },
    });
  }

  for (const m of marks) {
    features.push({
      type: "Feature",
      properties: {
        kind: "mark",
        id: m.id,
        verdict: m.verdict,
        note: m.note,
        at: new Date(m.t).toISOString(),
      },
      geometry:
        m.lat != null ? { type: "Point", coordinates: [m.lon, m.lat] } : null,
    });
  }

  return { type: "FeatureCollection", features };
}
