// 3D view of the lake bottom. Hand-rolled WebGL, no library: the whole point of
// this project is that it works on a phone with no signal, and a CDN script tag
// is a runtime network dependency wearing a hat.
//
// It renders the SAME interpolated grid the map contours come from, so the mesh
// and the depth lines can never tell different stories. And it is the same 1954
// survey underneath -- a smooth shaded basin looks far more authoritative than
// 260 soundings deserve, which is why the bottom is milled into discrete
// terraces rather than smoothed, and why the caveat is on the screen.
//
// Geometry generation lives in scene3d.js.

import { DepthGrid } from "./depth.js";
import { makeProjection } from "./geo.js";
import {
  FT_PER_M,
  buildSteppedMesh,
  buildFlatCells,
  shoreRings,
  buildShore,
  buildTrees,
  buildHazardRocks,
  buildBoatHull,
} from "./scene3d.js";
import { shaderSources } from "./shaders3d.js";

const { lake: LAKE_GEO, rocks: ROCK_GEO, depth: DEPTH_RAW } = window.SHOALRUN_DATA;

const el = (id) => document.getElementById(id);
const canvas = el("gl");

// Anything thrown during setup used to leave a black canvas and no explanation,
// which is indistinguishable from "the GPU cannot do this" and sent a shader bug
// hunting in entirely the wrong place. Put the real message on the screen -- a
// driver-specific failure is exactly the class of bug that only ever reproduces
// on someone else's machine.
function fatal(msg) {
  const box = el("nogl");
  box.style.display = "grid";
  box.innerHTML =
    `<div><p style="color:#f2b0a6;font-weight:700">3D view failed to start</p>` +
    `<p style="font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:pre-wrap">${
      String(msg).replace(/[<&]/g, (c) => (c === "<" ? "&lt;" : "&amp;"))
    }</p><p><a href="./index.html" style="color:#3d9be9">Back to the map</a></p></div>`;
}
window.addEventListener("error", (e) => fatal(e.message));

const gl = canvas.getContext("webgl", { antialias: true, alpha: false });
if (!gl) {
  fatal("This device has no WebGL.");
  throw new Error("WebGL unavailable");
}

const bounds = LAKE_GEO.bbox;
const proj = makeProjection((bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2);
const grid = new DepthGrid(DEPTH_RAW, proj);

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// New England Outdoor Center, from the OSM structures layer -- a real mapped
// building on this lake, which is where anyone launching actually starts.
const NEOC = { lat: 45.72635, lon: -68.81841 };

const derivExt = gl.getExtension("OES_standard_derivatives");

// Shared precision, chosen once and substituted into BOTH stages, because the
// only way a cross-stage precision mismatch cannot happen is if there is a
// single source of truth for it. A vertex shader defaults to highp; a fragment
// shader declaring mediump silently disagrees, which links on SwiftShader and
// hard-fails on AMD/ANGLE.
const HIGHP_FRAG = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT).precision > 0;
const P = HIGHP_FRAG ? "highp" : "mediump";

function compile(src, type) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    throw new Error(`shader: ${gl.getShaderInfoLog(s)}`);
  }
  return s;
}

function program(vsrc, fsrc) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(vsrc, gl.VERTEX_SHADER));
  gl.attachShader(p, compile(fsrc, gl.FRAGMENT_SHADER));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
    throw new Error(`link: ${gl.getProgramInfoLog(p)}`);
  }
  return p;
}

const S = shaderSources(P);
const bottomProg = program(S.bottom_vert, S.bottom_frag);
const litProg = program(S.lit_vert, S.lit_frag);
const flatProg = program(S.flat_vert, S.flat_frag);
const waterProg = program(S.water_vert, S.water_frag);
const skyProg = program(S.sky_vert, S.sky_frag);

// --- geometry --------------------------------------------------------------

const cx = (grid.worldX(0) + grid.worldX(grid.nx - 1)) / 2;
const cz = (grid.worldY(0) + grid.worldY(grid.ny - 1)) / 2;

function buf(data) {
  const b = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
  return b;
}

const opts = { exag: 8, shallow: 0, terrace: 3 };

// The terraced bottom is real geometry with vertical risers, so changing the
// terrace rebuilds it. Buffers are reused rather than reallocated.
let bottom = buildSteppedMesh(grid, cx, cz, opts.terrace);
const bufBottomPos = buf(bottom.pos);
const bufBottomNorm = buf(bottom.norm);
const bufBottomDep = buf(bottom.dep);

