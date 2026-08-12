// Geometry builders for the 3D viewer. Split out of viewer3d.js because that
// file was doing shaders, camera, input and mesh generation at once, and the
// mesh generation is the part that changes whenever the terrace setting moves.
//
// Everything here works in metres with Y up, +north mapped to -Z, and the grid
// centred on the origin. Depth is feet, positive down, because that is the unit
// the survey is in and converting it early would only make the numbers stop
// matching the source.

import { drawnAt, DEFAULT_DETAIL } from "./evidence.js";

export const FT_PER_M = 3.28084;

// --- waves -----------------------------------------------------------------

// Gerstner wave train. Sine waves give rounded humps; Gerstner displaces points
// horizontally as well as vertically, which bunches them at the crests and
// stretches them in the troughs -- sharp peaks, broad flat troughs, the shape
// water actually makes.
//
// ONE definition, consumed twice: the GLSL is generated from it, and the same
// numbers are evaluated in JS to float the boat. A hand-kept second copy would
// drift and the hull would ride water that is not the water being drawn.
//
// dir is a unit direction, len the wavelength in metres, amp the amplitude in
// metres, steep the Gerstner sharpness (0 = sine, 1 = cusped peak). Sized for a
// lake in a breeze, not the North Atlantic: 0.55 m from trough to crest total.
export const WAVES = [
  { dir: [0.92, 0.39], len: 27, amp: 0.19, steep: 0.72, speed: 1.0 },
  { dir: [0.62, -0.78], len: 17, amp: 0.13, steep: 0.68, speed: 1.18 },
  { dir: [-0.34, 0.94], len: 11, amp: 0.085, steep: 0.6, speed: 1.35 },
  { dir: [0.99, -0.14], len: 7.5, amp: 0.05, steep: 0.5, speed: 1.6 },
];

// GLSL that displaces a point and accumulates a normal. Emitted rather than
// written so it cannot disagree with the JS below.
export function gerstnerGLSL() {
  let body = "";
  for (const w of WAVES) {
    const k = (2 * Math.PI) / w.len;
    const c = Math.sqrt(9.81 / k) * w.speed;
    const [dx, dy] = w.dir;
    body += `
  {
    float k = ${k.toFixed(6)};
    vec2 d = vec2(${dx.toFixed(4)}, ${dy.toFixed(4)});
    float f = k * (dot(d, p) - ${c.toFixed(4)} * t);
    float a = ${w.amp.toFixed(4)} * fade;
    float q = ${w.steep.toFixed(4)};
    disp.xz += q * a * d * cos(f);
    disp.y += a * sin(f);
    // Analytic normal for this component, from the derivative of the surface.
    nrm.x -= d.x * k * a * cos(f);
    nrm.z -= d.y * k * a * cos(f);
    nrm.y -= q * k * a * sin(f);
    crest += a * sin(f);
  }`;
  }
  return `
// disp is the displacement, nrm accumulates the surface normal, crest is the
// signed height used for foam.
void gerstner(vec2 p, float t, float fade, out vec3 disp, out vec3 nrm, out float crest) {
  disp = vec3(0.0);
  nrm = vec3(0.0, 1.0, 0.0);
  crest = 0.0;
  ${body}
  nrm = normalize(nrm);
}`;
}

// The same evaluation in JS, for floating the boat on the water being drawn.
// Returns height plus the surface slope, which is what pitches and rolls a hull.
export function waveAt(x, z, t, fade = 1) {
  let y = 0;
  let dx = 0;
  let dz = 0;
  for (const w of WAVES) {
    const k = (2 * Math.PI) / w.len;
    const c = Math.sqrt(9.81 / k) * w.speed;
    const f = k * (w.dir[0] * x + w.dir[1] * z - c * t);
    const a = w.amp * fade;
    y += a * Math.sin(f);
    dx += w.dir[0] * k * a * Math.cos(f);
    dz += w.dir[1] * k * a * Math.cos(f);
  }
  return { y, dx, dz };
}

