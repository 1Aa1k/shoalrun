// 3D view of the lake bottom. Hand-rolled WebGL, no library: the whole point of
// this project is that it works on a phone with no signal, and a CDN script tag
// is a runtime network dependency wearing a hat.
//
// It renders the SAME interpolated grid the map contours come from, so the mesh
// and the depth lines can never tell different stories. And it is the same 1954
// survey underneath -- a smooth shaded 3D basin looks far more authoritative
// than 260 soundings deserve, which is why the caveat is on the screen, the
// terracing defaults on, and the depth is quantised rather than pretending to a
// continuous surface nobody measured.

import { DepthGrid } from "./depth.js";
import { makeProjection } from "./geo.js";

const { lake: LAKE_GEO, rocks: ROCK_GEO, depth: DEPTH_RAW } = window.SHOALRUN_DATA;

const el = (id) => document.getElementById(id);
const canvas = el("gl");

// Anything thrown during setup used to leave a black canvas and no explanation,
// which is indistinguishable from "the GPU cannot do this" and sent the last
// shader bug hunting in entirely the wrong place. Put the real message on the
// screen instead -- a driver-specific shader failure is exactly the class of bug
// that only ever reproduces on someone else's machine.
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

const FT_PER_M = 3.28084;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// Derivative-based normals are what make terracing read as steps rather than as
// a soft ramp. Without the extension the mesh still draws, just smooth-shaded.
const derivExt = gl.getExtension("OES_standard_derivatives");

// Shared precision, chosen once and substituted into BOTH stages, because the
// only way a cross-stage precision mismatch cannot happen is if there is one
// source of truth for it.
//
// It has to be highp wherever the flat-shading path runs. That path is
// normalize(cross(dFdx(vWorld), dFdy(vWorld))), and vWorld carries a world
// position: at magnitude ~5 a mediump (fp16) ULP is about 0.005, which is the
// entire per-pixel derivative. The subtraction then returns 0 or 1 ULP of pure
// noise, and the terraces render as blown-out white speckle.
const HIGHP_FRAG = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT).precision > 0;
const P = HIGHP_FRAG ? "highp" : "mediump";

// --- shaders ---------------------------------------------------------------

// Precision is stated explicitly on every uniform and varying that crosses the
// stage boundary. A vertex shader defaults to highp and a fragment shader here
// declares mediump, so anything shared silently disagrees -- which links fine on
// SwiftShader and hard-fails on AMD/ANGLE with "declared as type float16_t and
// type float". That failure surfaced as a black screen, because a throw during
// program setup leaves nothing on the canvas.
const VERT = `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute float aDepth;
uniform mat4 uMVP;
uniform float uExag;
uniform ${P} float uTerrace;
varying ${P} float vDepth;
varying ${P} vec3 vNormal;
varying ${P} vec3 vWorld;
void main() {
  // Depth is quantised here, not in the buffer, so the terrace slider does not
  // have to rebuild the mesh. Snapping to the NEAR side (floor) keeps every
  // terrace at or shallower than the interpolated depth -- on a boat, rounding
  // depth the optimistic way is the one direction you must not round.
  float d = uTerrace > 0.0 ? floor(aDepth / uTerrace) * uTerrace : aDepth;
  vec3 p = vec3(aPos.x, -(d / 3.28084) * uExag, aPos.z);
  vDepth = d;
  vWorld = p;
  // Exaggerating Y steepens the true slope, so the normal has to be corrected
  // the opposite way or the lighting stays flat while the terrain gets sharper.
  vNormal = normalize(vec3(aNormal.x * uExag, aNormal.y, aNormal.z * uExag));
  gl_Position = uMVP * vec4(p, 1.0);
}`;