function rebuildBottom() {
  bottom = buildSteppedMesh(grid, cx, cz, opts.terrace);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomPos);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.pos, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomNorm);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.norm, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomDep);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.dep, gl.STATIC_DRAW);
  updateStats();
}

const rings = shoreRings(LAKE_GEO, proj);
const land = buildFlatCells(grid, cx, cz, false);
const water = buildFlatCells(grid, cx, cz, true);
const shore = buildShore(rings, cx, cz);
const trees = buildTrees(grid, rings, cx, cz);
const rocks = buildHazardRocks(grid, ROCK_GEO, proj, cx, cz);
const hull = buildBoatHull();

const bufLand = buf(land);
const bufWater = buf(water);
const bufShore = buf(shore);
const bufTreePos = buf(trees.pos);
const bufTreeNorm = buf(trees.norm);
const bufTreeShade = buf(trees.shade);
const bufRockPos = buf(rocks.pos);
const bufRockNorm = buf(rocks.norm);
const bufRockKind = buf(rocks.kind);
const bufStems = buf(rocks.stems);
const bufHullPos = buf(hull.pos);
const bufHullNorm = buf(hull.norm);
const bufHullShade = buf(hull.shade);
const bufSky = buf(new Float32Array([-1, -1, 3, -1, -1, 3]));

// Land goes through the lit shader like everything else, so it needs matching
// normal and shade attributes. These have to be sized per-vertex or the shader
// reads off the end of the buffer -- three floats per vertex for the normal,
// one for the shade.
const bufLandNorm = buf((() => {
  const n = new Float32Array(land.length);
  for (let i = 1; i < n.length; i += 3) n[i] = 1; // straight up
  return n;
})());
const bufLandShade = buf(new Float32Array(land.length / 3).fill(1));

// --- camera ----------------------------------------------------------------

const MODES = ["orbit", "fly", "boat"];
let mode = "orbit";

const orbit = { yaw: 0, pitch: 0.62, dist: 7000 };
// Fly starts fast. At 400 m/s it took most of a minute to cross a 9 km lake,
// which is not flying, it is drifting.
const fly = { x: 0, y: 1200, z: 5000, yaw: 0, pitch: -0.35, speed: 1400 };
const boat = { x: 0, z: 0, yaw: Math.PI, pitch: -0.06, speed: 0, heading: Math.PI };

const BOAT_EYE_M = 1.9;
const BOAT_MAX_MS = 12; // ~23 kn, about what an outboard on this lake does
const keys = new Set();

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  return [
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) / (near - far), -1,
    0, 0, (2 * far * near) / (near - far), 0,
  ];
}

function multiply(a, b) {
  const o = new Array(16).fill(0);
  for (let i = 0; i < 4; i++) {
    for (let j = 0; j < 4; j++) {
      for (let k = 0; k < 4; k++) o[i * 4 + j] += a[i * 4 + k] * b[k * 4 + j];
    }
  }
  return o;
}