// Camera-following water patch. The lake-wide water quads are 75 m across and
// cannot carry a 10 m wave, so the detail is put where it is actually looked
// at: a fine grid that follows the boat, with the coarse surface behind it.
// Positions are LOCAL to the patch; the shader adds the snapped centre, which
// is what stops the mesh swimming under the waves as you move.
export function buildWaterPatch(n, spacing) {
  const pos = [];
  const half = (n * spacing) / 2;
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const x0 = i * spacing - half;
      const z0 = j * spacing - half;
      const x1 = x0 + spacing;
      const z1 = z0 + spacing;
      pos.push(x0, 0, z0, x1, 0, z0, x1, 0, z1, x0, 0, z0, x1, 0, z1, x0, 0, z1);
    }
  }
  return { pos: new Float32Array(pos), count: pos.length / 3, spacing, half };
}

// Deterministic hash so the scattered decoration (tree placement, rock shape)
// is identical on every load. A different lake every refresh would make it
// impossible to tell a rendering bug from a reroll.
function hash(n) {
  let x = Math.sin(n * 127.1) * 43758.5453;
  return x - Math.floor(x);
}

// --- terraced bottom -------------------------------------------------------

// The bottom as a milled contour model: every cell is a flat shelf at its
// terraced depth, and adjacent shelves at different levels are joined by a
// vertical wall. Quantising in the shader instead produced a ramp between
// levels -- the steps were there in the colour but the surface still sloped,
// which is exactly the thing a milled map does not do.
//
// Rebuilt on the CPU when the terrace changes, because the risers are real
// geometry and cannot be faked from a per-vertex depth.
export function buildSteppedMesh(grid, cx, cz, terraceFt, blockCells = 1) {
  const pos = [];
  const norm = [];
  const dep = [];
  const step = Math.max(1, terraceFt);
  const bs = Math.max(1, Math.round(blockCells));

  // Blocks of source cells collapsed into one voxel. At the raw 25 m grid the
  // terrace edges are a cell wide, which reads as noise rather than as steps --
  // the model wants blocks you can see, not the finest the data supports.
  //
  // The block takes the MINIMUM depth it covers, not the mean. A voxel that
  // averages a 2 ft rock shelf with the 30 ft hole beside it reports 16 ft of
  // water over a place with 2, and coarsening the display must never invent
  // depth that is not there.
  const nx = Math.ceil((grid.nx - 1) / bs);
  const ny = Math.ceil((grid.ny - 1) / bs);

  const lvl = new Float32Array(nx * ny).fill(NaN);
  for (let row = 0; row < ny; row++) {
    for (let col = 0; col < nx; col++) {
      let min = Infinity;
      for (let r = row * bs; r < Math.min((row + 1) * bs, grid.ny); r++) {
        for (let c = col * bs; c < Math.min((col + 1) * bs, grid.nx); c++) {
          const ft = grid.at(c, r);
          if (ft !== null && ft < min) min = ft;
        }
      }
      // A block is water only if some of it is. A block that is mostly land but
      // touches the lake still has to carry its shallow edge.
      if (min !== Infinity) lvl[row * nx + col] = Math.floor(min / step) * step;
    }
  }
  const at = (col, row) =>
    col < 0 || row < 0 || col >= nx || row >= ny ? NaN : lvl[row * nx + col];

  // Block footprint in world metres, from the source grid spacing.
  const bx = grid.stepX * bs;
  const bz = grid.stepY * bs;
  const originX = grid.worldX(0) - cx;
  const originZ = -(grid.worldY(0) - cz);

  const yOf = (ft) => -ft / FT_PER_M;

  const tri = (ax, ay, az, bx, by, bz, ccx, ccy, ccz, n, d) => {
    pos.push(ax, ay, az, bx, by, bz, ccx, ccy, ccz);
    norm.push(n[0], n[1], n[2], n[0], n[1], n[2], n[0], n[1], n[2]);
    dep.push(d, d, d);
  };

  const UP = [0, 1, 0];

  for (let row = 0; row < ny; row++) {
    for (let col = 0; col < nx; col++) {
      const d = at(col, row);
      if (Number.isNaN(d)) continue;

      // Block footprint. World Y from the grid runs north, and north is -Z.
      const x0 = originX + col * bx;
      const x1 = x0 + bx;
      const z0 = originZ - row * bz;
      const z1 = z0 - bz;
      const y = yOf(d);

      // Shelf top.
      tri(x0, y, z0, x1, y, z0, x1, y, z1, UP, d);
      tri(x0, y, z0, x1, y, z1, x0, y, z1, UP, d);

      // Risers. Only emitted toward the DEEPER neighbour, so each wall is built
      // once rather than twice from both sides. A land neighbour counts as
      // depth 0, which gives the shoreline a clean cut face.
      const sides = [
        [at(col + 1, row), x1, z0, x1, z1, [1, 0, 0]],
        [at(col - 1, row), x0, z1, x0, z0, [-1, 0, 0]],
        [at(col, row + 1), x0, z1, x1, z1, [0, 0, -1]],
        [at(col, row - 1), x1, z0, x0, z0, [0, 0, 1]],
      ];
      for (const [nd, ax, az, bx, bz, n] of sides) {
        const neighbour = Number.isNaN(nd) ? 0 : nd;
        if (neighbour <= d) continue;
        const yb = yOf(neighbour);
        // Wall faces the deeper side, so its normal points away from this cell.
        const wn = [-n[0], 0, -n[2]];
        tri(ax, y, az, bx, y, bz, bx, yb, bz, wn, d);
        tri(ax, y, az, bx, yb, bz, ax, yb, az, wn, d);
      }
    }
  }

  return {
    pos: new Float32Array(pos),
    norm: new Float32Array(norm),
    dep: new Float32Array(dep),
    count: pos.length / 3,
  };
}