const FRAG = `
#extension GL_OES_standard_derivatives : enable
precision ${P} float;
varying ${P} float vDepth;
varying ${P} vec3 vNormal;
varying ${P} vec3 vWorld;
uniform float uShallow;
uniform float uFlat;

// Same ramp as the 2D map, so the two views agree about what 15 ft looks like.
vec3 ramp(float ft) {
  vec3 c;
  if (ft < 3.0)       c = mix(vec3(0.494,0.769,0.698), vec3(0.408,0.698,0.690), ft / 3.0);
  else if (ft < 6.0)  c = mix(vec3(0.408,0.698,0.690), vec3(0.282,0.580,0.675), (ft-3.0)/3.0);
  else if (ft < 10.0) c = mix(vec3(0.282,0.580,0.675), vec3(0.180,0.463,0.635), (ft-6.0)/4.0);
  else if (ft < 15.0) c = mix(vec3(0.180,0.463,0.635), vec3(0.125,0.369,0.573), (ft-10.0)/5.0);
  else if (ft < 25.0) c = mix(vec3(0.125,0.369,0.573), vec3(0.086,0.282,0.478), (ft-15.0)/10.0);
  else if (ft < 40.0) c = mix(vec3(0.086,0.282,0.478), vec3(0.063,0.212,0.376), (ft-25.0)/15.0);
  else if (ft < 60.0) c = mix(vec3(0.063,0.212,0.376), vec3(0.043,0.157,0.290), (ft-40.0)/20.0);
  else                c = mix(vec3(0.043,0.157,0.290), vec3(0.031,0.118,0.227), min((ft-60.0)/20.0, 1.0));
  return c;
}

void main() {
  vec3 base = ramp(vDepth);
  if (uShallow > 0.0 && vDepth <= uShallow) base = mix(base, vec3(1.0, 0.72, 0.25), 0.55);

  vec3 n = normalize(vNormal);
#ifdef GL_OES_standard_derivatives
  // Flat-shade off the true facet when terracing, so each step gets its own
  // tone and the risers actually read as risers.
  if (uFlat > 0.5) n = normalize(cross(dFdx(vWorld), dFdy(vWorld))) * sign(normalize(vNormal).y);
#endif

  vec3 lightDir = normalize(vec3(0.45, 0.8, 0.4));
  float diff = max(dot(n, lightDir), 0.0);
  // Ambient floor is high: this is a bottom surface being read for shape, and
  // a physically honest light would put half the basin in unreadable shadow.
  vec3 color = base * (0.5 + 0.7 * diff);
  gl_FragColor = vec4(color, 1.0);
}`;

// Land fills every cell the lake mask excludes -- the shore and all 74 islands.
// Without it those cells were simply absent, and a lake full of holes onto the
// background reads as broken geometry rather than as islands.
const LAND_VERT = `
precision highp float;
attribute vec3 aPos;
uniform mat4 uMVP;
uniform float uY;
void main() {
  gl_Position = uMVP * vec4(vec3(aPos.x, aPos.y + uY, aPos.z), 1.0);
}`;

// Translucent water surface, drawn only in boat mode. Sitting 1.8 m off the
// water and seeing the bare bottom is disorienting -- you cannot tell where the
// waterline is, which is the one reference a boat view needs. Thin enough to
// see the bottom through: an opaque surface makes boat mode a blue plane, and
// seeing what is under you is the entire reason to be in boat mode.
const WATER_FRAG = `
precision mediump float;
void main() { gl_FragColor = vec4(0.184, 0.404, 0.510, 0.34); }`;

// Sky gradient, drawn as a screen-space pass before anything else. A flat black
// background made the far shore look like the edge of the world, and in boat
// mode there was no horizon at all to judge attitude against.
const SKY_VERT = `
precision highp float;
attribute vec2 aXY;
varying mediump float vY;
void main() { vY = aXY.y * 0.5 + 0.5; gl_Position = vec4(aXY, 0.0, 1.0); }`;

const SKY_FRAG = `
precision mediump float;
varying mediump float vY;
void main() {
  // Light band held low and wide: the horizon sits near the middle of the
  // screen at boat pitch, and that is exactly where it has to be readable.
  vec3 low  = vec3(0.322, 0.388, 0.443);
  vec3 high = vec3(0.075, 0.110, 0.169);
  gl_FragColor = vec4(mix(low, high, smoothstep(0.30, 1.0, vY)), 1.0);
}`;

