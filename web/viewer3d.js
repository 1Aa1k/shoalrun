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
  buildSurveyBoat,
  buildWaterPatch,
  waveAt,
} from "./scene3d.js";
import { shaderSources } from "./shaders3d.js";
import { perspective, multiply, lookAt, forwardOf, modelYaw, add3, sub3, cross3, norm3 } from "./mat3d.js";

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
  box.classList.add("show");
  box.innerHTML =
    `<div><p style="color:#f2b0a6;font-weight:700">3D view failed to start</p>` +
    `<p style="font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:pre-wrap">${
      String(msg).replace(/[<&]/g, (c) => (c === "<" ? "&lt;" : "&amp;"))
    }</p><p>The map tab still works.</p></div>`;
}

// Only while this view is being stood up. Once it is running, an error anywhere
// in the app would otherwise blank the 3D tab with a driver-failure message
// that has nothing to do with what went wrong -- the map has its own handler
// and it reports to the status line where the user can see it.
const onSetupError = (e) => fatal(e.message);
window.addEventListener("error", onSetupError);

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
const rockProg = program(S.rock_vert, S.lit_frag);
const waterProg = program(S.water_vert, S.water_frag);
const patchProg = program(S.waterpatch_vert, S.water_frag);
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

// blockCells is in source grid cells; the grid is 25 m, so 1 -> 25 m voxels.
// 25 m is the floor because that is the resolution the depth surface actually
// has -- subdividing below it would resample an interpolation and invent
// structure the 1954 survey never measured.
const opts = { exag: 30, shallow: 0, terrace: 3, block: 1, wave: 2.0, sway: true };

// The terraced bottom is real geometry with vertical risers, so changing the
// terrace rebuilds it. Buffers are reused rather than reallocated.
let bottom = buildSteppedMesh(grid, cx, cz, opts.terrace, opts.block);
const bufBottomPos = buf(bottom.pos);
const bufBottomNorm = buf(bottom.norm);
const bufBottomDep = buf(bottom.dep);

function rebuildBottom() {
  bottom = buildSteppedMesh(grid, cx, cz, opts.terrace, opts.block);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomPos);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.pos, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomNorm);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.norm, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufBottomDep);
  gl.bufferData(gl.ARRAY_BUFFER, bottom.dep, gl.STATIC_DRAW);

  // Water blocks follow the bottom blocks, so the swell attenuation is keyed to
  // the same depth the shelf under it was built from.
  water = buildFlatCells(grid, cx, cz, true, opts.block);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufWater);
  gl.bufferData(gl.ARRAY_BUFFER, water.pos, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, bufWaterDep);
  gl.bufferData(gl.ARRAY_BUFFER, water.dep, gl.STATIC_DRAW);
  updateStats();
}

const rings = shoreRings(LAKE_GEO, proj);
const land = buildFlatCells(grid, cx, cz, false, opts.block);
let water = buildFlatCells(grid, cx, cz, true, opts.block);
const shore = buildShore(rings, cx, cz);
const trees = buildTrees(grid, rings, cx, cz);
const rocks = buildHazardRocks(grid, ROCK_GEO, proj, cx, cz);
const boatMesh = buildSurveyBoat();

const bufLand = buf(land.pos);
const bufWater = buf(water.pos);
const bufWaterDep = buf(water.dep);
const bufShore = buf(shore);
const bufTreePos = buf(trees.pos);
const bufTreeNorm = buf(trees.norm);
const bufTreeShade = buf(trees.shade);
const bufBreakPos = buf(rocks.breaking.pos);
const bufBreakNorm = buf(rocks.breaking.norm);
const bufBreakShade = buf(rocks.breaking.shade);
const bufShoalPos = buf(rocks.shoal.pos);
const bufShoalNorm = buf(rocks.shoal.norm);
const bufShoalShade = buf(rocks.shoal.shade);
const bufBreakBase = buf(rocks.breaking.base);
const bufShoalBase = buf(rocks.shoal.base);
const bufStems = buf(rocks.stems);
const bufHullPos = buf(boatMesh.pos);
const bufHullNorm = buf(boatMesh.norm);
const bufHullShade = buf(boatMesh.shade);
const bufSky = buf(new Float32Array([-1, -1, 3, -1, -1, 3]));