// --- land, water, shoreline ------------------------------------------------

// Land is every cell the lake mask excludes, plus a skirt to the horizon.
// Water is the complement, and gets its own buffer so the surface can be drawn
// translucent over the bottom.
export function buildFlatCells(grid, cx, cz, wantWater, blockCells = 1) {
  const v = [];
  const dep = [];
  const bs = Math.max(1, Math.round(blockCells));
  const quad = (ax, az, bx, bz, d) => {
    v.push(ax, 0, az, bx, 0, az, bx, 0, bz, ax, 0, az, bx, 0, bz, ax, 0, bz);
    for (let i = 0; i < 6; i++) dep.push(d);
  };

  const nx = Math.ceil((grid.nx - 1) / bs);
  const ny = Math.ceil((grid.ny - 1) / bs);
  const bx = grid.stepX * bs;
  const bz = grid.stepY * bs;
  const originX = grid.worldX(0) - cx;
  const originZ = -(grid.worldY(0) - cz);

  for (let row = 0; row < ny; row++) {
    for (let col = 0; col < nx; col++) {
      // Same minimum rule as the bottom, so the water surface and the shelf
      // under it agree about how deep a block is. They have to: the swell
      // amplitude is derived from this, and if it disagreed the surface would
      // dip through the bottom in exactly the shallows where it must not.
      let min = Infinity;
      for (let r = row * bs; r < Math.min((row + 1) * bs, grid.ny); r++) {
        for (let c = col * bs; c < Math.min((col + 1) * bs, grid.nx); c++) {
          const ft = grid.at(c, r);
          if (ft !== null && ft < min) min = ft;
        }
      }
      const wet = min !== Infinity;
      if (wet !== wantWater) continue;
      const x0 = originX + col * bx;
      const z0 = originZ - row * bz;
      quad(x0, z0, x0 + bx, z0 - bz, wet ? min : 0);
    }
  }

  if (!wantWater) {
    const x0 = grid.worldX(0) - cx;
    const x1 = grid.worldX(grid.nx - 1) - cx;
    const z0 = -(grid.worldY(0) - cz);
    const z1 = -(grid.worldY(grid.ny - 1) - cz);
    const pad = 15000;
    quad(x0 - pad, z0 + pad, x1 + pad, z0, 0);
    quad(x0 - pad, z1, x1 + pad, z1 - pad, 0);
    quad(x0 - pad, z0, x0, z1, 0);
    quad(x1, z0, x1 + pad, z1, 0);
  }

  return { pos: new Float32Array(v), dep: new Float32Array(dep), count: v.length / 3 };
}

export function shoreRings(LAKE_GEO, proj) {
  const geom = LAKE_GEO.geometry;
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
  const rings = [];
  for (const poly of polys) {
    for (const ring of poly) rings.push(ring.map(([lon, lat]) => proj.fwd(lon, lat)));
  }
  return rings;
}