const LAND_FRAG = `
precision mediump float;
void main() {
  // Flat, and deliberately so: there is no terrain elevation data for this
  // lake, so any relief here would be invented. Light enough to read as
  // ground -- at the first darker value the islands looked like holes punched
  // in the mesh rather than like land.
  gl_FragColor = vec4(0.294, 0.310, 0.243, 1.0);
}`;

const LINE_VERT = `
precision highp float;
attribute vec3 aPos;
uniform mat4 uMVP;
uniform float uExag;
uniform float uPointSize;
void main() {
  gl_Position = uMVP * vec4(aPos.x, aPos.y * uExag, aPos.z, 1.0);
  gl_PointSize = uPointSize;
}`;

const LINE_FRAG = `
precision mediump float;
uniform vec4 uColor;
void main() { gl_FragColor = uColor; }`;

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

const meshProg = program(VERT, FRAG);
const landProg = program(LAND_VERT, LAND_FRAG);
const waterProg = program(LAND_VERT, WATER_FRAG);
const skyProg = program(SKY_VERT, SKY_FRAG);
const lineProg = program(LINE_VERT, LINE_FRAG);

const bufSky = (() => {
  const b = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  return b;
})();

// --- mesh ------------------------------------------------------------------

const cx = (grid.worldX(0) + grid.worldX(grid.nx - 1)) / 2;
const cz = (grid.worldY(0) + grid.worldY(grid.ny - 1)) / 2;

// A cell just outside the lake still has a real depth: zero. Treating the first
// ring of land cells as 0 ft closes the mesh right up to the waterline instead
// of leaving the ragged fringe you get from requiring all four corners wet --
// which is what made the shoreline look chewed.
function depthForMesh(col, row) {
  const v = grid.at(col, row);
  if (v !== null) return { ft: v, wet: true };
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      if (grid.at(col + dc, row + dr) !== null) return { ft: 0, wet: false };
    }
  }
  return null;
}

function buildMesh() {
  const pos = [];
  const norm = [];
  const dep = [];
  const idx = [];
  const vertIndex = new Int32Array(grid.nx * grid.ny).fill(-1);
  const cell = new Array(grid.nx * grid.ny).fill(null);

  for (let row = 0; row < grid.ny; row++) {
    for (let col = 0; col < grid.nx; col++) {
      cell[row * grid.nx + col] = depthForMesh(col, row);
    }
  }
  const ftAt = (col, row) => {
    if (col < 0 || row < 0 || col >= grid.nx || row >= grid.ny) return null;
    const c = cell[row * grid.nx + col];
    return c ? c.ft : null;
  };

  for (let row = 0; row < grid.ny; row++) {
    for (let col = 0; col < grid.nx; col++) {
      const c = cell[row * grid.nx + col];
      if (!c) continue;
      vertIndex[row * grid.nx + col] = pos.length / 3;

      // +north maps to -Z so the default camera looks north up the lake, which
      // is how the survey map is drawn and how people describe this lake. Y is
      // filled by the vertex shader from the depth attribute.
      pos.push(grid.worldX(col) - cx, 0, -(grid.worldY(row) - cz));
      dep.push(c.ft);

      const l = ftAt(col - 1, row) ?? c.ft;
      const r = ftAt(col + 1, row) ?? c.ft;
      const d = ftAt(col, row - 1) ?? c.ft;
      const u = ftAt(col, row + 1) ?? c.ft;
      const sx = ((l - r) / FT_PER_M) / (2 * grid.gridM);
      const sz = ((d - u) / FT_PER_M) / (2 * grid.gridM);
      const len = Math.hypot(sx, 1, sz);
      norm.push(sx / len, 1 / len, sz / len);
    }
  }

  // A quad needs four corners, and at least one of them genuinely wet -- that
  // last condition is what stops the mesh sheeting across an island whose whole
  // footprint is dry. `covered` records which cells the water mesh claimed, so
  // the land pass can fill exactly the complement and nothing overlaps.
  const covered = new Uint8Array((grid.nx - 1) * (grid.ny - 1));
  for (let row = 0; row < grid.ny - 1; row++) {
    for (let col = 0; col < grid.nx - 1; col++) {
      const ia = vertIndex[row * grid.nx + col];
      const ib = vertIndex[row * grid.nx + col + 1];
      const ic = vertIndex[(row + 1) * grid.nx + col + 1];
      const ie = vertIndex[(row + 1) * grid.nx + col];
      if (ia < 0 || ib < 0 || ic < 0 || ie < 0) continue;
      const anyWet =
        cell[row * grid.nx + col].wet ||
        cell[row * grid.nx + col + 1].wet ||
        cell[(row + 1) * grid.nx + col + 1].wet ||
        cell[(row + 1) * grid.nx + col].wet;
      if (!anyWet) continue;
      idx.push(ia, ib, ic, ia, ic, ie);
      covered[row * (grid.nx - 1) + col] = 1;
    }
  }

  return {
    pos: new Float32Array(pos),
    norm: new Float32Array(norm),
    dep: new Float32Array(dep),
    idx: new Uint32Array(idx),
    count: idx.length,
    covered,
  };
}