// 320 m of finely tessellated water that follows the camera. Beyond it the
// coarse lake-wide surface takes over, where the waves are sub-pixel anyway.
const patch = buildWaterPatch(128, 2.5);
const bufPatchPos = buf(patch.pos);
const patchDepth = new Float32Array(patch.count);
const bufPatchDep = buf(patchDepth);

// Land goes through the lit shader like everything else, so it needs matching
// normal and shade attributes. These have to be sized per-vertex or the shader
// reads off the end of the buffer -- three floats per vertex for the normal,
// one for the shade.
const bufLandNorm = buf((() => {
  const n = new Float32Array(land.pos.length);
  for (let i = 1; i < n.length; i += 3) n[i] = 1; // straight up
  return n;
})());
const bufLandShade = buf(new Float32Array(land.count).fill(1));

// --- camera ----------------------------------------------------------------

const MODES = ["orbit", "fly", "boat"];
let mode = "orbit";

const orbit = { yaw: 0, pitch: 0.62, dist: 7000 };
// Fly starts fast. At 400 m/s it took most of a minute to cross a 9 km lake,
// which is not flying, it is drifting.
const fly = { x: 0, y: 1200, z: 5000, yaw: 0, pitch: -0.35, speed: 1400 };
// heading is where the hull points; camYaw is where the camera points; look is
// the drag offset between them. They were one variable, which meant dragging
// steered the boat AND the camera together, so the hull could never appear to
// turn -- it was welded to the view.
// Each part gets a shadow tone and a lit tone; the lit shader mixes between them
// on the shade attribute. Orange on the hulls is not decoration -- a survey boat
// that cannot be spotted again is a survey boat you have lost.
const BOAT_COLORS = {
  hull:  [[0.55, 0.20, 0.06], [0.94, 0.46, 0.18]],
  deck:  [[0.22, 0.25, 0.30], [0.52, 0.56, 0.62]],
  solar: [[0.11, 0.15, 0.30], [0.30, 0.40, 0.62]],
  sonar: [[0.12, 0.26, 0.42], [0.38, 0.68, 1.00]],
};

const BOAT_EYE_M = 1.9;
// Chase camera. The boat is 1.8 m long, so the distance that frames it is small
// -- at the 8 m a full-size boat would want, it is a speck.
const CHASE = { dist: 5.6, height: 1.0, lookAhead: 3.0, min: 2.2, max: 22,
                follow: 3.2, maxLag: 0.22 };

const boat = {
  x: 0, z: 0, speed: 0,
  heading: Math.PI,
  camYaw: Math.PI,
  look: 0,
  pitch: -0.06,
  roll: 0,
  chase: true,          // third person by default -- there is a boat to look at now
  dist: CHASE.dist,
  looking: false,       // true while a drag is actively turning the view
};

