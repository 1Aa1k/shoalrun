import { makeProjection, GridIndex } from "./geo.js";
import { scan, alertLevel, CORRIDOR_HALF_W } from "./hazard.js";
import { MapView } from "./render.js";
import { logFix, allMarks, setMark, clearMark, exportAll, trackCount } from "./store.js";

// DATA is injected at build time so the app is one self-contained file with no
// network dependency of any kind. There is no cell service on this lake.
const { lake: LAKE_GEO, rocks: ROCK_GEO, contours: CONTOUR_GEO, meta: DATA_META } = window.SHOALRUN_DATA;

const el = (id) => document.getElementById(id);
const state = {
  lake: [],
  rocks: [],
  marks: new Map(),
  track: [],
  contours: [],
  fix: null,
  heading: null,
  speed: 0,
  corridor: null,
  alert: "clear",
  trip: `trip-${Date.now()}`,
  logged: 0,
};

// --- geometry setup --------------------------------------------------------

const bounds = LAKE_GEO.bbox;
const proj = makeProjection((bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2);

function ringsOf(geom) {
  if (geom.type === "Polygon") return [geom.coordinates];
  if (geom.type === "MultiPolygon") return geom.coordinates;
  return [];
}

for (const poly of ringsOf(LAKE_GEO.geometry)) {
  state.lake.push(poly.map((ring) => ring.map(([lon, lat]) => proj.fwd(lon, lat))));
}

for (const f of (CONTOUR_GEO ? CONTOUR_GEO.features : [])) {
  state.contours.push({
    depth: f.properties.depth_ft,
    pts: f.geometry.coordinates.map(([lon, lat]) => proj.fwd(lon, lat)),
  });
}

const index = new GridIndex(200);
ROCK_GEO.features.forEach((f, i) => {
  const p = f.properties;
  const [x, y] = proj.fwd(p.lon, p.lat);
  const rock = {
    id: `r${i}`,
    cls: p.class,
    lat: p.lat,
    lon: p.lon,
    area_m2: p.area_m2,
    confidence: p.confidence,
    x,
    y,
  };
  state.rocks.push(rock);
  index.insert(x, y, rock);
});

const view = new MapView(el("map"), proj);
view.center = { x: 0, y: 0 };
view.scale = Math.min(
  view.w / ((bounds[2] - bounds[0]) * proj.mPerDegLon),
  view.h / ((bounds[3] - bounds[1]) * proj.mPerDegLat)
) * 0.92;
view.rotation = 0;
view.follow = false;

// --- marks -----------------------------------------------------------------

async function loadMarks() {
  const marks = await allMarks();
  state.marks = new Map(marks.map((m) => [m.id, m]));
  refreshCounts();
}

function dismissedSet() {
  const s = new Set();
  for (const [id, m] of state.marks) if (m.verdict === "absent") s.add(id);
  return s;
}

// --- GPS -------------------------------------------------------------------

let watchId = null;
let lastFix = null;

function onFix(pos) {
  const c = pos.coords;
  const [x, y] = proj.fwd(c.longitude, c.latitude);
  const t = pos.timestamp;

  // Prefer the device's own course/speed; fall back to differencing consecutive
  // fixes, because iOS Safari reports heading as null far more often than not.
  let speed = c.speed;
  let headingDeg = c.heading;

  if ((speed == null || Number.isNaN(speed)) && lastFix) {
    const dt = (t - lastFix.t) / 1000;
    if (dt > 0.5) speed = Math.hypot(x - lastFix.x, y - lastFix.y) / dt;
  }
  if ((headingDeg == null || Number.isNaN(headingDeg)) && lastFix) {
    const dx = x - lastFix.x;
    const dy = y - lastFix.y;
    if (Math.hypot(dx, dy) > 3) headingDeg = (90 - (Math.atan2(dy, dx) * 180) / Math.PI + 360) % 360;
  }

  speed = speed ?? 0;
  // Device heading is a compass bearing; the maths works in standard maths
  // angles (0 = +x = east, counter-clockwise).
  const headingRad = headingDeg == null ? null : ((90 - headingDeg) * Math.PI) / 180;

  state.fix = { x, y, lat: c.latitude, lon: c.longitude, accuracy: c.accuracy, t };
  state.heading = headingRad;
  state.speed = speed;
  lastFix = { x, y, t };

  state.track.push({ x, y });
  if (state.track.length > 5000) state.track.shift();

  logFix(state.trip, {
    t,
    lat: c.latitude,
    lon: c.longitude,
    speed,
    accuracy: c.accuracy,
  }).then((ok) => {
    if (ok) state.logged++;
  });

  evaluate();

  if (view.follow) {
    view.center = { x, y };
    if (view.courseUp && headingRad != null && speed > 1.5) {
      view.rotation = headingRad - Math.PI / 2;
    }
  }
}

function onGpsError(err) {
  setStatus(`GPS: ${err.message}`, "warn");
}

function startGps() {
  if (!navigator.geolocation) return setStatus("no geolocation on this device", "warn");
  watchId = navigator.geolocation.watchPosition(onFix, onGpsError, {
    enableHighAccuracy: true,
    maximumAge: 1000,
    timeout: 15000,
  });
  setStatus("GPS active", "ok");
}

// --- hazard evaluation -----------------------------------------------------

// Hysteresis: an alert must clear for this long before the banner drops, so a
// hazard passing in and out of the corridor at the edge of tolerance does not
// produce a strobing warning that the user learns to ignore.
const CLEAR_HOLD_MS = 4000;
let lastDangerAt = 0;

function evaluate() {
  if (!state.fix) return;
  const result = scan(state.fix, state.heading, state.speed, index, dismissedSet());

  state.corridor = result.moving
    ? {
        ax: state.fix.x,
        ay: state.fix.y,
        bx: state.fix.x + Math.cos(state.heading) * result.reach,
        by: state.fix.y + Math.sin(state.heading) * result.reach,
        halfW: CORRIDOR_HALF_W,
      }
    : null;

  let level = alertLevel(result.worst);
  const now = Date.now();
  if (level !== "clear") lastDangerAt = now;
  else if (now - lastDangerAt < CLEAR_HOLD_MS) level = state.alert === "clear" ? "clear" : "caution";

  if (level !== state.alert) {
    state.alert = level;
    if (level === "danger") buzz([200, 80, 200]);
    else if (level === "caution") buzz([120]);
  }
  renderAlert(result);
}

function buzz(pattern) {
  if (navigator.vibrate) navigator.vibrate(pattern);
  if (el("sound").checked && state.alert === "danger") beep();
}

let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.frequency.value = 880;
    g.gain.value = 0.25;
    o.connect(g).connect(audioCtx.destination);
    o.start();
    o.stop(audioCtx.currentTime + 0.18);
  } catch (_) {}
}