// Land fills exactly the cells the water mesh did not claim -- the shore and
// every one of the 74 islands -- plus a skirt out to the horizon. Cutting it
// per-cell rather than laying one slab over everything is the whole trick: a
// slab hides the lake, and no slab leaves the islands as holes onto the
// background, which is what made the geometry look broken.
function buildLand(covered, wantWater = false) {
  const v = [];
  const y = 0;
  const quad = (ax, az, bx, bz) => {
    v.push(ax, y, az, bx, y, az, bx, y, bz, ax, y, az, bx, y, bz, ax, y, bz);
  };

  for (let row = 0; row < grid.ny - 1; row++) {
    for (let col = 0; col < grid.nx - 1; col++) {
      if (!!covered[row * (grid.nx - 1) + col] !== wantWater) continue;
      quad(
        grid.worldX(col) - cx,
        -(grid.worldY(row) - cz),
        grid.worldX(col + 1) - cx,
        -(grid.worldY(row + 1) - cz)
      );
    }
  }

  // Skirt beyond the grid, so the horizon is ground rather than a cut edge.
  if (!wantWater) {
    const x0 = grid.worldX(0) - cx;
    const x1 = grid.worldX(grid.nx - 1) - cx;
    const z0 = -(grid.worldY(0) - cz);
    const z1 = -(grid.worldY(grid.ny - 1) - cz);
    const pad = 15000;
    quad(x0 - pad, z0 + pad, x1 + pad, z0);
    quad(x0 - pad, z1, x1 + pad, z1 - pad);
    quad(x0 - pad, z0, x0, z1);
    quad(x1, z0, x1 + pad, z1);
  }

  return new Float32Array(v);
}

// Index counts here run past 65535, so 32-bit indices are required. Every
// target browser has the extension, but failing loudly beats rendering a
// scrambled lake.
if (!gl.getExtension("OES_element_index_uint")) throw new Error("OES_element_index_uint unavailable");

const mesh = buildMesh();

function bufferOf(data, target = gl.ARRAY_BUFFER) {
  const b = gl.createBuffer();
  gl.bindBuffer(target, b);
  gl.bufferData(target, data, gl.STATIC_DRAW);
  return b;
}

const bufPos = bufferOf(mesh.pos);
const bufNorm = bufferOf(mesh.norm);
const bufDep = bufferOf(mesh.dep);
const bufIdx = bufferOf(mesh.idx, gl.ELEMENT_ARRAY_BUFFER);
const land = buildLand(mesh.covered);
const bufLand = bufferOf(land);
const water = buildLand(mesh.covered, true);
const bufWater = bufferOf(water);

// --- shoreline + hazards ---------------------------------------------------

function buildShore() {
  const segs = [];
  const push = (ring) => {
    for (let i = 0; i < ring.length - 1; i++) {
      segs.push(ring[i][0] - cx, 0, -(ring[i][1] - cz));
      segs.push(ring[i + 1][0] - cx, 0, -(ring[i + 1][1] - cz));
    }
  };
  const geom = LAKE_GEO.geometry;
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
  for (const poly of polys) {
    for (const ring of poly) push(ring.map(([lon, lat]) => proj.fwd(lon, lat)));
  }
  return new Float32Array(segs);
}

