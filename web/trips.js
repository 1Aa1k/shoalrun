// Trip summaries off the stored track, for the log in the Info tab.
//
// The fixes are already on the phone -- every outing logs them for the driven
// water layer -- and until now nothing read them back as trips. A list of what
// each outing covered is what gets the app opened after the boat is on the
// trailer, which is when a mark gets reviewed and a report gets filed.

const EARTH_R = 6371000;

// Great-circle distance between two lat/lon points, metres. The stored track
// carries lat/lon rather than projected metres, so this stays self-contained.
export function haversineM(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.sqrt(a));
}

// A leg longer than this between consecutive fixes is a gap (phone asleep, GPS
// lost), not distance driven, and is not summed. 30 s at 35 kn is 540 m; a
// legitimate leg is never anywhere near it at one fix a second.
export const MAX_LEG_MS = 30000;

/**
 * Group stored fixes into trips and total each one.
 *
 * @param {Array<{trip:string,t:number,lat:number,lon:number,spd?:number}>} fixes
 * @param {Array<{t:number,kind?:string}>} [marks]  flags placed; counted into
 *   the trip whose time span contains them
 * @returns {Array<{trip:string,start:number,end:number,meters:number,
 *   maxSpeedMs:number,points:number,flags:number}>} newest first
 */
export function summarizeTrips(fixes, marks = []) {
  const byTrip = new Map();
  for (const f of fixes) {
    if (!f || f.lat == null || f.lon == null || !Number.isFinite(f.t)) continue;
    const key = f.trip || "unknown";
    if (!byTrip.has(key)) byTrip.set(key, []);
    byTrip.get(key).push(f);
  }

  const trips = [];
  for (const [trip, pts] of byTrip) {
    pts.sort((a, b) => a.t - b.t);
    let meters = 0;
    let maxSpeedMs = 0;
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (Number.isFinite(p.spd) && p.spd > maxSpeedMs) maxSpeedMs = p.spd;
      if (i === 0) continue;
      const q = pts[i - 1];
      if (p.t - q.t > MAX_LEG_MS) continue;
      meters += haversineM(q.lat, q.lon, p.lat, p.lon);
    }
    const start = pts[0].t;
    const end = pts[pts.length - 1].t;
    const flags = marks.filter(
      (m) => m && (m.kind === "flag" || m.kind == null) && m.t >= start && m.t <= end
    ).length;
    trips.push({ trip, start, end, meters, maxSpeedMs, points: pts.length, flags });
  }

  trips.sort((a, b) => b.start - a.start);
  return trips;
}

const MS_TO_KN = 1.943844;

/** One trip as the words the log prints. */
export function describeTrip(t, now = Date.now()) {
  const km = (t.meters / 1000).toFixed(1);
  const mins = Math.max(1, Math.round((t.end - t.start) / 60000));
  const dur = mins < 60 ? `${mins} min` : `${Math.floor(mins / 60)} h ${mins % 60} min`;
  const kn = (t.maxSpeedMs * MS_TO_KN).toFixed(0);
  const flags = t.flags ? ` · ${t.flags} report${t.flags === 1 ? "" : "s"}` : "";
  return `${km} km · ${dur} · top ${kn} kn${flags}`;
}
