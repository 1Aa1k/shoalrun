// Geometry builders for the 3D viewer. Split out of viewer3d.js because that
// file was doing shaders, camera, input and mesh generation at once, and the
// mesh generation is the part that changes whenever the terrace setting moves.
//
// Everything here works in metres with Y up, +north mapped to -Z, and the grid
// centred on the origin. Depth is feet, positive down, because that is the unit
// the survey is in and converting it early would only make the numbers stop
// matching the source.

export const FT_PER_M = 3.28084;

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
export function buildHazardRocks(grid, ROCK_GEO, proj, cx, cz) {
  const pos = [];
  const norm = [];
  const kind = [];
  const stems = [];
  const list = [];

  let seed = 0;
  for (const f of ROCK_GEO.features) {
    const p = f.properties;
    if (p.offshore === false) continue;
    const [wx, wy] = proj.fwd(p.lon, p.lat);
    const ft = grid.sampleXY(wx, wy);
    if (ft === null) continue;

    seed++;
    const x = wx - cx;
    const z = -(wy - cz);
    const submerged = p.class === "shoal" || p.class === "drawdown";

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
    const topY = bottomY + height;

    const sides = 6;
    for (let k = 0; k < sides; k++) {
      const a0 = (k / sides) * Math.PI * 2;
      const a1 = ((k + 1) / sides) * Math.PI * 2;
      // Irregular radius per facet so they do not read as a field of cones.
      const r0 = r * (0.62 + hash(seed * 7.3 + k) * 0.55);
      const r1 = r * (0.62 + hash(seed * 7.3 + k + 1) * 0.55);
      const p0 = [x + Math.cos(a0) * r0, bottomY, z + Math.sin(a0) * r0];
      const p1 = [x + Math.cos(a1) * r1, bottomY, z + Math.sin(a1) * r1];
      const apex = [
        x + (hash(seed * 2.1) - 0.5) * r * 0.4,
        topY,
        z + (hash(seed * 4.9) - 0.5) * r * 0.4,
      ];
      const mid = (a0 + a1) / 2;
      const n = [Math.cos(mid) * 0.6, 0.72, Math.sin(mid) * 0.6];
      pos.push(...p0, ...p1, ...apex);
      for (let j = 0; j < 3; j++) norm.push(n[0], n[1], n[2]);
      // 1 = submerged shoal, 0 = breaks the surface. Drives colour.
      for (let j = 0; j < 3; j++) kind.push(submerged ? 1 : 0);
    }

    if (submerged) stems.push(x, topY, z, x, 0, z);
    list.push({ x, z, ft, cls: p.class, submerged });
  }

  return {
    pos: new Float32Array(pos),
    norm: new Float32Array(norm),
    kind: new Float32Array(kind),
    stems: new Float32Array(stems),
    count: pos.length / 3,
    list,
  };
}

// --- boat ------------------------------------------------------------------

// A hull drawn ahead of and below the camera in boat mode. Local coords: +X is
// starboard, -Z is forward, Y up, origin at the helm. Crude on purpose -- it is
// there so the view reads as "from a boat" rather than "from a hovering eye",
// and any more detail would just be in the way.
export function buildBoatHull() {
  const pos = [];
  const norm = [];
  const shade = [];

  const tri = (a, b, c, n, s) => {
    pos.push(...a, ...b, ...c);
    for (let i = 0; i < 3; i++) norm.push(...n);
    shade.push(s, s, s);
  };

  // Every part of the hull sits FORWARD of the eye and below it. The first
  // version put the transom behind the camera at +Z, so the deck triangle ran
  // off past the near plane and filled the screen with white.
  const bowZ = -5.4;
  const sternZ = -0.9;
  const beam = 0.85;
  const deckY = -1.25;
  const keelY = -1.75;

  // Foredeck: bow point back to the cockpit coaming.
  tri([0, deckY, bowZ], [beam, deckY, sternZ], [-beam, deckY, sternZ], [0, 1, 0], 1.0);
  // Topsides.
  tri([0, deckY, bowZ], [beam, keelY, sternZ], [beam, deckY, sternZ], [0.9, 0.25, 0], 0.72);
  tri([0, deckY, bowZ], [-beam, deckY, sternZ], [-beam, keelY, sternZ], [-0.9, 0.25, 0], 0.58);
  // Stem, so the bow has some depth to it rather than being a flat wedge.
  tri([0, deckY, bowZ], [-beam, keelY, sternZ], [beam, keelY, sternZ], [0, -0.3, -0.9], 0.66);
  // Coaming across the front of the cockpit -- the near edge you look over.
  tri([beam, deckY, sternZ], [-beam, deckY, sternZ], [-beam, deckY + 0.3, sternZ], [0, 0.2, 1], 1.2);
  tri([beam, deckY, sternZ], [-beam, deckY + 0.3, sternZ], [beam, deckY + 0.3, sternZ], [0, 0.2, 1], 1.2);

  return {
    pos: new Float32Array(pos),
    norm: new Float32Array(norm),
    shade: new Float32Array(shade),
    count: pos.length / 3,
  };
}
