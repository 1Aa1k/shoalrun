import { makeProjection, GridIndex } from "./geo.js";
import { scan, alertLevel, CORRIDOR_HALF_W } from "./hazard.js";
import { MapView } from "./render.js";
import { DepthGrid, contoursAt, contoursAtLevels, rampCss, CHART_BAND_EDGES } from "./depth.js";
import { logFix, allTracks, allMarks, setMark, clearMark, exportAll, trackCount } from "./store.js";
import { SweptGrid, coverageStats, sweptFromFixes } from "./swept.js";

// DATA is injected at build time so the app is one self-contained file with no
// network dependency of any kind. There is no cell service on this lake.
const { lake: LAKE_GEO, rocks: ROCK_GEO, depth: DEPTH_RAW, soundings: SOUNDING_RAW, structures: STRUCT_GEO, meta: DATA_META } = window.SHOALRUN_DATA;

// Contour intervals the slider steps through. Discrete rather than continuous
// because a 7 ft contour interval is not a thing anyone wants -- the useful
// choices are "every couple of feet in the shallows" through "just show me the
// basin", and snapping to them makes the slider land on a sane value every time.
const INTERVALS = [2, 5, 10, 15, 20, 30];

const el = (id) => document.getElementById(id);
const state = {
  lake: [],
  rocks: [],
  marks: new Map(),
  track: [],
  // Water proven by having driven it. The one evidence source on this lake that
  // does not depend on seeing through the water.
  swept: new SweptGrid(),
  sweptPrev: null,
  grid: null,
  contours: [],
  soundings: [],
  contourInterval: 10,
  shallowFt: 0,
  theme: "night",
  showDepth: true,
  showContours: true,
  showSoundings: true,
  showShore: false,
  showCamps: true,
  structures: [],
  fix: null,
  heading: null,
  speed: 0,
  corridor: null,
  alert: "clear",
  guest: false,
  showSwept: true,
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

// The depth surface ships as a grid, not as pre-cut lines, so the contour
// interval is a runtime choice. Contours are derived on demand and cached per
// interval inside the grid.
if (DEPTH_RAW) {
  state.grid = new DepthGrid(DEPTH_RAW, proj);
  state.contours = contoursAt(state.grid, state.contourInterval);
}

// The measured 1954 soundings, projected once. The chart theme prints these as
// numbers on the water the way a paper chart does.
for (const [lon, lat, ft] of SOUNDING_RAW || []) {
  const [x, y] = proj.fwd(lon, lat);
  state.soundings.push({ x, y, ft });
}

// Camps, buildings and piers as orientation landmarks. A hazard cloud on a bare
// shoreline is hard to place; people read this lake by its camps.
for (const f of (STRUCT_GEO ? STRUCT_GEO.features : [])) {
  const g = f.geometry;
  if (!g) continue;
  const conv = (ring) => ring.map(([lon, lat]) => proj.fwd(lon, lat));
  let pts = null;
  let type = g.type;
  if (g.type === "Polygon") pts = conv(g.coordinates[0]);
  else if (g.type === "LineString") pts = conv(g.coordinates);
  else if (g.type === "Point") pts = [proj.fwd(g.coordinates[0], g.coordinates[1])];
  if (pts) state.structures.push({ type, pts, kind: f.properties.kind, name: f.properties.name });
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
    verdict: p.verdict || "unchecked",
    tier: p.tier || "unverified",
    basis: p.basis,
    evidence: p.evidence || "sentinel_only",
    shore_m: p.shore_m,
    offshore: p.offshore !== false,
    x,
    y,
  };
  state.rocks.push(rock);
  index.insert(x, y, rock);
});