const sub3 = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add3 = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const dot3 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross3 = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
function norm3(v) {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

function lookAt(eye, target, up) {
  const z = norm3(sub3(eye, target));
  const x = norm3(cross3(up, z));
  const y = cross3(z, x);
  return [
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot3(x, eye), -dot3(y, eye), -dot3(z, eye), 1,
  ];
}

// Yaw is measured from -Z (north), so heading and the map's course-up agree.
function forwardOf(yaw, pitch) {
  return [
    Math.sin(yaw) * Math.cos(pitch),
    Math.sin(pitch),
    -Math.cos(yaw) * Math.cos(pitch),
  ];
}

// World Z is negated north, so it has to be undone before sampling.
const depthAtWorld = (wx, wz) => grid.sampleXY(wx + cx, -wz + cz);

// Nearest water to a given point with at least `minFt` under it. Used for the
// spawn: NEOC's mapped position is the building, which is on land.
function waterNear(x0, z0, minFt = 8) {
  let best = null;
  let bestD = Infinity;
  for (let row = 0; row < grid.ny; row += 2) {
    for (let col = 0; col < grid.nx; col += 2) {
      const ft = grid.at(col, row);
      if (ft === null || ft < minFt) continue;
      const x = grid.worldX(col) - cx;
      const z = -(grid.worldY(row) - cz);
      const d = Math.hypot(x - x0, z - z0);
      if (d < bestD) {
        bestD = d;
        best = { x, z };
      }
    }
  }
  return best || { x: 0, z: 0 };
}

const SPAWNS = (() => {
  const [nx, ny] = proj.fwd(NEOC.lon, NEOC.lat);
  const neoc = waterNear(nx - cx, -(ny - cz));
  return {
    neoc: { ...neoc, label: "NEOC" },
    deep: { ...waterNear(0, 0, 45), label: "Deep basin" },
    shoals: { ...pickShoalWater(), label: "Shoal field" },
  };
})();

// Somewhere with hazards actually around you, which is the interesting place to
// be dropped and the one the boat view is for.
function pickShoalWater() {
  let best = null;
  let bestN = -1;
  for (const h of rocks.list) {
    if (!h.submerged) continue;
    let n = 0;
    for (const o of rocks.list) {
      if (Math.abs(o.x - h.x) < 400 && Math.abs(o.z - h.z) < 400) n++;
    }
    if (n > bestN) {
      bestN = n;
      best = h;
    }
  }
  return best ? waterNear(best.x, best.z, 6) : waterNear(0, 0, 6);
}

function spawnAt(key) {
  const s = SPAWNS[key] || SPAWNS.neoc;
  boat.x = s.x;
  boat.z = s.z;
  boat.speed = 0;
  fly.x = s.x;
  fly.z = s.z + 900;
  fly.y = 500;
  fly.pitch = -0.3;
}

function cameraFor(dt) {
  if (mode === "orbit") {
    const eye = [
      Math.sin(orbit.yaw) * Math.cos(orbit.pitch) * orbit.dist,
      Math.sin(orbit.pitch) * orbit.dist,
      Math.cos(orbit.yaw) * Math.cos(orbit.pitch) * orbit.dist,
    ];
    return { eye, target: [0, 0, 0] };
  }

  if (mode === "fly") {
    const fwd = forwardOf(fly.yaw, fly.pitch);
    const right = norm3(cross3(fwd, [0, 1, 0]));
    let move = [0, 0, 0];
    if (keys.has("w")) move = add3(move, fwd);
    if (keys.has("s")) move = sub3(move, fwd);
    if (keys.has("d")) move = add3(move, right);
    if (keys.has("a")) move = sub3(move, right);
    if (keys.has(" ")) move = add3(move, [0, 1, 0]);
    if (keys.has("shift")) move = sub3(move, [0, 1, 0]);
    if (move[0] || move[1] || move[2]) {
      const m = norm3(move);
      const v = fly.speed * dt;
      fly.x += m[0] * v;
      fly.y += m[1] * v;
      fly.z += m[2] * v;
    }
    const eye = [fly.x, fly.y, fly.z];
    return { eye, target: add3(eye, fwd) };
  }

  if (keys.has("a")) boat.heading -= 1.1 * dt;
  if (keys.has("d")) boat.heading += 1.1 * dt;
  const want = keys.has("w") ? BOAT_MAX_MS : keys.has("s") ? -BOAT_MAX_MS * 0.4 : 0;
  // Eased rather than instant, because instant throttle makes the depth readout
  // jump in a way that is impossible to read.
  boat.speed += (want - boat.speed) * Math.min(1, dt * 1.6);
  const h = forwardOf(boat.heading, 0);
  boat.x += h[0] * boat.speed * dt;
  boat.z += h[2] * boat.speed * dt;

  boat.yaw = boat.heading;
  const eye = [boat.x, BOAT_EYE_M, boat.z];
  return { eye, target: add3(eye, forwardOf(boat.yaw, boat.pitch)) };
}

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const r = canvas.getBoundingClientRect();
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
}
window.addEventListener("resize", resize);
resize();

function attr(prog, name, b, size) {
  const loc = gl.getAttribLocation(prog, name);
  if (loc < 0) return;
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
}
const uni = (prog, n) => gl.getUniformLocation(prog, n);

// Hull sits in the camera's own frame, so it is placed by building a model
// matrix from the boat's heading and position rather than by baking it in.
function hullMatrix() {
  const c = Math.cos(boat.heading);
  const s = Math.sin(boat.heading);
  return [
    c, 0, -s, 0,
    0, 1, 0, 0,
    s, 0, c, 0,
    boat.x, BOAT_EYE_M, boat.z, 1,
  ];
}