export function buildShore(rings, cx, cz) {
  const segs = [];
  for (const ring of rings) {
    for (let i = 0; i < ring.length - 1; i++) {
      segs.push(ring[i][0] - cx, 0, -(ring[i][1] - cz));
      segs.push(ring[i + 1][0] - cx, 0, -(ring[i + 1][1] - cz));
    }
  }
  return new Float32Array(segs);
}

// --- trees -----------------------------------------------------------------

// Conifers along the shore, as real (if crude) geometry rather than sprites.
// Sprites have to be turned to face the camera every frame, and a four-triangle
// cone costs less than that bookkeeping. They exist for scale: without anything
// of known height on the shoreline, boat mode is a coloured plane and a line,
// and there is no way to judge how far away the far shore is.
//
// Placement is deterministic and inland-checked against the depth grid, so no
// tree ends up standing in the lake.
export function buildTrees(grid, rings, cx, cz, spacing = 22) {
  const pos = [];
  const norm = [];
  const shade = [];

  const push = (ax, ay, az, bx, by, bz, ccx, ccy, ccz, n, s) => {
    pos.push(ax, ay, az, bx, by, bz, ccx, ccy, ccz);
    norm.push(n[0], n[1], n[2], n[0], n[1], n[2], n[0], n[1], n[2]);
    shade.push(s, s, s);
  };

  let seed = 0;
  for (const ring of rings) {
    // Walked by arc length across the whole ring, not per segment. The shoreline
    // polygon has vertices every few metres in places, so emitting one tree per
    // segment would put a forest on every headland and nothing on the straights.
    let nextAt = 0;
    let travelled = 0;

    for (let i = 0; i < ring.length - 1; i++) {
      const [ax, ay] = ring[i];
      const [bx, by] = ring[i + 1];
      const segLen = Math.hypot(bx - ax, by - ay);
      if (segLen < 1e-6) continue;

      // Outward normal of the segment, in grid space.
      const tx = (bx - ax) / segLen;
      const ty = (by - ay) / segLen;
      const px = -ty;
      const py = tx;

      while (nextAt < travelled + segLen) {
        const t = nextAt - travelled;
        nextAt += spacing;
        seed++;
        const f = t / segLen;
        const gx = ax + (bx - ax) * f;
        const gy = ay + (by - ay) * f;

        // Step off the shoreline to whichever side is dry. The ring winding is
        // not reliable across 74 islands, so ask the grid rather than assume.
        const off = 8 + hash(seed) * 26;
        let sx = gx + px * off;
        let sy = gy + py * off;
        if (grid.sampleXY(sx, sy) !== null) {
          sx = gx - px * off;
          sy = gy - py * off;
          if (grid.sampleXY(sx, sy) !== null) continue; // both sides wet: skip
        }

        const h = 9 + hash(seed * 3.7) * 11;
        const r = h * (0.16 + hash(seed * 5.1) * 0.07);
        const x = sx - cx;
        const z = -(sy - cz);
        const s = 0.72 + hash(seed * 9.3) * 0.5;

        // Four-sided cone. Flat-shaded per face so it catches light unevenly
        // and a hillside of them does not read as one green mass.
        for (let k = 0; k < 4; k++) {
          const a0 = (k / 4) * Math.PI * 2;
          const a1 = ((k + 1) / 4) * Math.PI * 2;
          const p0 = [x + Math.cos(a0) * r, 0, z + Math.sin(a0) * r];
          const p1 = [x + Math.cos(a1) * r, 0, z + Math.sin(a1) * r];
          const mid = (a0 + a1) / 2;
          push(
            p0[0], p0[1], p0[2],
            p1[0], p1[1], p1[2],
            x, h, z,
            [Math.cos(mid) * 0.7, 0.7, Math.sin(mid) * 0.7],
            s * (0.85 + 0.3 * hash(seed + k))
          );
        }
      }
      travelled += segLen;
    }
  }

  return {
    pos: new Float32Array(pos),
    norm: new Float32Array(norm),
    shade: new Float32Array(shade),
    count: pos.length / 3,
  };
}

// --- hazard rocks ----------------------------------------------------------