// Lake area in projected metres, for the coverage readout. Computed from the
// same rings the map draws, so it cannot drift from what is on screen.
const LAKE_AREA_M2 = state.lake.reduce((sum, poly) => {
  const ringArea = (r) => {
    let a = 0;
    for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
      a += (r[j][0] + r[i][0]) * (r[j][1] - r[i][1]);
    }
    return Math.abs(a / 2);
  };
  // First ring is the outside, the rest are islands and come off the total.
  return sum + poly.reduce((acc, r, i) => acc + (i === 0 ? ringArea(r) : -ringArea(r)), 0);
}, 0);

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

  // Grow the proven-water layer live, so the fog lifts as he drives rather
  // than only after a reload.
  if (state.sweptPrev && t - state.sweptPrev.t < 30000) {
    state.swept.addLeg(state.sweptPrev.x, state.sweptPrev.y, x, y, c.accuracy, speed, t);
  } else {
    state.swept.addFix(x, y, c.accuracy, speed, t);
  }
  if (c.accuracy <= 12) state.sweptPrev = { x, y, t };

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
  showDepthUnder(x, y);

  if (view.follow) {
    view.center = { x, y };
    if (view.courseUp && headingRad != null && speed > 1.5) {
      view.rotation = headingRad - Math.PI / 2;
    }
  }
}

// Interpolated depth beneath the boat. Shown to one foot because that is the
// quantum the grid is stored at -- adding a decimal would imply the 1954
// transects support a precision they do not.
function showDepthUnder(x, y) {
  const card = el("depthNow");
  if (!state.grid) return;
  const ft = state.grid.sampleXY(x, y);
  card.style.display = "block";
  el("depthVal").textContent = ft == null ? "--" : String(ft);
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
  const shown = state.showShore ? state.rocks.length : state.rocks.filter((r) => r.offshore).length;
  el("counts").textContent =
    `${shown} shown of ${state.rocks.length} - ${confirmed} confirmed, ${absent} dismissed` +
    (state.showShore ? "" : " (offshore >50 m; all still alarm)");
}

let selected = null;