// Hazards as vertical stems from the bottom to the surface. A dot floating at
// depth is unreadable in 3D -- you cannot tell how far away it is. A stem tells
// you where it is on the bottom and how much water is over it in one mark.
function buildHazards() {
  const stems = [];
  const heads = [];
  const list = [];
  for (const f of ROCK_GEO.features) {
    const p = f.properties;
    if (p.offshore === false) continue;
    const [x, y] = proj.fwd(p.lon, p.lat);
    const ft = grid.sampleXY(x, y);
    if (ft === null) continue;
    const wx = x - cx;
    const wz = -(y - cz);
    stems.push(wx, -ft / FT_PER_M, wz, wx, 0, wz);
    heads.push(wx, 0, wz);
    list.push({ x: wx, z: wz, ft, cls: p.class });
  }
  return { stems: new Float32Array(stems), heads: new Float32Array(heads), list };
}

const shore = buildShore();
const hz = buildHazards();
const bufShore = bufferOf(shore);
const bufStems = bufferOf(hz.stems);
const bufHeads = bufferOf(hz.heads);

// --- camera ----------------------------------------------------------------

// Three modes, because "look at the lake" and "be on the lake" want different
// controls and neither is served well by the other.
//   orbit -- the whole basin, spun around its centre. Good for shape.
//   fly   -- free camera. Good for getting in close to one shoal.
//   boat  -- eye height above the surface, driving. Good for "what am I about
//            to hit", which is the question this whole project exists to answer.
const MODES = ["orbit", "fly", "boat"];
let mode = "orbit";

const orbit = { yaw: 0, pitch: 0.62, dist: 7000 };
const fly = { x: 0, y: 1200, z: 5000, yaw: 0, pitch: -0.35, speed: 400 };
const boat = { x: 0, z: 2000, yaw: Math.PI, pitch: -0.05, speed: 0, heading: Math.PI };
const opts = { exag: 8, shallow: 0, terrace: 3 };

const BOAT_EYE_M = 1.8;      // eye height above the waterline
const BOAT_MAX_MS = 12;      // ~23 kn, about what an outboard on this lake does
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