const BOAT_MAX_MS = 12; // ~23 kn, about what an outboard on this lake does
const keys = new Set();

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

  // Point at the nearest hazard rather than at whatever bearing was left over.
  // Spawning at "shoals" and facing empty water is a spawn that does not do what
  // its name says.
  let best = null;
  let bestD = Infinity;
  for (const h of rocks.list) {
    const d = (h.x - s.x) ** 2 + (h.z - s.z) ** 2;
    if (d < bestD) { bestD = d; best = h; }
  }
  if (best) {
    // forwardOf(yaw) is [sin(yaw), *, -cos(yaw)] -- forward is -Z, so the z term
    // negates. Getting this wrong spawns you facing exactly away from the thing
    // the spawn is named after, which is what it used to do.
    boat.heading = Math.atan2(best.x - s.x, -(best.z - s.z));
  }
  boat.camYaw = boat.heading;
  boat.look = 0;
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

  // Turn rate falls off with speed, the way a hull actually behaves -- a boat
  // at rest pivots, a boat at 20 kn carves.
  let turn = 0;
  if (keys.has("a")) turn -= 1;
  if (keys.has("d")) turn += 1;
  const rate = 1.35 / (1 + Math.abs(boat.speed) * 0.06);
  boat.heading += turn * rate * dt;

  const want = keys.has("w") ? BOAT_MAX_MS : keys.has("s") ? -BOAT_MAX_MS * 0.4 : 0;
  // Eased rather than instant, because instant throttle makes the depth readout
  // jump in a way that is impossible to read.
  boat.speed += (want - boat.speed) * Math.min(1, dt * 1.6);
  const h = forwardOf(boat.heading, 0);
  boat.x += h[0] * boat.speed * dt;
  boat.z += h[2] * boat.speed * dt;

  // Camera chases the heading rather than being locked to it. A little lag is
  // what makes the boat read as turning instead of the world spinning around a
  // fixed bow. Free look springs back once the drag ends.
  if (!boat.looking) boat.look += (0 - boat.look) * Math.min(1, dt * 1.1);

  const wrap = (a) => {
    while (a > Math.PI) a -= Math.PI * 2;
    while (a < -Math.PI) a += Math.PI * 2;
    return a;
  };
  let err = wrap(boat.heading + boat.look - boat.camYaw);
  boat.camYaw += err * Math.min(1, dt * CHASE.follow);
  // A pure exponential follow settles at a *constant* offset during a sustained
  // turn -- rate/follow radians -- so the boat sits permanently cocked in frame
  // and reads as sliding sideways rather than turning. Capping the offset ends
  // the turn with the camera behind the boat, which is where it belongs.
  if (!boat.looking) {
    const over = wrap(boat.heading - boat.camYaw);
    if (Math.abs(over) > CHASE.maxLag) boat.camYaw = boat.heading - Math.sign(over) * CHASE.maxLag;
  }
  boat.roll += (-turn * Math.min(1, Math.abs(boat.speed) / BOAT_MAX_MS) * 0.13 - boat.roll)
    * Math.min(1, dt * 2.5);

  // Ride the water rather than hovering over it. Sampling the same wave
  // function the shader draws with is the whole reason it is defined once --
  // a second hand-kept copy would drift and the hull would sit in water that
  // is not the water on screen.
  const fade = Math.min(1, Math.max(0, (depthAtWorld(boat.x, boat.z) ?? 0) / 8));
  const w = waveAt(boat.x, boat.z, clock, fade * opts.wave);
  // Slope along the hull's own axes gives pitch and roll from the surface.
  const fwd = forwardOf(boat.heading, 0);
  const rightV = [-fwd[2], 0, fwd[0]];
  const slopeF = w.dx * fwd[0] + w.dz * fwd[2];
  const slopeR = w.dx * rightV[0] + w.dz * rightV[2];

  // Sway is the camera's share of the boat's motion -- heave, and the pitch and
  // roll the surface slope imparts. With it off the eye holds a fixed height and
  // a level horizon while the hull still rides the water underneath: steadier to
  // read a depth off, and the obvious thing to reach for if the motion is making
  // someone queasy. The hull always rides, because a boat that ignores the water
  // it is floating on looks broken either way.
  const sway = opts.sway ? 1 : 0;
  // First person puts the eye at the helm. Third person pulls back along the
  // camera's own yaw -- not the hull's heading -- so the boat swings in frame
  // during a turn instead of the world rotating around a fixed stern.
  // The hull's own attitude: it banks into its turns and takes the surface slope
  // under it. This is the boat moving, not the camera, so it is independent of
  // the sway toggle and of which view you are in.
  const hull = {
    y: w.y,
    pitch: slopeF * 0.8,
    roll: boat.roll + slopeR * 0.9,
  };

  let eye;
  let target;
  if (boat.chase) {
    const back = forwardOf(boat.camYaw, 0);
    const ride = w.y * sway;
    eye = [
      boat.x - back[0] * boat.dist,
      ride + CHASE.height + boat.dist * 0.09,
      boat.z - back[2] * boat.dist,
    ];
    target = [
      boat.x + back[0] * CHASE.lookAhead,
      ride + 0.35 + (boat.pitch - slopeF * 0.35 * sway) * CHASE.lookAhead,
      boat.z + back[2] * CHASE.lookAhead,
    ];
    // Third person keeps the horizon level. Rolling the camera here would tilt
    // the whole world while the boat stayed pinned at the same angle on screen --
    // which looks like the lake wobbling, not the boat leaning. The hull carries
    // the lean instead, which is the thing you are actually looking at.
    return { eye, target, roll: 0, hull };
  }
  eye = [boat.x, BOAT_EYE_M + w.y * sway, boat.z];
  return {
    eye,
    target: add3(eye, forwardOf(boat.camYaw, boat.pitch - slopeF * 0.35 * sway)),
    // First person is the one view where camera roll is right: your head goes
    // with the hull. A beam sea leans you the way it should.
    roll: (boat.roll + slopeR * 0.45) * sway,
    hull,
  };
}

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const r = canvas.getBoundingClientRect();
  // A hidden section measures 0x0. Resizing the drawing buffer to nothing while
  // the map tab is open would leave the 3D view blank on return, so a rotate or
  // a keyboard opening in another view is simply ignored -- the next real frame
  // in this view resizes anyway.
  if (r.width < 1 || r.height < 1) return;
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