function renderAlert(result) {
  const banner = el("alert");
  banner.className = `alert ${state.alert}`;
  const w = result.worst;
  if (!w) {
    banner.textContent = result.moving ? "clear ahead" : "clear";
    return;
  }
  const label = w.rock.cls === "shoal" ? "SHOAL" : "ROCK";
  const dist = Math.round(w.range);
  const ttc = Number.isFinite(w.ttc) ? `  ${Math.round(w.ttc)}s` : "";
  banner.textContent =
    state.alert === "clear" ? `nearest ${label.toLowerCase()} ${dist} m` : `${label} ${dist} m${ttc}`;
}

// --- UI --------------------------------------------------------------------

function setStatus(msg, kind = "") {
  const s = el("status");
  s.textContent = msg;
  s.className = kind;
}

function refreshCounts() {
  const confirmed = [...state.marks.values()].filter((m) => m.verdict === "confirmed").length;
  const absent = [...state.marks.values()].filter((m) => m.verdict === "absent").length;
  el("counts").textContent =
    `${state.rocks.length} candidates - ${confirmed} confirmed, ${absent} dismissed`;
}

let selected = null;

el("map").addEventListener("click", (e) => {
  const r = el("map").getBoundingClientRect();
  const hit = view.hitTest(e.clientX - r.left, e.clientY - r.top, state.rocks);
  selected = hit;
  showSheet(hit);
});

function showSheet(rock) {
  const sheet = el("sheet");
  if (!rock) return sheet.classList.remove("open");
  const m = state.marks.get(rock.id);
  el("sheetTitle").textContent = rock.cls === "shoal" ? "Submerged shoal" : "Exposed rock";
  el("sheetBody").innerHTML =
    `<div class="kv"><span>position</span><b>${rock.lat.toFixed(5)}, ${rock.lon.toFixed(5)}</b></div>` +
    `<div class="kv"><span>footprint</span><b>${rock.area_m2} m&sup2;</b></div>` +
    `<div class="kv"><span>detector confidence</span><b>${rock.confidence}</b></div>` +
    `<div class="kv"><span>your verdict</span><b>${m ? m.verdict : "none"}</b></div>`;
  sheet.classList.add("open");
}

