import { makeProjection, GridIndex } from "./geo.js";
import { scan, alertLevel, CORRIDOR_HALF_W } from "./hazard.js";
import { MapView } from "./render.js";
import { DepthGrid, contoursAt, contoursAtLevels, rampCss, CHART_BAND_EDGES } from "./depth.js";
import { logFix, allTracks, allMarks, setMark, clearMark, exportAll, trackCount } from "./store.js";
import { SweptGrid, coverageStats, sweptFromFixes } from "./swept.js";
import { FLAG_STATUS, alertsFor, flagToHazard, makeFlag, reviewQueue } from "./flags.js";
import { autoSync, isConfigured, whoAmI, joinLake, leaveLake, lakeCode, endpoint } from "./sync.js";
import { initViews, isActive, showView } from "./views.js";
import { installPwa } from "./basepath.js";
import { gpsFailure } from "./gps.js";
import { trackVisibleHeight } from "./viewport.js";

// DATA is injected at build time so the app is one self-contained file with no
// network dependency of any kind. There is no cell service on this lake.
const { lake: LAKE_GEO, rocks: ROCK_GEO, depth: DEPTH_RAW, soundings: SOUNDING_RAW, structures: STRUCT_GEO, meta: DATA_META } = window.SHOALRUN_DATA;

// Contour intervals the slider steps through. Discrete rather than continuous
// because a 7 ft contour interval is not a thing anyone wants -- the useful
// choices are "every couple of feet in the shallows" through "just show me the
// basin", and snapping to them makes the slider land on a sane value every time.
const INTERVALS = [2, 5, 10, 15, 20, 30];

const el = (id) => document.getElementById(id);

// Tier totals, counted off the data actually loaded rather than baked into the
// build, so the number under the button cannot drift from the number of marks
// on the screen.
const TIERS = { confirmed: 0, likely: 0, unverified: 0 };

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
  showReach: false,
  // Water within this many metres of a 1954 sounding is drawn unveiled. A 25 m
  // grid cell either side of a transect is genuinely close to a measurement;
  // beyond that the surface starts being invention.
  reachNearM: 120,
  showSoundings: true,
  // How much of the candidate set gets drawn.
  //
  //   "verified" -- confirmed and likely only. 48 marks cross-checked at 0.3 m
  //                 plus 1,311 that return infrared, so a dry surface breaking
  //                 the water. This is the default.
  //   "all"      -- adds the 3,549 unverified. Imagery on this lake was measured
  //                 to carry no depth information, so these persisted across six
  //                 flights and mean nothing more than that.
  //
  // This used to be two separate toggles -- "guest mode" and "all rocks" -- on
  // opposite ends of the panel, and the app opened with 1,673 marks ringing the
  // whole shoreline in magenta. A map where every mark looks the same is a map
  // that teaches you to ignore all of them, including the 48 that are real.
  //
  // DRAWING ONLY. Every hazard stays in the alert index either way.
  detail: "verified",
  showCamps: true,
  structures: [],
  fix: null,
  heading: null,
  speed: 0,
  corridor: null,
  alert: "clear",
  flags: [],
  showSwept: true,
  sound: true,
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

for (const r of state.rocks) {
  if (r.tier in TIERS) TIERS[r.tier]++;
}

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

// #lat,lon,zoom opens the map somewhere specific. Written for handing someone a
// spot -- "the camps on Evergreen Way" is a URL rather than four sentences of
// pan-and-zoom directions -- and it costs nothing on a lake with no network,
// because the whole app is one file and the hash never leaves the device.
//
// Anything unparseable is ignored rather than raised on: a mistyped link should
// open the lake, not a blank screen.
function applyHash(hash) {
  const m = /^#(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?$/.exec(hash || "");
  if (!m) return false;
  const lat = +m[1];
  const lon = +m[2];
  if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
  const [x, y] = proj.fwd(lon, lat);
  view.center = { x, y };
  view.follow = false;
  if (m[3]) view.scale = Math.max(0.01, Math.min(4, +m[3]));
  return true;
}
applyHash(location.hash);
window.addEventListener("hashchange", () => applyHash(location.hash));

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

// A hazard app that does not know where the boat is is not degraded, it is off,
// and on a phone this is the most likely thing to go wrong on a first open. The
// banner carries it at banner size; the status line carries the fix. See gps.js.
function onGpsError(err) {
  const f = gpsFailure(err);
  if (f.banner) {
    const banner = el("alert");
    banner.className = "alert caution";
    banner.textContent = f.banner;
  }
  setStatus(f.status, f.level);
}