let lastT = performance.now();
let clock = 0;

function draw(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000) || 0.016;
  lastT = now;
  clock += dt;

  const { eye, target } = cameraFor(dt);
  // Boat mode ignores the exaggeration slider. From 1.9 m above the water, 8x
  // turns a 20 ft bottom into a 160 ft canyon and puts every shallow at eye
  // level -- the one view that pretends you are on the lake is the one where
  // the geometry has to be true.
  const exag = mode === "boat" ? 1 : opts.exag;

  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  gl.disable(gl.DEPTH_TEST);
  gl.depthMask(false);
  gl.useProgram(skyProg);
  attr(skyProg, "aXY", bufSky, 2);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  gl.depthMask(true);
  gl.enable(gl.DEPTH_TEST);

  const view = lookAt(eye, target, [0, 1, 0]);
  const projM = perspective(0.9, canvas.width / canvas.height, 0.4, 80000);
  const mvp = new Float32Array(multiply(view, projM));

  // Land.
  gl.useProgram(litProg);
  gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, mvp);
  gl.uniform1f(uni(litProg, "uExag"), 1);
  gl.uniform3f(uni(litProg, "uColorA"), 0.271, 0.286, 0.216);
  gl.uniform3f(uni(litProg, "uColorB"), 0.271, 0.286, 0.216);
  attr(litProg, "aPos", bufLand, 3);
  attr(litProg, "aNormal", bufLandNorm, 3);
  attr(litProg, "aShade", bufLandShade, 1);
  gl.drawArrays(gl.TRIANGLES, 0, land.length / 3);

  // Bottom.
  gl.useProgram(bottomProg);
  gl.uniformMatrix4fv(uni(bottomProg, "uMVP"), false, mvp);
  gl.uniform1f(uni(bottomProg, "uExag"), exag);
  gl.uniform1f(uni(bottomProg, "uShallow"), opts.shallow);
  attr(bottomProg, "aPos", bufBottomPos, 3);
  attr(bottomProg, "aNormal", bufBottomNorm, 3);
  attr(bottomProg, "aDepth", bufBottomDep, 1);
  gl.drawArrays(gl.TRIANGLES, 0, bottom.count);

  // Rocks, trees, hull -- all through the lit shader.
  gl.useProgram(litProg);
  gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, mvp);

  gl.uniform1f(uni(litProg, "uExag"), exag);
  gl.uniform3f(uni(litProg, "uColorA"), 0.62, 0.24, 0.20);   // breaks surface
  gl.uniform3f(uni(litProg, "uColorB"), 0.85, 0.48, 0.16);   // submerged shoal
  attr(litProg, "aPos", bufRockPos, 3);
  attr(litProg, "aNormal", bufRockNorm, 3);
  attr(litProg, "aShade", bufRockKind, 1);
  gl.drawArrays(gl.TRIANGLES, 0, rocks.count);

  gl.uniform1f(uni(litProg, "uExag"), 1);
  gl.uniform3f(uni(litProg, "uColorA"), 0.106, 0.208, 0.145);
  gl.uniform3f(uni(litProg, "uColorB"), 0.180, 0.310, 0.196);
  attr(litProg, "aPos", bufTreePos, 3);
  attr(litProg, "aNormal", bufTreeNorm, 3);
  attr(litProg, "aShade", bufTreeShade, 1);
  gl.drawArrays(gl.TRIANGLES, 0, trees.count);

  if (mode === "boat") {
    gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, new Float32Array(multiply(hullMatrix(), multiply(view, projM))));
    gl.uniform3f(uni(litProg, "uColorA"), 0.62, 0.64, 0.67);
    gl.uniform3f(uni(litProg, "uColorB"), 0.78, 0.79, 0.81);
    attr(litProg, "aPos", bufHullPos, 3);
    attr(litProg, "aNormal", bufHullNorm, 3);
    attr(litProg, "aShade", bufHullShade, 1);
    gl.drawArrays(gl.TRIANGLES, 0, hull.count);
    gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, mvp);
  }

  // Shoreline and shoal stems.
  gl.useProgram(flatProg);
  gl.uniformMatrix4fv(uni(flatProg, "uMVP"), false, mvp);
  // Shoreline lives at the waterline, so exaggeration must not move it.
  gl.uniform1f(uni(flatProg, "uY"), 0.15);
  gl.uniform1f(uni(flatProg, "uExag"), 1);
  gl.uniform4f(uni(flatProg, "uColor"), 0.42, 0.34, 0.22, 1);
  attr(flatProg, "aPos", bufShore, 3);
  gl.drawArrays(gl.LINES, 0, shore.length / 3);

  if (rocks.stems.length) {
    // Stems run from the rock up to the surface, so they have to be stretched
    // by the same factor as the bottom or they float free of it.
    gl.uniform1f(uni(flatProg, "uY"), 0);
    gl.uniform1f(uni(flatProg, "uExag"), exag);
    gl.uniform4f(uni(flatProg, "uColor"), 1.0, 0.55, 0.18, 0.7);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    attr(flatProg, "aPos", bufStems, 3);
    gl.drawArrays(gl.LINES, 0, rocks.stems.length / 3);
    gl.disable(gl.BLEND);
  }

  // Water last, blended, with depth writes off so the bottom stays visible
  // through it. Opaque water makes boat mode a blue plane, and seeing what is
  // under you is the entire reason to be in boat mode.
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.depthMask(false);
  gl.useProgram(waterProg);
  gl.uniformMatrix4fv(uni(waterProg, "uMVP"), false, mvp);
  gl.uniform1f(uni(waterProg, "uTime"), clock);
  gl.uniform1f(uni(waterProg, "uAlpha"), mode === "boat" ? 0.62 : 0.2);
  attr(waterProg, "aPos", bufWater, 3);
  gl.drawArrays(gl.TRIANGLES, 0, water.length / 3);
  gl.depthMask(true);
  gl.disable(gl.BLEND);

  updateHud();
  requestAnimationFrame(draw);
}