// Depth under every patch vertex, refreshed when the patch moves. The wave
// fade needs it, and the patch is not fixed to the world, so it cannot be
// baked in. Snapped to the patch spacing so the grid does not swim under the
// waves as the boat moves through it.
let patchCentre = [NaN, NaN];
function updatePatch(x, z) {
  const g = patch.spacing;
  const sx = Math.round(x / g) * g;
  const sz = Math.round(z / g) * g;
  if (sx === patchCentre[0] && sz === patchCentre[1]) return patchCentre;
  patchCentre = [sx, sz];
  for (let i = 0; i < patch.count; i++) {
    const px = patch.pos[i * 3] + sx;
    const pz = patch.pos[i * 3 + 2] + sz;
    const ft = grid.sampleXY(px + cx, -pz + cz);
    patchDepth[i] = ft === null ? 0 : ft;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, bufPatchDep);
  gl.bufferData(gl.ARRAY_BUFFER, patchDepth, gl.DYNAMIC_DRAW);
  return patchCentre;
}

// Hull sits in the camera's own frame, so it is placed by building a model
// matrix from the boat's heading and position rather than by baking it in.
// Roll first about the hull's own long axis, then pitch about its beam, then
// yaw into the world. Rolling last would tilt the boat about a world axis and
// the bow would swing sideways instead of the deck leaning.
function hullMatrix(hull) {
  const cp = Math.cos(hull.pitch), sp = Math.sin(hull.pitch);
  const cr = Math.cos(hull.roll), sr = Math.sin(hull.roll);
  const roll = [cr, sr, 0, 0, -sr, cr, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  const pitch = [1, 0, 0, 0, 0, cp, sp, 0, 0, -sp, cp, 0, 0, 0, 0, 1];
  const yaw = modelYaw(boat.heading);
  const m = multiply(roll, multiply(pitch, yaw));
  m[12] = boat.x;
  m[13] = hull.y;
  m[14] = boat.z;
  return m;
}

let lastT = performance.now();
let clock = 0;

// This view shares a page with the map now, and both run their own animation
// loop. Rendering 156k triangles behind a tab nobody is on costs a phone real
// battery for frames that are never composited, so the loop stays alive -- it
// has to, to pick up again instantly -- but skips the work.
const section3d = el("view-3d");
const onScreen = () => section3d.classList.contains("active");
let wasOnScreen = true;

function draw(now) {
  const visible = onScreen();
  if (!visible) {
    // Reset the clock, or the first frame back computes a dt of however many
    // seconds the tab was hidden and the camera lurches.
    lastT = now;
    wasOnScreen = false;
    requestAnimationFrame(draw);
    return;
  }
  if (!wasOnScreen) {
    // Any rotate or window change while this view was hidden was ignored,
    // because a hidden canvas measures zero. Pick it up on the way back in.
    wasOnScreen = true;
    resize();
  }
  const dt = Math.min(0.05, (now - lastT) / 1000) || 0.016;
  lastT = now;
  clock += dt;

  const { eye, target, roll, hull } = cameraFor(dt);
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

  // Roll has to be about the camera's own forward axis. Tilting a world-space
  // up vector instead only rolls when you happen to be looking down -Z; at
  // other headings the same tilt comes out as pitch and throws the view --
  // and the hull with it -- off the screen.
  let up = [0, 1, 0];
  if (roll) {
    const fwd = norm3(sub3(target, eye));
    const right = norm3(cross3(fwd, [0, 1, 0]));
    const c = Math.cos(roll);
    const sn = Math.sin(roll);
    up = norm3([
      right[0] * sn + up[0] * c,
      right[1] * sn + up[1] * c,
      right[2] * sn + up[2] * c,
    ]);
  }
  const view = lookAt(eye, target, up);
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
  gl.drawArrays(gl.TRIANGLES, 0, land.count);

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

  // Two passes rather than one, because the classes mean different things and a
  // single A/B mix had to carry both the class and the tone. Now each class owns
  // its own pair and the shade attribute is free to vary the tone within it --
  // which is what stops a thousand of them reading as one mass.
  // Rocks go through their own program: it exaggerates where they sit without
  // exaggerating how big they are.
  gl.useProgram(rockProg);
  gl.uniformMatrix4fv(uni(rockProg, "uMVP"), false, mvp);
  gl.uniform1f(uni(rockProg, "uExag"), exag);

  gl.uniform3f(uni(rockProg, "uColorA"), 0.40, 0.36, 0.34);   // wet granite
  gl.uniform3f(uni(rockProg, "uColorB"), 0.72, 0.66, 0.60);   // dry, sun-bleached
  attr(rockProg, "aPos", bufBreakPos, 3);
  attr(rockProg, "aNormal", bufBreakNorm, 3);
  attr(rockProg, "aShade", bufBreakShade, 1);
  attr(rockProg, "aBase", bufBreakBase, 1);
  gl.drawArrays(gl.TRIANGLES, 0, rocks.breaking.count);

  gl.uniform3f(uni(rockProg, "uColorA"), 0.36, 0.30, 0.16);   // shoal in shadow
  gl.uniform3f(uni(rockProg, "uColorB"), 0.86, 0.62, 0.22);   // shoal lit
  attr(rockProg, "aPos", bufShoalPos, 3);
  attr(rockProg, "aNormal", bufShoalNorm, 3);
  attr(rockProg, "aShade", bufShoalShade, 1);
  attr(rockProg, "aBase", bufShoalBase, 1);
  gl.drawArrays(gl.TRIANGLES, 0, rocks.shoal.count);

  gl.useProgram(litProg);
  gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, mvp);

  gl.uniform1f(uni(litProg, "uExag"), 1);
  gl.uniform3f(uni(litProg, "uColorA"), 0.106, 0.208, 0.145);
  gl.uniform3f(uni(litProg, "uColorB"), 0.180, 0.310, 0.196);
  attr(litProg, "aPos", bufTreePos, 3);
  attr(litProg, "aNormal", bufTreeNorm, 3);
  attr(litProg, "aShade", bufTreeShade, 1);
  gl.drawArrays(gl.TRIANGLES, 0, trees.count);

  if (mode === "boat") {
    // The boat is 1.8 m and everything else here is metres, so it must not be
    // exaggerated with the terrain or it would tower over its own lake.
    gl.uniform1f(uni(litProg, "uExag"), 1);
    gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, new Float32Array(multiply(hullMatrix(hull), multiply(view, projM))));
    attr(litProg, "aPos", bufHullPos, 3);
    attr(litProg, "aNormal", bufHullNorm, 3);
    attr(litProg, "aShade", bufHullShade, 1);
    for (const part of boatMesh.parts) {
      const c = BOAT_COLORS[part.color];
      gl.uniform3f(uni(litProg, "uColorA"), c[0][0], c[0][1], c[0][2]);
      gl.uniform3f(uni(litProg, "uColorB"), c[1][0], c[1][1], c[1][2]);
      gl.drawArrays(gl.TRIANGLES, part.start, part.count);
    }
    gl.uniformMatrix4fv(uni(litProg, "uMVP"), false, mvp);
    gl.uniform1f(uni(litProg, "uExag"), exag);
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
    gl.drawArrays(gl.LINES, 0, rocks.stemCount);
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
  // Swell is metres of displacement, so it must not be exaggerated along with
  // the bottom -- at 8x it would tower over the shallows it is meant to skim.
  gl.uniform1f(uni(waterProg, "uSwell"), mode === "boat" ? 1 : 0);
  gl.uniform1f(uni(waterProg, "uWave"), opts.wave);
  attr(waterProg, "aPos", bufWater, 3);
  attr(waterProg, "aDepth", bufWaterDep, 1);
  gl.drawArrays(gl.TRIANGLES, 0, water.count);

  // Fine patch on top, only where it is looked at closely. Drawn after the
  // coarse surface so it wins wherever the two overlap.
  if (mode === "boat") {
    const [px, pz] = updatePatch(eye[0], eye[2]);
    gl.useProgram(patchProg);
    gl.uniformMatrix4fv(uni(patchProg, "uMVP"), false, mvp);
    gl.uniform1f(uni(patchProg, "uTime"), clock);
    gl.uniform1f(uni(patchProg, "uAlpha"), 0.72);
    gl.uniform2f(uni(patchProg, "uCentre"), px, pz);
    gl.uniform1f(uni(patchProg, "uHalf"), patch.half);
    gl.uniform1f(uni(patchProg, "uWave"), opts.wave);
    attr(patchProg, "aPos", bufPatchPos, 3);
    attr(patchProg, "aDepth", bufPatchDep, 1);
    gl.drawArrays(gl.TRIANGLES, 0, patch.count);
  }

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

  // How far the nearest 1954 sounding is from where the boat actually is. The
  // depth above is quoted to the foot, which is a confidence the surface has
  // not earned everywhere: the survey ran twelve lines and between them the
  // number is interpolation. This is the qualifier on that number, and it is
  // the one thing worth knowing underway -- "18 ft" a metre off a transect and
  // "18 ft" 600 m from anything are not the same claim.
  const reach = grid.reachXY(boat.x + cx, -boat.z + cz);
  const rEl = el("hudReach");
  if (reach == null) {
    rEl.textContent = "--";
    rEl.style.color = "";
  } else {
    rEl.textContent = reach >= 1000 ? `${(reach / 1000).toFixed(1)} km` : `${Math.round(reach)} m`;
    // Amber past the point where the surface is mostly interpolation rather
    // than measurement. Not red: this is a caveat on the depth, not a hazard.
    rEl.style.color = reach > 250 ? "#ffc043" : "";
  }

  el("hudSpeed").textContent = (Math.abs(boat.speed) * 1.94384).toFixed(1);
  const near = nearestHazard(boat.x, boat.z);
  el("hudNear").textContent = near ? `${Math.round(near.d)} m` : "--";
  el("hudNear").style.color = near && near.d < 60 ? "#ff6a5a" : "";
}