// Keep the screen awake while the GPS is running.
//
// Without this the phone blanks after ~30 s and the app goes on tracking and
// alerting to nobody -- which is the worst possible failure for this tool,
// because it looks like it is working right up until you need it. A hazard
// warning behind a dark screen is not a warning.
//
// The lock is dropped whenever the page is hidden and retaken on return; the
// browser revokes it anyway on backgrounding, and holding it while the app is
// not on screen would drain the battery for nothing.
let wakeLock = null;

async function holdScreenAwake() {
  if (!("wakeLock" in navigator)) return; // iOS < 16.4, older Android
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      wakeLock = null;
    });
  } catch {
    // Denied or unsupported. Not worth a banner -- the user can do nothing
    // about it, and a warning they cannot act on is noise competing with the
    // hazard alerts.
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && watchId != null && !wakeLock) {
      holdScreenAwake();
    }
  });
}

function startGps() {
  if (!navigator.geolocation) return setStatus("no geolocation on this device", "warn");
  watchId = navigator.geolocation.watchPosition(onFix, onGpsError, {
    enableHighAccuracy: true,
    maximumAge: 1000,
    timeout: 15000,
  });
  holdScreenAwake();
  setStatus(
    "wakeLock" in navigator
      ? "GPS active, screen will stay on"
      : "GPS active - set your screen timeout to Never, this phone cannot hold it awake",
    "ok",
  );
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
  if (state.sound && state.alert === "danger") beep();
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

// The status line is hidden until it has something to say. A permanently
// present grey strip costs a line of map to tell you nothing; appearing is what
// makes it worth reading when it does.
function setStatus(msg, kind = "") {
  const s = el("status");
  s.textContent = msg;
  s.className = `status show ${kind}`;
}

function refreshCounts() {
  const confirmed = [...state.marks.values()].filter((m) => m.verdict === "confirmed").length;
  const absent = [...state.marks.values()].filter((m) => m.verdict === "absent").length;
  const verified = TIERS.confirmed + TIERS.likely;
  const yours = confirmed || absent
    ? ` You have confirmed ${confirmed} and dismissed ${absent}.`
    : "";
  el("detailNote").textContent =
    state.detail === "all"
      ? `Showing all ${state.rocks.length}, including ${TIERS.unverified} unverified — ` +
        `persistent in the imagery, meaning unknown.${yours}`
      : `Showing ${verified} marks with evidence behind them. ` +
        `${TIERS.unverified} unverified are hidden — they still set off the alarm.${yours}`;
}

let selected = null;

el("map").addEventListener("click", (e) => {
  if (suppressClick) { suppressClick = false; return; }
  el("panel").classList.remove("open");
  el("btnLayers").classList.remove("on");
  const r = el("map").getBoundingClientRect();
  const hit = view.hitTest(e.clientX - r.left, e.clientY - r.top, state.rocks, state.detail);
  selected = hit;
  showSheet(hit);
});

function showSheet(rock) {
  const sheet = el("sheet");
  if (!rock) return sheet.classList.remove("open");
  const m = state.marks.get(rock.id);
  el("sheetTitle").textContent = rock.cls === "shoal" ? "Submerged shoal" : "Exposed rock";
  const depth = state.grid ? state.grid.sampleXY(rock.x, rock.y) : null;

  // Rows whose value is missing are dropped rather than printed. Most of these
  // candidates carry no detector confidence -- they came from NAIP persistence
  // or were mapped by hand -- and the sheet was rendering the string
  // "undefined" at them, which reads as a broken app rather than as an absent
  // field.
  const rows = [];
  const row = (label, value) => {
    if (value == null || value === "") return;
    rows.push(`<div class="kv"><span>${label}</span><b>${value}</b></div>`);
  };

  // The tier leads, because it is the only thing on this sheet that says how
  // much the mark is worth, and it is what decides whether the map draws it.
  row("evidence", {
    confirmed: "confirmed above the waterline",
    likely: "likely — returns infrared",
    unverified: "unverified — meaning unknown",
  }[rock.tier] || rock.tier);
  row("position", `${rock.lat.toFixed(5)}, ${rock.lon.toFixed(5)}`);
  row("footprint", rock.area_m2 == null ? null : `${rock.area_m2} m&sup2;`);
  // Surrounding depth, not the rock's own depth -- the interpolated surface is
  // 25 m cells off 1954 transects and knows nothing about this rock.
  row("surrounding depth", depth == null ? "outside survey" : `~${depth} ft (1954)`);
  row("detector confidence", rock.confidence);
  row("0.3 m aerial check", {
    rock_confirmed: "rock confirmed",
    shoal_confirmed: "shoal confirmed",
    open_water: "NOT confirmed",
    unchecked: "not checked",
    human_mapped: "mapped by a person",
    naip_multiyear: "seen in several NAIP flights",
  }[rock.verdict] || rock.verdict);
  row("your verdict", m ? m.verdict : "none");

  el("sheetBody").innerHTML = rows.join("");
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

// Every drawer already has a Done button, but it sits at the bottom of a panel
// that is taller than a phone screen -- the way out was below the fold. The
// sticky X delegates to that same button rather than duplicating its logic,
// which for the layers panel also has to unlight the Layers control.
document.querySelectorAll(".drawer .x").forEach((b) => {
  b.onclick = () => el(b.dataset.done).click();
});

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
  el("btnThemeChart").classList.toggle("on", chart);
  el("btnThemeNight").classList.toggle("on", !chart);
  // Printed soundings are a chart convention and the night theme does not draw
  // them, so the control that turns them on and off goes with them.
  el("btnSoundings").style.display = chart ? "" : "none";
  state.contours = contourSet(state.contourInterval);
  if (state.grid) el("legendRamp").style.background = rampCss(state.grid.maxFt, name);
}

el("btnThemeChart").onclick = () => setTheme("chart");
el("btnThemeNight").onclick = () => setTheme("night");

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

// Off by default. It is the honest view, but it fogs 42% of the lake, and a
// map that opens half-obscured reads as broken rather than as candid. It is
// one tap away and the legend says what it means.
el("btnReach").onclick = () => {
  state.showReach = !state.showReach;
  el("btnReach").classList.toggle("on", state.showReach);
  el("legendReach").style.display = state.showReach ? "" : "none";
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

// How much of the candidate set to draw. The case that motivated all of this:
// somebody else at the helm who does not know the lake.
//
// Nobody can use 4,908 markers, and a stranger least of all -- 3,549 of them are
// unverified because the imagery cannot see depth on this lake, and a guest has
// no way to weigh that. The honest markers drown in the doubtful ones and every
// mark ends up looking the same.
//
// So the default shows LESS, on purpose, and leans on the driven-water layer,
// which is the one claim in the whole tool backed by direct evidence.
function setDetail(level) {
  state.detail = level;
  el("btnDetailVerified").classList.toggle("on", level === "verified");
  el("btnDetailAll").classList.toggle("on", level === "all");
  if (level === "verified") {
    state.showSwept = true;
    el("btnSwept").classList.add("on");
  }
  refreshCounts();
  view.draw(state);
}

el("btnDetailVerified").onclick = () => setDetail("verified");
el("btnDetailAll").onclick = () => setDetail("all");

// One tap: "something here". No typing, no category, no menu -- a guest at the
// helm has an interaction budget of exactly one press, and a report that is too
// much work to file is a report that never gets filed.
//
// The flag is inert until somebody who knows the lake reviews it. It alerts the
// person who made it, because they are standing over it, and nobody else. A
// crowd that can put unverified marks on everyone's map is the same failure
// this project just spent a night stripping out of the satellite layer.
el("btnFlag").onclick = async () => {
  if (!state.fix) {
    setStatus("No GPS fix yet - cannot place a report.", "warn");
    return;
  }
  const f = makeFlag({ ...state.fix, speed: state.speed }, whoAmI());
  state.flags.push(f);
  await setMark(f.id, "flagged", "", {
    lat: f.lat, lon: f.lon, kind: "flag", status: f.status,
    reporter: f.reporter, accuracy: f.accuracy, speed: f.speed,
  });
  const btn = el("btnFlag");
  btn.classList.add("done");
  btn.innerHTML = "<span>&#10003;</span> Marked";
  setTimeout(() => {
    btn.classList.remove("done");
    btn.innerHTML = "<span>&#9888;</span> Something here";
  }, 2200);
  setStatus(
    isConfigured()
      ? "Marked. It will go to the others next time you have signal."
      : "Marked. Only you will see it until it is reviewed.",
    "ok",
  );
  refreshPending();
  view.draw(state);
};

function refreshPending() {
  const q = reviewQueue(state.flags);
  const n = el("nPending");
  if (n) n.textContent = String(q.length);
  return q;
}

// The review queue. Reports become hazards only here, and only when someone who
// knows the lake says so -- that is the whole trust model. Spots reported
// independently by several people sort to the top.
el("btnReview").onclick = () => {
  const box = el("reviewList");
  if (box.style.display === "block") {
    box.style.display = "none";
    return;
  }
  const q = refreshPending();
  box.style.display = "block";
  if (!q.length) {
    box.innerHTML = '<div class="foot">No reports waiting.</div>';
    return;
  }
  box.innerHTML = q
    .map((g, i) => {
      const acc = g.bestAccuracy == null ? "unknown" : `${g.bestAccuracy.toFixed(0)} m`;
      const people = g.reporters === 1 ? "1 person" : `${g.reporters} people`;
      return `<div class="rev" data-i="${i}">
        <b>${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}</b>
        <div class="who">${people}, ${g.count} report${g.count === 1 ? "" : "s"} &middot;
          best fix ${acc} &middot; ${new Date(g.newest).toLocaleDateString()}</div>
        <div class="acts">
          <button data-act="go" data-i="${i}">Show me</button>
          <button data-act="yes" data-i="${i}">Real - add it</button>
          <button data-act="no" data-i="${i}" class="ghost">Nothing there</button>
        </div>
      </div>`;
    })
    .join("");

  box.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      const g = q[+b.dataset.i];
      if (b.dataset.act === "go") {
        view.center = { x: g.x, y: g.y };
        view.follow = false;
        el("btnFollow").classList.remove("on");
        // The report is a place on the lake, so showing it means being on the
        // map -- reviewing from the info tab and having nothing visibly happen
        // is the kind of dead button that makes people stop trusting a tool.
        showView("map");
        view.draw(state);
        return;
      }
      const status = b.dataset.act === "yes" ? FLAG_STATUS.CONFIRMED : FLAG_STATUS.REJECTED;
      for (const f of g.flags) {
        f.status = status;
        f.reviewedT = Date.now();
        await setMark(f.id, "flagged", "", {
          lat: f.lat, lon: f.lon, kind: "flag", status,
          reporter: f.reporter, accuracy: f.accuracy, speed: f.speed,
        });
      }
      if (status === FLAG_STATUS.CONFIRMED) {
        const h = flagToHazard(g, "you");
        const [x, y] = proj.fwd(h.lon, h.lat);
        const rock = { id: `flag-${g.flags[0].id}`, ...h, x, y };
        state.rocks.push(rock);
        index.insert(x, y, rock);
        setStatus("Added. Everyone sees it now.", "ok");
      } else {
        setStatus("Cleared.", "ok");
      }
      el("btnReview").click();
      el("btnReview").click();
      view.draw(state);
    };
  });
};