// --- HUD -------------------------------------------------------------------

function nearestHazard(x, z) {
  let best = null;
  let bestD = Infinity;
  for (const h of rocks.list) {
    const d = Math.hypot(h.x - x, h.z - z);
    if (d < bestD) {
      bestD = d;
      best = h;
    }
  }
  return best ? { h: best, d: bestD } : null;
}

function updateHud() {
  const hud = el("hud");
  if (mode !== "boat") {
    hud.style.display = "none";
    return;
  }
  hud.style.display = "block";
  const ft = depthAtWorld(boat.x, boat.z);
  el("hudDepth").textContent = ft == null ? "--" : String(ft);
  el("hudSpeed").textContent = (Math.abs(boat.speed) * 1.94384).toFixed(1);
  const near = nearestHazard(boat.x, boat.z);
  el("hudNear").textContent = near ? `${Math.round(near.d)} m` : "--";
  el("hudNear").style.color = near && near.d < 60 ? "#ff6a5a" : "";
}

function updateStats() {
  el("stats").textContent =
    `${(bottom.count / 3).toLocaleString()} triangles - ${opts.terrace} ft terraces - ` +
    `${rocks.list.length} hazards - ${(trees.count / 12).toLocaleString()} trees`;
}

// --- interaction -----------------------------------------------------------

const pointers = new Map();
let last = null;
let pinch = 0;

canvas.addEventListener("pointerdown", (e) => {
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  last = { x: e.clientX, y: e.clientY };
  canvas.setPointerCapture(e.pointerId);
});

canvas.addEventListener("pointermove", (e) => {
  if (!pointers.has(e.pointerId)) return;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

  if (pointers.size >= 2) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinch > 0) {
      if (mode === "orbit") orbit.dist = clamp(orbit.dist * (pinch / d), 400, 40000);
      else if (mode === "fly") fly.speed = clamp(fly.speed * (d / pinch), 50, 9000);
    }
    pinch = d;
    return;
  }
  if (!last) return;
  const dx = e.clientX - last.x;
  const dy = e.clientY - last.y;
  last = { x: e.clientX, y: e.clientY };

  if (mode === "orbit") {
    orbit.yaw -= dx * 0.006;
    // Clamped short of straight down and short of the horizon: past either the
    // mesh degenerates to a line and the view is useless.
    orbit.pitch = clamp(orbit.pitch + dy * 0.005, 0.08, 1.45);
  } else if (mode === "fly") {
    fly.yaw += dx * 0.005;
    fly.pitch = clamp(fly.pitch - dy * 0.005, -1.4, 1.4);
  } else {
    // In boat mode the drag looks around; steering is A/D, so you can look off
    // the beam while holding a course.
    boat.heading += dx * 0.005;
    boat.pitch = clamp(boat.pitch - dy * 0.004, -0.6, 0.5);
  }
});