el("map").addEventListener("click", (e) => {
  el("panel").classList.remove("open");
  el("btnLayers").classList.remove("on");
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
  const depth = state.grid ? state.grid.sampleXY(rock.x, rock.y) : null;
  el("sheetBody").innerHTML =
    `<div class="kv"><span>position</span><b>${rock.lat.toFixed(5)}, ${rock.lon.toFixed(5)}</b></div>` +
    `<div class="kv"><span>footprint</span><b>${rock.area_m2} m&sup2;</b></div>` +
    // Surrounding depth, not the rock's own depth -- the interpolated surface
    // is 25 m cells off 1954 transects and knows nothing about this rock.
    `<div class="kv"><span>surrounding depth</span><b>${
      depth == null ? "outside survey" : `~${depth} ft (1954)`
    }</b></div>` +
    `<div class="kv"><span>detector confidence</span><b>${rock.confidence}</b></div>` +
    `<div class="kv"><span>0.3 m aerial check</span><b>${
      { rock_confirmed: "rock confirmed", shoal_confirmed: "shoal confirmed",
        open_water: "NOT confirmed", unchecked: "not checked",
        human_mapped: "mapped by a person", naip_multiyear: "NAIP multi-year" }[rock.verdict] || rock.verdict
    }</b></div>` +
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

// --- depth controls --------------------------------------------------------

// Recontouring the whole grid is ~100 ms, which is fine on release but awful if
// it runs on every pixel of slider travel. The label updates immediately so the
// slider still feels live; the lines catch up when the finger settles.
let contourTimer = null;

// Chart mode adds the depth-tint band edges to whatever the slider asked for.
// On a paper chart the tint boundary IS a contour, and drawing it also hides
// the soft edge the upscaled raster shows where one band meets the next.
function contourSet(ft) {
  if (!state.grid) return [];
  if (state.theme !== "chart") return contoursAt(state.grid, ft);

  const levels = new Set(CHART_BAND_EDGES);
  for (let d = ft; d <= state.grid.maxFt; d += ft) levels.add(d);
  const sorted = [...levels].sort((a, b) => a - b);
  const bandEdge = new Set(CHART_BAND_EDGES);
  return contoursAtLevels(state.grid, sorted).map((c) =>
    bandEdge.has(c.depth) ? { ...c, major: true } : c
  );
}

function setInterval_(ft) {
  state.contourInterval = ft;
  el("valInterval").textContent = `${ft} ft`;
  clearTimeout(contourTimer);
  contourTimer = setTimeout(() => {
    state.contours = contourSet(ft);
  }, 90);
}

// Two looks for two conditions: dark for dusk and night, NOAA chart for direct
// sun. Chart mode is not a skin -- the discrete shoal tints, printed soundings
// and magenta danger symbols are the conventions anyone who reads a chart
// already knows, and they survive glare that a dark screen does not.
function setTheme(name) {
  state.theme = name;
  view.theme = name;
  const chart = name === "chart";
  document.body.classList.toggle("chart", chart);
  el("btnTheme").textContent = chart ? "Chart" : "Night";
  el("btnSoundings").style.display = chart ? "" : "none";
  state.contours = contourSet(state.contourInterval);
  if (state.grid) el("legendRamp").style.background = rampCss(state.grid.maxFt, name);
}

el("btnTheme").onclick = () => setTheme(state.theme === "chart" ? "night" : "chart");

el("btnSoundings").onclick = () => {
  state.showSoundings = !state.showSoundings;
  el("btnSoundings").classList.toggle("on", state.showSoundings);
};

el("sldInterval").addEventListener("input", (e) => {
  setInterval_(INTERVALS[+e.target.value]);
});

el("sldShallow").addEventListener("input", (e) => {
  state.shallowFt = +e.target.value;
  el("valShallow").textContent = state.shallowFt === 0 ? "off" : `${state.shallowFt} ft`;
});

el("btnDepth").onclick = () => {
  state.showDepth = !state.showDepth;
  el("btnDepth").classList.toggle("on", state.showDepth);
  el("sldShallow").disabled = !state.showDepth;
};

el("btnLines").onclick = () => {
  state.showContours = !state.showContours;
  el("btnLines").classList.toggle("on", state.showContours);
  el("sldInterval").disabled = !state.showContours;
};

el("btnLayers").onclick = () => {
  const open = el("panel").classList.toggle("open");
  el("btnLayers").classList.toggle("on", open);
  el("sheet").classList.remove("open");
};
el("btnPanelClose").onclick = () => {
  el("panel").classList.remove("open");
  el("btnLayers").classList.remove("on");
};

// Shoreline toggle affects DRAWING ONLY. Every hazard stays in the alert index
// regardless -- the known rocks sit a median 2 m from shore, so suppressing them
// from the alarm to tidy the map would remove most of the real hazards on the
// lake. Hidden from view is not the same as hidden from the alarm.
el("btnCamps").onclick = () => {
  state.showCamps = !state.showCamps;
  el("btnCamps").classList.toggle("on", state.showCamps);
};

// Label stays fixed and the lit state carries the meaning. The old button
// swapped its own label on press, so what it said and what it did were never
// the same thing at the same time.
el("btnSwept").onclick = () => {
  state.showSwept = state.showSwept === false;
  el("btnSwept").classList.toggle("on", state.showSwept !== false);
  view.draw(state);
};

// Guest mode. The case that actually motivated all of this: somebody else at
// the helm who does not know the lake.
//
// A guest cannot use 4,908 markers. 3,549 of them are unverified -- the imagery
// cannot see depth on this lake -- and a stranger has no way to weigh that, so
// the honest markers drown in the doubtful ones and every alert looks the same.
// What a guest can use is "stay on the water we have driven", which is the one
// claim in the whole tool backed by direct evidence.
//
// So this hides the unverified layer, keeps the hazards that hold up, and
// leans on the driven-water layer. It shows LESS, on purpose.
el("btnGuest").onclick = () => {
  state.guest = !state.guest;
  el("btnGuest").classList.toggle("on", state.guest);
  if (state.guest) {
    state.showSwept = true;
    el("btnSwept").classList.add("on");
    setStatus(
      "Guest mode: unverified marks hidden. Stay on the green water - that is " +
        "water this boat has actually driven. White water is unknown, not " +
        "necessarily bad.",
      "ok",
    );
  } else {
    setStatus("All candidates shown, including unverified ones.", "warn");
  }
  view.draw(state);
};

// Share coverage between boats. Deliberately a FILE, not a server: it works
// with no signal at the landing, over AirDrop, a text, or a USB cable, and
// there is no account to make or service to keep running.
el("btnShare").onclick = async () => {
  const blob = new Blob(
    [JSON.stringify({ kind: "shoalrun-swept", v: 1, swept: state.swept })],
    { type: "application/json" },
  );
  const name = `shoalrun-driven-${new Date().toISOString().slice(0, 10)}.json`;
  const file = new File([blob], name, { type: "application/json" });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file], title: "Driven water" });
      return;
    } catch (e) {
      if (e.name === "AbortError") return;
    }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
};