el("btnSound").onclick = () => {
  state.sound = !state.sound;
  const b = el("btnSound");
  b.classList.toggle("on", state.sound);
  b.classList.toggle("off", !state.sound);
  b.querySelector(".ic").innerHTML = state.sound ? "&#128266;" : "&#128263;";
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

// A tap and a pan both end in a click and the browser does not tell them apart,
// so without this every pan drops the detail sheet on whatever the finger
// happened to stop over. Measured as accumulated path, not net displacement --
// a finger that wobbles and returns has still panned the map, and a thumb on a
// wet screen wobbles.
const TAP_SLOP = 12;
let suppressClick = false;

const map = el("map");

function zoomBy(factor) {
  view.scale = Math.max(0.01, Math.min(4, view.scale * factor));
}

map.addEventListener("pointerdown", (e) => {
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 1) {
    drag = { x: e.clientX, y: e.clientY, moved: 0 };
    suppressClick = false;
  } else if (pointers.size === 2) {
    drag = null;
    suppressClick = true;
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
  if (drag.moved > TAP_SLOP) suppressClick = true;
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

// The GPS watch, the alarm and the track log keep running in every view -- the
// boat does not stop moving because somebody opened the info tab. Only the
// drawing pauses, because redrawing a canvas nobody is looking at is pure
// battery on a phone that is already holding a wake lock.
function frame(now) {
  if (sim.on) stepSim(now || performance.now());
  if (isActive("map")) view.draw(state);
  requestAnimationFrame(frame);
}

// --- sharing ---------------------------------------------------------------
// The worker and the join/leave calls have existed since the sync module was
// written; there was no way to reach them from the app, so the handover doc
// told people to do something the UI could not do.

function refreshSync() {
  const box = el("syncState");
  if (isConfigured()) {
    box.className = "syncstate on";
    box.textContent = `Joined ${lakeCode()}. Syncing when this phone has signal.`;
  } else {
    box.className = "syncstate";
    box.textContent = "Not joined. Everything stays on this phone.";
  }
  el("lakeCode").value = lakeCode() || "";
  el("syncUrl").value = endpoint() || "";
}

el("btnJoin").onclick = () => {
  const code = el("lakeCode").value.trim();
  const url = el("syncUrl").value.trim();
  if (!code || !url) {
    setStatus("A lake code and a sync address are both needed to join.", "warn");
    return;
  }
  // Refuse plaintext outright rather than joining and failing quietly later:
  // this uploads boat positions, and a URL somebody mistyped as http is a
  // silent downgrade of exactly the thing they were asked to consent to.
  if (!/^https:\/\//i.test(url)) {
    setStatus("The sync address has to start with https://", "warn");
    return;
  }
  joinLake(code, url);
  refreshSync();
  setStatus(`Joined ${lakeCode()}.`, "ok");
};

el("btnLeave").onclick = () => {
  leaveLake();
  refreshSync();
  setStatus("Left. Nothing leaves this phone.", "ok");
};

// The lake is on screen; the placeholder can go. Left in the markup rather than
// created here so it paints before this 2.6 MB file has finished parsing --
// which on a phone over cellular is the difference between a blank screen and a
// page that is obviously working.
// Safari's address bar sits at the bottom, over the bottom of the page, and
// everything this app puts down there is a control. Ask the browser how tall
// the visible area actually is rather than inferring it from a unit.
trackVisibleHeight();

const boot = el("boot");
if (boot) boot.remove();

el("meta").textContent = DATA_META.summary;
el("tierConfirmed").textContent = TIERS.confirmed.toLocaleString();
el("tierLikely").textContent = TIERS.likely.toLocaleString();
el("tierUnverified").textContent = TIERS.unverified.toLocaleString();
el("valInterval").textContent = `${state.contourInterval} ft`;
if (state.grid) el("rampMax").textContent = `${state.grid.maxFt} ft`;
refreshSync();
refreshCounts();

// The 3D view builds a 156k-triangle mesh and takes a WebGL context, so it is
// stood up the first time somebody opens that tab and never on a device that
// does not. See views.js.
// Manifest and service worker, pointed at wherever this copy is served from.
// See basepath.js -- a clean URL with no trailing slash resolves them to the
// host root and silently loses offline support.
installPwa();

initViews(
  {
    "3d": () => {
      try {
        if (typeof window.__initViewer3d === "function") window.__initViewer3d();
      } catch (err) {
        // A device without WebGL throws out of setup by design, and it has
        // already painted its own explanation over the 3D canvas. What must not
        // happen is that failure taking the map down with it.
        console.warn("3D view unavailable:", err);
      }
    },
  },
  {
    // A hidden canvas measures 0x0, so a rotate that happens while another tab
    // is open leaves this one sized for the old orientation. Re-measure on the
    // way back in. The 3D view does the same for itself, off its draw loop.
    map: () => {
      view.resize();
      view.draw(state);
    },
  },
);
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

  // Restore flags and start automatic sync. With no lake code configured
  // autoSync is inert -- nothing leaves the phone.
  allMarks().then((ms) => {
    state.flags = ms
      .filter((m) => m.kind === "flag")
      .map((m) => ({
        id: m.id, lat: m.lat, lon: m.lon, t: m.t,
        accuracy: m.accuracy, speed: m.speed,
        reporter: m.reporter || "unknown",
        status: m.status || FLAG_STATUS.PENDING,
        ...(() => { const [x, y] = proj.fwd(m.lon, m.lat); return { x, y }; })(),
      }));
    refreshPending();
    autoSync(
      () => ({ swept: state.swept, flags: state.flags }),
      (r) => {
        if (r && r.swept) {
          const gained = state.swept.merge(SweptGrid.fromJSON(r.swept));
          if (gained) setStatus(`Picked up ${gained} cells of driven water from the others.`, "ok");
        }
        if (r && Array.isArray(r.flags)) {
          const known = new Set(state.flags.map((f) => f.id));
          for (const f of r.flags) {
            if (known.has(f.id)) continue;
            const [x, y] = proj.fwd(f.lon, f.lat);
            state.flags.push({ ...f, x, y });
          }
          refreshPending();
        }
        view.draw(state);
      },
    );
  });

  trackCount().then((n) => {
    if (n) setStatus(`${n} logged track points on this device`, "ok");
  });
});
frame();

if (new URLSearchParams(location.search).get("sim") === "1") startSim();
else startGps();

// Last, after the boot overlay is gone and the lake is drawn, so the first ring
// lands on something rather than on a loading bar. It decides for itself
// whether to run: ?tour=1 always, a first visit otherwise, never twice.
startTour();