// Rocks as lumps of geometry rather than dots. A dot floating in 3D gives no
// sense of distance or size; a rock sitting on the bottom does.
//
// The two classes sit differently on purpose. A `rock` breaks the surface, so
// its top is above the waterline where you would actually see it. A `shoal`
// is submerged, so it sits on the bottom and its top stays below zero -- which
// is precisely why it also keeps a marker stem, because otherwise the dangerous
// class is the invisible one.
/* Shared triangle sink with real geometric normals.

   The old builders pushed a hand-guessed normal per facet -- `[cos*0.6, 0.72,
   sin*0.6]` on every rock face regardless of the face's actual orientation. That
   is why a field of them lit as one flat mass: the shading carried no shape
   information. Deriving the normal from the triangle costs nothing and is the
   single biggest visual difference here. */
function mesh() {
  const pos = [], norm = [], shade = [], base = [];
  let baseY = 0;                       // set by the caller before emitting a solid
  const tri = (a, b, c, s, n) => {
    if (!n) {
      const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
      const v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
      n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
      const l = Math.hypot(n[0], n[1], n[2]) || 1;
      n = [n[0] / l, n[1] / l, n[2] / l];
    }
    pos.push(...a, ...b, ...c);
    for (let i = 0; i < 3; i++) norm.push(n[0], n[1], n[2]);
    shade.push(s, s, s);
    base.push(baseY, baseY, baseY);
  };
  const quad = (a, b, c, d, s) => { tri(a, b, c, s); tri(a, c, d, s); };
  return {
    pos, norm, shade, base, tri, quad,
    mark: () => pos.length / 3,
    setBase: (y) => { baseY = y; },
  };
}

/* One boulder: a dome deformed on three axes, sitting on the bottom.

   Cones were the problem. A cone has a single apex and straight generators, so
   six of them side by side read as tent pegs no matter what colour they are.
   Deforming a dome per-vertex with the same hash the trees use gives lumpy,
   asymmetric rock, and because the deformation is 3D the silhouette changes with
   viewing angle the way a real boulder does.

   Facets are budgeted by radius -- there are 1,673 of these in the lake, and a
   3 m rock does not need the same tessellation as a 22 m ledge. */
function boulder(M, x, baseY, z, r, h, seed, shadeBase) {
  M.setBase(baseY);
  const lon = r > 12 ? 10 : r > 6 ? 8 : 6;
  const lat = r > 12 ? 4 : 3;
  const ring = [];
  for (let i = 0; i <= lat; i++) {
    const t = i / lat;
    // Dome profile, flattened near the top so it does not come to a point.
    const rr = r * Math.cos(t * Math.PI * 0.42);
    const yy = baseY + h * Math.sin(t * Math.PI * 0.5);
    const row = [];
    for (let j = 0; j < lon; j++) {
      const a = (j / lon) * Math.PI * 2;
      // Per-vertex lumpiness. Anisotropic on purpose: rocks here are glacial,
      // so they are longer one way than the other.
      const d = 0.68 + hash(seed * 3.1 + i * 5.7 + j * 11.3) * 0.62;
      const squash = 0.75 + hash(seed * 1.7) * 0.5;
      row.push([
        x + Math.cos(a) * rr * d,
        yy + (hash(seed * 9.4 + i * 2.3 + j * 4.1) - 0.5) * h * 0.22,
        z + Math.sin(a) * rr * d * squash,
      ]);
    }
    ring.push(row);
  }
  for (let i = 0; i < lat; i++) {
    for (let j = 0; j < lon; j++) {
      const k = (j + 1) % lon;
      // Shade jitter per facet so the surface breaks up under a single light.
      const s = shadeBase + hash(seed * 6.2 + i * 3.9 + j * 7.1) * 0.55;
      M.quad(ring[i][j], ring[i][k], ring[i + 1][k], ring[i + 1][j], s);
    }
  }
  // Cap.
  const top = ring[lat];
  const apex = [x, baseY + h * 1.02, z];
  for (let j = 0; j < lon; j++) {
    M.tri(top[j], top[(j + 1) % lon], apex, shadeBase + hash(seed * 8.8 + j) * 0.55);
  }
}