el("fileImport").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const o = JSON.parse(await f.text());
    if (o.kind !== "shoalrun-swept") throw new Error("not a driven-water file");
    const gained = state.swept.merge(SweptGrid.fromJSON(o.swept));
    const st = coverageStats(state.swept, LAKE_AREA_M2);
    setStatus(
      `Added ${gained} new cells. Now ${(st.provenM2 / 1e6).toFixed(2)} km2 driven ` +
        `water on this device.`,
      "ok",
    );
    view.draw(state);
  } catch (err) {
    setStatus(`Could not read that file: ${err.message}`, "warn");
  }
  e.target.value = "";
};

el("btnShore").onclick = () => {
  state.showShore = !state.showShore;
  el("btnShore").classList.toggle("on", state.showShore);
  refreshCounts();
};

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
// Pointers are tracked in a map rather than as a single drag, because the
// target device is a phone: wheel zoom does not exist there, and without pinch
// the map is stuck at whatever zoom it loaded at.
let drag = null;
const pointers = new Map();
let pinchDist = 0;
const map = el("map");

function zoomBy(factor) {
  view.scale = Math.max(0.01, Math.min(4, view.scale * factor));
}

map.addEventListener("pointerdown", (e) => {
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 1) drag = { x: e.clientX, y: e.clientY, moved: 0 };
  else if (pointers.size === 2) {
    drag = null;
    const [a, b] = [...pointers.values()];
    pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
  }
});

map.addEventListener("pointermove", (e) => {
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

  if (pointers.size >= 2) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinchDist > 0 && d > 0) zoomBy(d / pinchDist);
    pinchDist = d;
    return;
  }

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

function endPointer(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinchDist = 0;
  if (pointers.size === 0) drag = null;
}
map.addEventListener("pointerup", endPointer);
map.addEventListener("pointercancel", endPointer);

map.addEventListener("wheel", (e) => {
  e.preventDefault();
  zoomBy(e.deltaY < 0 ? 1.12 : 0.89);
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
el("valInterval").textContent = `${state.contourInterval} ft`;
if (state.grid) el("rampMax").textContent = `${state.grid.maxFt} ft`;
// Chart is the default: this gets used outdoors, in daylight, most of the time.
setTheme(new URLSearchParams(location.search).get("theme") === "night" ? "night" : "chart");
loadMarks().then(() => {
  // Rebuild the proven-water layer from every past trip. Until now these fixes
  // were logged and never read back, so each outing started from a blank lake.
  allTracks().then((fixes) => {
    const pts = fixes
      .filter((f) => f.lat != null && f.lon != null)
      .sort((a, b) => a.t - b.t)
      .map((f) => {
        const [x, y] = proj.fwd(f.lon, f.lat);
        return { x, y, accuracy: f.accuracy, speed: f.speed, t: f.t };
      });
    if (!pts.length) return;
    state.swept = sweptFromFixes(pts);
    const st = coverageStats(state.swept, LAKE_AREA_M2);
    setStatus(
      `${st.kmDriven.toFixed(1)} km driven, ${(st.provenM2 / 1e6).toFixed(2)} km2 ` +
        `proven (${st.pctOfLake.toFixed(1)}% of the lake)`,
      "ok",
    );
  });

  trackCount().then((n) => {
    if (n) setStatus(`${n} logged track points on this device`, "ok");
  });
});
frame();

if (new URLSearchParams(location.search).get("sim") === "1") startSim();
else startGps();