el("btnConfirm").onclick = async () => {
  if (!selected) return;
  await setMark(selected.id, "confirmed", "", { lat: selected.lat, lon: selected.lon });
  await loadMarks();
  showSheet(selected);
};
el("btnAbsent").onclick = async () => {
  if (!selected) return;
  await setMark(selected.id, "absent", "", { lat: selected.lat, lon: selected.lon });
  await loadMarks();
  showSheet(selected);
};
el("btnUnmark").onclick = async () => {
  if (!selected) return;
  await clearMark(selected.id);
  await loadMarks();
  showSheet(selected);
};
el("btnClose").onclick = () => el("sheet").classList.remove("open");

el("btnFollow").onclick = () => {
  view.follow = !view.follow;
  el("btnFollow").classList.toggle("on", view.follow);
  if (!view.follow) view.rotation = 0;
};

el("btnExport").onclick = async () => {
  const fc = await exportAll();
  const blob = new Blob([JSON.stringify(fc, null, 2)], { type: "application/geo+json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `shoalrun-${new Date().toISOString().slice(0, 10)}.geojson`;
  a.click();
};

// pan / zoom
let drag = null;
const map = el("map");
map.addEventListener("pointerdown", (e) => {
  drag = { x: e.clientX, y: e.clientY, moved: 0 };
});
map.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x;
  const dy = e.clientY - drag.y;
  drag.moved += Math.hypot(dx, dy);
  const c = Math.cos(-view.rotation);
  const s = Math.sin(-view.rotation);
  view.center.x -= (dx * c - -dy * s) / view.scale;
  view.center.y += (dy * c + dx * s) / view.scale;
  drag.x = e.clientX;
  drag.y = e.clientY;
  view.follow = false;
  el("btnFollow").classList.remove("on");
});
map.addEventListener("pointerup", () => (drag = null));
map.addEventListener("wheel", (e) => {
  e.preventDefault();
  view.scale *= e.deltaY < 0 ? 1.12 : 0.89;
  view.scale = Math.max(0.01, Math.min(4, view.scale));
}, { passive: false });

// --- simulation ------------------------------------------------------------
// Drives a synthetic boat so the alert logic can be exercised without standing
// on the water. Explicitly opt-in via ?sim=1 -- it must never be mistaken for a
// real fix, so it is labelled loudly while running.

// lastStep starts at -Infinity so the first fix lands on the very first frame
// instead of after a full second of blank map.
const sim = { on: false, t: 0, px: 0, py: 0, hdg: 0.6, lastStep: -Infinity };

function startSim() {
  document.body.classList.add("sim");
  setStatus("SIMULATION - not real GPS", "warn");
  // Start in the island-studded eastern arm, which is where the hazards are.
  const start = proj.fwd(-68.79, 45.745);
  sim.px = start[0];
  sim.py = start[1];
  sim.on = true;
  view.follow = true;
  view.scale = 0.35;
  stepSim(performance.now());
}

// Driven from the render loop rather than setInterval. Timers are throttled or
// suspended on hidden/background pages, which silently stalls the simulation --
// exactly what made this look broken under a headless screenshot.
function stepSim(now) {
  if (now - sim.lastStep < 1000) return;
  sim.lastStep = now;
  sim.t += 1;
  sim.hdg += Math.sin(sim.t / 18) * 0.05;
  const spd = 8; // ~16 kn
  sim.px += Math.cos(sim.hdg) * spd;
  sim.py += Math.sin(sim.hdg) * spd;
  const [lon, lat] = proj.inv(sim.px, sim.py);
  onFix({
    coords: {
      latitude: lat,
      longitude: lon,
      accuracy: 5,
      speed: spd,
      heading: (90 - (sim.hdg * 180) / Math.PI + 360) % 360,
    },
    timestamp: Date.now(),
  });
}

// --- boot ------------------------------------------------------------------

// A thrown exception inside a GPS callback would otherwise vanish into the
// console -- on a phone, on a boat, with nobody watching. Surface it where the
// user will actually see it, because a frozen map that still looks alive is the
// most dangerous failure this app can have.
window.addEventListener("error", (e) => {
  setStatus(`ERROR: ${e.message}`, "warn");
});
window.addEventListener("unhandledrejection", (e) => {
  setStatus(`ERROR: ${e.reason && e.reason.message ? e.reason.message : e.reason}`, "warn");
});

function frame(now) {
  if (sim.on) stepSim(now || performance.now());
  view.draw(state);
  requestAnimationFrame(frame);
}

el("meta").textContent = DATA_META.summary;
loadMarks().then(() => {
  trackCount().then((n) => {
    if (n) setStatus(`${n} logged track points on this device`, "ok");
  });
});
frame();

if (new URLSearchParams(location.search).get("sim") === "1") startSim();
else startGps();