// Depth under an arbitrary world point, for the boat readout. World Z is
// negated north, so it has to be undone before sampling.
function depthAtWorld(wx, wz) {
  return grid.sampleXY(wx + cx, -wz + cz);
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

  // boat
  if (keys.has("a")) boat.heading -= 1.1 * dt;
  if (keys.has("d")) boat.heading += 1.1 * dt;
  const target = keys.has("w") ? BOAT_MAX_MS : keys.has("s") ? -BOAT_MAX_MS * 0.4 : 0;
  // Eased rather than instant, because instant throttle makes the depth readout
  // jump in a way that is impossible to read.
  boat.speed += (target - boat.speed) * Math.min(1, dt * 1.6);
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

function bindAttr(prog, name, buf, size) {
  const loc = gl.getAttribLocation(prog, name);
  if (loc < 0) return;
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
}

let lastT = performance.now();

function draw(now) {
  const dt = Math.min(0.05, (now - lastT) / 1000) || 0.016;
  lastT = now;

  const { eye, target } = cameraFor(dt);

  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  // Sky first, with depth writes off so it never occludes the scene.
  gl.disable(gl.DEPTH_TEST);
  gl.depthMask(false);
  gl.useProgram(skyProg);
  const skyLoc = gl.getAttribLocation(skyProg, "aXY");
  gl.bindBuffer(gl.ARRAY_BUFFER, bufSky);
  gl.enableVertexAttribArray(skyLoc);
  gl.vertexAttribPointer(skyLoc, 2, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.TRIANGLES, 0, 3);
  gl.depthMask(true);
  gl.enable(gl.DEPTH_TEST);

  const view = lookAt(eye, target, [0, 1, 0]);
  // Near plane is tight so boat mode can sit 1.8 m off the water without
  // clipping through it; far plane is generous because at low pitch the far
  // shore is most of the lake away.
  const projM = perspective(0.9, canvas.width / canvas.height, 0.5, 80000);
  const mvp = new Float32Array(multiply(view, projM));

  // Boat mode ignores the exaggeration slider. From 1.8 m above the water,
  // 8x turns a 20 ft bottom into a 160 ft canyon and puts every shallow at eye
  // level -- the one view where the geometry has to be true is the one where
  // you are pretending to be on it.
  const exag = mode === "boat" ? 1 : opts.exag;

  gl.useProgram(landProg);
  gl.uniformMatrix4fv(gl.getUniformLocation(landProg, "uMVP"), false, mvp);
  // Land sits a hair under the waterline so the shoreline line reads on top.
  gl.uniform1f(gl.getUniformLocation(landProg, "uY"), -0.05);
  bindAttr(landProg, "aPos", bufLand, 3);
  gl.drawArrays(gl.TRIANGLES, 0, land.length / 3);

  gl.useProgram(meshProg);
  gl.uniformMatrix4fv(gl.getUniformLocation(meshProg, "uMVP"), false, mvp);
  gl.uniform1f(gl.getUniformLocation(meshProg, "uExag"), exag);
  gl.uniform1f(gl.getUniformLocation(meshProg, "uShallow"), opts.shallow);
  gl.uniform1f(gl.getUniformLocation(meshProg, "uTerrace"), opts.terrace);
  // Flat shading needs both the derivative extension and highp in the fragment
  // stage. Without highp the derivative is precision noise, which renders as
  // white speckle -- smooth-shaded terraces are the better failure.
  gl.uniform1f(
    gl.getUniformLocation(meshProg, "uFlat"),
    opts.terrace > 0 && derivExt && HIGHP_FRAG ? 1 : 0
  );

  bindAttr(meshProg, "aPos", bufPos, 3);
  bindAttr(meshProg, "aNormal", bufNorm, 3);
  bindAttr(meshProg, "aDepth", bufDep, 1);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bufIdx);
  gl.drawElements(gl.TRIANGLES, mesh.count, gl.UNSIGNED_INT, 0);

  // Water surface last among the solids and with blending on, so the bottom
  // shows through it rather than being replaced by it.
  if (mode === "boat") {
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    gl.useProgram(waterProg);
    gl.uniformMatrix4fv(gl.getUniformLocation(waterProg, "uMVP"), false, mvp);
    gl.uniform1f(gl.getUniformLocation(waterProg, "uY"), 0);
    bindAttr(waterProg, "aPos", bufWater, 3);
    gl.drawArrays(gl.TRIANGLES, 0, water.length / 3);
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }

  gl.useProgram(lineProg);
  gl.uniformMatrix4fv(gl.getUniformLocation(lineProg, "uMVP"), false, mvp);
  const exagLoc = gl.getUniformLocation(lineProg, "uExag");
  const colorLoc = gl.getUniformLocation(lineProg, "uColor");

  // Shoreline sits at Y=0, so exaggeration must not move it.
  gl.uniform1f(exagLoc, 1.0);
  gl.uniform4f(colorLoc, 0.5, 0.77, 0.91, 0.85);
  bindAttr(lineProg, "aPos", bufShore, 3);
  gl.drawArrays(gl.LINES, 0, shore.length / 3);

  if (hz.stems.length) {
    gl.uniform1f(exagLoc, exag);
    gl.uniform4f(colorLoc, 1.0, 0.42, 0.35, 0.75);
    bindAttr(lineProg, "aPos", bufStems, 3);
    gl.drawArrays(gl.LINES, 0, hz.stems.length / 3);

    gl.uniform1f(exagLoc, 1.0);
    gl.uniform4f(colorLoc, 1.0, 0.35, 0.28, 1.0);
    // Bigger marks in boat mode: from the helm a hazard has to be findable at a
    // glance, not hunted for among 7 px dots.
    gl.uniform1f(gl.getUniformLocation(lineProg, "uPointSize"), mode === "boat" ? 11 : 7);
    bindAttr(lineProg, "aPos", bufHeads, 3);
    gl.drawArrays(gl.POINTS, 0, hz.heads.length / 3);
  }

  updateHud();
  requestAnimationFrame(draw);
}