export function buildHazardRocks(grid, ROCK_GEO, proj, cx, cz) {
  // Two meshes, drawn separately, because the two classes mean different things
  // and must not blend into each other. Within each, shade varies per facet for
  // tone -- that is what the lit shader's A/B mix is for.
  const breaking = mesh();
  const shoal = mesh();
  const stems = [];
  const list = [];
  let evidenced = 0;   // marks the map would draw
  let offSurvey = 0;   // of those, ones with no surveyed bottom to stand on

  let seed = 0;
  for (const f of ROCK_GEO.features) {
    const p = f.properties;
    // The same evidence rule the map draws by, not the old distance-from-shore
    // one. Filtering on `offshore` here put 1,673 rocks in this water while the
    // map drew 1,359, and most of the difference was unverified candidates --
    // so the tab that renders them as solid objects on the bottom, the most
    // convincing view in the whole app, was the one showing the least
    // evidenced set.
    if (!drawnAt(p, DEFAULT_DETAIL)) continue;
    evidenced++;
    const [wx, wy] = proj.fwd(p.lon, p.lat);
    const ft = grid.sampleXY(wx, wy);
    // No surveyed bottom here, so there is nothing to sit the rock on. Counted
    // rather than silently dropped: this view showing fewer hazards than the
    // map is a fact about the 1954 survey's coverage, and the header says so
    // instead of leaving two numbers to contradict each other.
    if (ft === null) { offSurvey++; continue; }

    seed++;
    const x = wx - cx;
    const z = -(wy - cz);
    const submerged = p.class === "shoal" || p.class === "drawdown";
    const M = submerged ? shoal : breaking;

    // Footprint radius from the detected area, clamped: a 96,700 m2 "island"
    // would otherwise be a 175 m boulder, and a sub-pixel detection would be
    // invisible.
    const r = Math.max(3, Math.min(22, Math.sqrt((p.area_m2 || 20) / Math.PI)));
    const bottomY = -ft / FT_PER_M;

    // A breaking rock pokes above the surface. A shoal rises off the bottom but
    // stops short of it -- never drawn poking through, because the whole point
    // of the class is that it does not.
    const height = submerged
      ? Math.min(r * 1.1, Math.max(0.5, -bottomY - 0.6))
      : -bottomY + 0.5 + r * 0.25;

    boulder(M, x, bottomY, z, r, height, seed, 0.6);

    // Big features get satellites. Real ledges are groups, not single stones,
    // and the clustering is most of what makes a shoal read as a shoal.
    if (r > 7) {
      const n = 2 + Math.floor(hash(seed * 12.7) * 2);
      for (let k = 0; k < n; k++) {
        const a = hash(seed * 4.3 + k * 9.1) * Math.PI * 2;
        const d = r * (0.75 + hash(seed * 5.9 + k) * 0.75);
        const rr = r * (0.22 + hash(seed * 7.7 + k) * 0.3);
        const hh = submerged
          ? Math.min(rr * 1.2, Math.max(0.4, -bottomY - 1.2))
          : height * (0.35 + hash(seed * 2.9 + k) * 0.4);
        boulder(M, x + Math.cos(a) * d, bottomY, z + Math.sin(a) * d,
                rr, hh, seed * 31 + k, 0.6);
      }
    }

    if (submerged) stems.push(x, bottomY + height, z, x, 0, z);
    list.push({ x, z, ft, cls: p.class, submerged });
  }

  const pack = (M) => ({
    pos: new Float32Array(M.pos),
    norm: new Float32Array(M.norm),
    shade: new Float32Array(M.shade),
    base: new Float32Array(M.base),
    count: M.pos.length / 3,
  });

  return {
    breaking: pack(breaking),
    shoal: pack(shoal),
    stems: new Float32Array(stems),
    stemCount: stems.length / 3,
    list,
    evidenced,
    offSurvey,
  };
}

/* The survey boat, at its real 1.8 m, bow toward -Z.

   Replaces the first-person cockpit wedge. That wedge only worked from inside
   it -- every part sat forward of the eye by construction, so there was nothing
   to look AT from behind. This is a whole vessel in world space, which is what
   a chase camera needs.

   Dimensions come from docs/solar-survey-boat-spec.md and are the same numbers
   docs/boat-viewer.html renders, so the thing you drive here is the thing that
   would get built. Parts are returned as draw ranges rather than one blob so
   the orange hulls, grey deck and dark solar panel can each keep their colour
   through a shader that only carries two per call. */