function updateStats() {
  const m = Math.round(grid.gridM * opts.block);
  // The hazard count says what it is missing. This view draws the same
  // evidenced set the map does, but a rock needs a surveyed bottom to stand on
  // and 1954 did not sound everywhere -- so the number here is legitimately
  // smaller than the map's, and saying why beats two numbers that disagree.
  const missing = rocks.offSurvey
    ? ` (${rocks.offSurvey} more outside the 1954 survey)` : "";
  el("stats").textContent =
    `${(bottom.count / 3).toLocaleString()} triangles - ${m} m voxels, ` +
    `${opts.terrace} ft steps - ${rocks.list.length} hazards${missing} - ` +
    `${(trees.count / 12).toLocaleString()} trees`;
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
    // Drag looks around; steering is A/D. The offset is clamped so you cannot
    // end up facing astern with no way to tell which way the boat is going.
    boat.look = clamp(boat.look + dx * 0.005, -2.2, 2.2);
    boat.pitch = clamp(boat.pitch - dy * 0.004, -0.6, 0.5);
    boat.looking = true;
  }
});

function release(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = 0;
  if (pointers.size === 0) {
    last = null;
    boat.looking = false;
  }
}
canvas.addEventListener("pointerup", release);
canvas.addEventListener("pointercancel", release);

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const f = e.deltaY > 0 ? 1.12 : 0.89;
  if (mode === "orbit") orbit.dist = clamp(orbit.dist * f, 400, 40000);
  else if (mode === "fly") fly.speed = clamp(fly.speed / f, 50, 9000);
  else if (mode === "boat" && boat.chase) boat.dist = clamp(boat.dist * f, CHASE.min, CHASE.max);
}, { passive: false });