// --- HUD -------------------------------------------------------------------

function nearestHazard(x, z) {
  let best = null;
  let bestD = Infinity;
  for (const h of hz.list) {
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
      else if (mode === "fly") fly.speed = clamp(fly.speed * (d / pinch), 20, 4000);
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
    // In boat mode the drag is looking around, not steering -- steering is the
    // A/D keys, so you can look off the beam while holding a course.
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
  const f = e.deltaY > 0 ? 1.1 : 0.91;
  if (mode === "orbit") orbit.dist = clamp(orbit.dist * f, 400, 40000);
  else if (mode === "fly") fly.speed = clamp(fly.speed / f, 20, 4000);
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

// Touch throttle, so boat and fly modes work without a keyboard.
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

// Nearest cell to the lake centre with real water under it. Spawning at a fixed
// coordinate put the boat on the 0 ft shore ring, where the view is a wall of
// shallow and nothing reads.
function findWater(minFt = 12) {
  let best = null;
  let bestD = Infinity;
  for (let row = 0; row < grid.ny; row += 2) {
    for (let col = 0; col < grid.nx; col += 2) {
      const ft = grid.at(col, row);
      if (ft === null || ft < minFt) continue;
      const x = grid.worldX(col) - cx;
      const z = -(grid.worldY(row) - cz);
      const d = Math.hypot(x, z);
      if (d < bestD) {
        bestD = d;
        best = { x, z };
      }
    }
  }
  return best || { x: 0, z: 0 };
}

function setMode(m) {
  mode = m;
  for (const other of MODES) el(`mode_${other}`).classList.toggle("on", other === m);
  el("pad").style.display = m === "orbit" ? "none" : "grid";
  el("hint").textContent =
    m === "orbit"
      ? "Drag to orbit - pinch or scroll to zoom"
      : m === "fly"
      ? "Drag to look - W/A/S/D to move, Space/Shift for up/down, scroll for speed"
      : "W to throttle up, A/D to steer, drag to look around";
  if (m === "boat") {
    // Drop the boat somewhere it is actually floating rather than wherever the
    // camera last was, which is usually over land or 1200 m in the air.
    const here = depthAtWorld(boat.x, boat.z);
    if (here == null || here < 6) {
      const spot = findWater();
      boat.x = spot.x;
      boat.z = spot.z;
    }
    boat.speed = 0;
  }
}
for (const m of MODES) el(`mode_${m}`).onclick = () => setMode(m);

el("sldExag").addEventListener("input", (e) => {
  opts.exag = +e.target.value;
  el("valExag").textContent = `${opts.exag}x`;
});
el("sldShallow3d").addEventListener("input", (e) => {
  opts.shallow = +e.target.value;
  el("valShallow3d").textContent = opts.shallow === 0 ? "off" : `${opts.shallow} ft`;
});
el("sldTerrace").addEventListener("input", (e) => {
  opts.terrace = +e.target.value;
  el("valTerrace").textContent = opts.terrace === 0 ? "smooth" : `${opts.terrace} ft`;
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
  fly.x = 0; fly.y = 1200; fly.z = 5000; fly.yaw = 0; fly.pitch = -0.35;
  boat.x = 0; boat.z = 2000; boat.heading = Math.PI; boat.pitch = -0.05; boat.speed = 0;
};

el("stats").textContent =
  `${(mesh.count / 3).toLocaleString()} triangles - ${grid.nx}x${grid.ny} grid at ` +
  `${grid.gridM} m - 0-${grid.maxFt} ft - ${hz.list.length} hazards`;
el("valExag").textContent = `${opts.exag}x`;
el("valTerrace").textContent = `${opts.terrace} ft`;

// ?mode=boat deep-links straight into a mode, which is how this gets checked
// without a human clicking, and is handy for a bookmark.
const wanted = new URLSearchParams(location.search).get("mode");
setMode(MODES.includes(wanted) ? wanted : "orbit");

requestAnimationFrame(draw);