export function buildSurveyBoat() {
  const M = mesh();
  const parts = [];
  const LOA = 1.80, BEAM = 0.90, DRAFT = 0.12;
  const HULL_W = 0.24, HULL_H = 0.26;
  const SEP = BEAM - HULL_W;
  const deckY = HULL_H - DRAFT + 0.07;

  const boxAt = (cxx, cyy, czz, w, h, d, s) => {
    const X = w / 2, Y = h / 2, Z = d / 2;
    const v = (a, b, c) => [cxx + a * X, cyy + b * Y, czz + c * Z];
    M.quad(v(-1,1,-1), v(1,1,-1), v(1,1,1), v(-1,1,1), s);
    M.quad(v(-1,-1,1), v(1,-1,1), v(1,-1,-1), v(-1,-1,-1), s);
    M.quad(v(-1,-1,-1), v(1,-1,-1), v(1,1,-1), v(-1,1,-1), s);
    M.quad(v(1,-1,1), v(-1,-1,1), v(-1,1,1), v(1,1,1), s);
    M.quad(v(-1,-1,1), v(-1,-1,-1), v(-1,1,-1), v(-1,1,1), s);
    M.quad(v(1,-1,-1), v(1,-1,1), v(1,1,1), v(1,1,-1), s);
  };

  // Two lofted hulls. The tunnel between them is the whole reason the boat is a
  // catamaran -- it is where the transducer hangs, in water the props have not
  // stirred -- so it has to be visibly open.
  const start = M.mark();
  const hullTop = deckY - 0.07;
  for (const side of [-1, 1]) {
    const NS = 16, NR = 7;
    const taper = (t) => Math.min(1, Math.pow(t / 0.42, 0.62))
      * (1 - 0.18 * Math.pow(Math.max(0, t - 0.82) / 0.18, 2));
    const ring = (t) => {
      const k = taper(t), out = [];
      for (let j = 0; j <= NR; j++) {
        const a = Math.PI * j / NR;
        out.push([
          side * SEP / 2 + (HULL_W / 2) * Math.cos(a) * k,
          hullTop - (HULL_H) * Math.sin(a) * (0.55 + 0.45 * k),
          (t - 0.5) * LOA,
        ]);
      }
      return out;
    };
    let prev = ring(0);
    for (let i = 1; i <= NS; i++) {
      const cur = ring(i / NS);
      for (let j = 0; j < NR; j++) M.quad(prev[j], cur[j], cur[j + 1], prev[j + 1], 0.5);
      M.quad([prev[0][0], hullTop, prev[0][2]],
             [cur[0][0], hullTop, cur[0][2]],
             [cur[NR][0], hullTop, cur[NR][2]],
             [prev[NR][0], hullTop, prev[NR][2]], 1.4);
      prev = cur;
    }
  }
  parts.push({ start, count: M.mark() - start, color: "hull" });

  // Bridgedeck spans only the tunnel so both hulls stay readable from outside.
  const s2 = M.mark();
  boxAt(0, deckY, 0, SEP + HULL_W * 0.55, 0.035, LOA * 0.80, 0.4);
  boxAt(0.0, deckY + 0.10, -LOA * 0.14, 0.20, 0.14, 0.26, 1.5);   // avionics pod
  boxAt(0.16, deckY + 0.08, LOA * 0.20, 0.09, 0.07, 0.10, 0.4);   // beacon
  boxAt(0, deckY + 0.36, LOA * 0.30, 0.02, 0.62, 0.02, 1.5);      // mast
  parts.push({ start: s2, count: M.mark() - s2, color: "deck" });

  // Solar deck.
  const s3 = M.mark();
  boxAt(0, deckY + 0.03, 0, (SEP + HULL_W * 0.55) * 0.9, 0.014, LOA * 0.72, 0.4);
  parts.push({ start: s3, count: M.mark() - s3, color: "solar" });

  // Transducer on its strut, hanging in the tunnel below the waterline.
  const s4 = M.mark();
  boxAt(0, -DRAFT - 0.09, -0.10, 0.05, 0.40, 0.09, 0.4);
  boxAt(0, -DRAFT - 0.30, -0.10, 0.10, 0.08, 0.13, 1.5);
  parts.push({ start: s4, count: M.mark() - s4, color: "sonar" });

  return {
    pos: new Float32Array(M.pos),
    norm: new Float32Array(M.norm),
    shade: new Float32Array(M.shade),
    count: M.pos.length / 3,
    parts,
  };
}