window.addEventListener("keydown", (e) => {
  // The keys are bound to the window, and the window now also holds a map and a
  // form with a lake code in it. WASD typed into a text field should not steer
  // the boat, and space should not be swallowed on a tab this view is not on.
  if (!onScreen()) return;
  if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  keys.add(k === "shift" ? "shift" : k);
  if (k === "v" && mode === "boat") { boat.chase = !boat.chase; syncChaseBtn(); }
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
  // Boat mode pins exaggeration to 1 -- at eye level a 30x bottom would put every
  // shallow above your head. The slider still moves, so say it does not apply
  // rather than leave it reading 30x while nothing happens.
  el("exagRow").classList.toggle("na", m === "boat");
  el("valExag").textContent = m === "boat" ? "1x here" : `${opts.exag}x`;
  el("swayRow").style.display = m === "boat" ? "flex" : "none";
  el("spawnRow").style.display = m === "orbit" ? "none" : "flex";
  el("hint").textContent =
    m === "orbit"
      ? "Drag to orbit - pinch or scroll to zoom"
      : m === "fly"
      ? "Drag to look - W/A/S/D to move, Space/Shift for up/down, scroll for speed"
      : "W to throttle up, A/D to steer, drag to look - scroll to pull the camera back";
  if (m === "boat" && depthAtWorld(boat.x, boat.z) == null) spawnAt("neoc");
}
for (const m of MODES) el(`mode_${m}`).onclick = () => setMode(m);
for (const key of Object.keys(SPAWNS)) {
  const b = el(`spawn_${key}`);
  if (b) b.onclick = () => spawnAt(key);
}

el("sldExag").addEventListener("input", (e) => {
  if (mode === "boat") return;
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
function queueRebuild() {
  clearTimeout(terraceTimer);
  terraceTimer = setTimeout(rebuildBottom, 140);
}

el("sldTerrace").addEventListener("input", (e) => {
  opts.terrace = +e.target.value;
  el("valTerrace").textContent = `${opts.terrace} ft`;
  queueRebuild();
});

el("sldWave").addEventListener("input", (e) => {
  opts.wave = +e.target.value / 10;
  el("valWave").textContent = opts.wave === 0 ? "flat" : `${(opts.wave * 0.55).toFixed(1)} m`;
});

el("sldBlock").addEventListener("input", (e) => {
  opts.block = +e.target.value;
  el("valBlock").textContent = `${Math.round(grid.gridM * opts.block)} m`;
  queueRebuild();
});

function syncChaseBtn() {
  const b = el("btnChase");
  b.textContent = boat.chase ? "Third person" : "First person";
  b.classList.toggle("on", boat.chase);
}
el("btnChase").onclick = () => { boat.chase = !boat.chase; syncChaseBtn(); };

el("btnSway").onclick = () => {
  opts.sway = !opts.sway;
  el("btnSway").classList.toggle("on", opts.sway);
};

// Same drawer the map's Layers button opens, in the same place, with the same
// grip on top -- one gesture to learn instead of two.
function setGear(open) {
  el("sliders").classList.toggle("open", open);
  el("btnGear").classList.toggle("on", open);
}
el("btnGear").onclick = () => setGear(!el("sliders").classList.contains("open"));
el("btnGearClose").onclick = () => setGear(false);

el("btnReset").onclick = () => {
  orbit.yaw = 0;
  orbit.pitch = 0.62;
  orbit.dist = 7000;
  fly.yaw = 0;
  fly.pitch = -0.35;
  fly.speed = 1400;
  boat.look = 0;
  boat.pitch = -0.06;
  boat.speed = 0;
  boat.dist = CHASE.dist;
  boat.chase = true;
  syncChaseBtn();
  if (mode !== "boat") spawnAt("neoc");
};

updateStats();
el("valExag").textContent = `${opts.exag}x`;
syncChaseBtn();
el("valTerrace").textContent = `${opts.terrace} ft`;
el("valBlock").textContent = `${Math.round(grid.gridM * opts.block)} m`;
el("valWave").textContent = `${(opts.wave * 0.55).toFixed(1)} m`;
spawnAt("neoc");

// ?mode= deep-links straight into a mode, which is how this gets checked
// without a human clicking, and is handy for a bookmark.
const params = new URLSearchParams(location.search);
const wanted = params.get("mode");
setMode(MODES.includes(wanted) ? wanted : "orbit");
const wantSpawn = params.get("at");
if (wantSpawn && SPAWNS[wantSpawn]) spawnAt(wantSpawn);

// Setup survived. Hand errors back to the app's own reporting from here.
window.removeEventListener("error", onSetupError);

requestAnimationFrame(draw);