function release(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = 0;
  if (pointers.size === 0) last = null;
}
canvas.addEventListener("pointerup", release);
canvas.addEventListener("pointercancel", release);

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const f = e.deltaY > 0 ? 1.12 : 0.89;
  if (mode === "orbit") orbit.dist = clamp(orbit.dist * f, 400, 40000);
  else if (mode === "fly") fly.speed = clamp(fly.speed / f, 50, 9000);
}, { passive: false });

window.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  keys.add(k === "shift" ? "shift" : k);
  if ([" ", "w", "a", "s", "d"].includes(k)) e.preventDefault();
});
window.addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase();
  keys.delete(k === "shift" ? "shift" : k);
});
// A held key with the window unfocused would otherwise stick on forever.
window.addEventListener("blur", () => keys.clear());

for (const [id, key] of [["padFwd", "w"], ["padBack", "s"], ["padLeft", "a"], ["padRight", "d"]]) {
  const b = el(id);
  const on = (e) => { e.preventDefault(); keys.add(key); };
  const off = (e) => { e.preventDefault(); keys.delete(key); };
  b.addEventListener("pointerdown", on);
  b.addEventListener("pointerup", off);
  b.addEventListener("pointercancel", off);
  b.addEventListener("pointerleave", off);
}

// --- controls --------------------------------------------------------------

function setMode(m) {
  mode = m;
  for (const other of MODES) el(`mode_${other}`).classList.toggle("on", other === m);
  el("pad").style.display = m === "orbit" ? "none" : "grid";
  el("spawnRow").style.display = m === "orbit" ? "none" : "flex";
  el("hint").textContent =
    m === "orbit"
      ? "Drag to orbit - pinch or scroll to zoom"
      : m === "fly"
      ? "Drag to look - W/A/S/D to move, Space/Shift for up/down, scroll for speed"
      : "W to throttle up, A/D to steer, drag to look around";
  if (m === "boat" && depthAtWorld(boat.x, boat.z) == null) spawnAt("neoc");
}
for (const m of MODES) el(`mode_${m}`).onclick = () => setMode(m);
for (const key of Object.keys(SPAWNS)) {
  const b = el(`spawn_${key}`);
  if (b) b.onclick = () => spawnAt(key);
}

el("sldExag").addEventListener("input", (e) => {
  opts.exag = +e.target.value;
  el("valExag").textContent = `${opts.exag}x`;
});
el("sldShallow3d").addEventListener("input", (e) => {
  opts.shallow = +e.target.value;
  el("valShallow3d").textContent = opts.shallow === 0 ? "off" : `${opts.shallow} ft`;
});

// The terraces are geometry now, so this rebuilds a ~10 MB buffer. Debounced,
// or dragging the slider would rebuild it thirty times on the way past.
let terraceTimer = null;
el("sldTerrace").addEventListener("input", (e) => {
  opts.terrace = +e.target.value;
  el("valTerrace").textContent = `${opts.terrace} ft`;
  clearTimeout(terraceTimer);
  terraceTimer = setTimeout(rebuildBottom, 140);
});

el("btnGear").onclick = () => {
  const open = document.body.classList.toggle("open");
  el("btnGear").classList.toggle("on", open);
};
el("caveat").onclick = () => el("caveat").classList.toggle("open");

el("btnReset").onclick = () => {
  orbit.yaw = 0;
  orbit.pitch = 0.62;
  orbit.dist = 7000;
  fly.yaw = 0;
  fly.pitch = -0.35;
  fly.speed = 1400;
  spawnAt("neoc");
};

updateStats();
el("valExag").textContent = `${opts.exag}x`;
el("valTerrace").textContent = `${opts.terrace} ft`;
spawnAt("neoc");

// ?mode= deep-links straight into a mode, which is how this gets checked
// without a human clicking, and is handy for a bookmark.
const wanted = new URLSearchParams(location.search).get("mode");
setMode(MODES.includes(wanted) ? wanted : "orbit");

requestAnimationFrame(draw);
