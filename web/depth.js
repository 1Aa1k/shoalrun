// The depth surface, and everything derived from it at runtime.
//
// Contours used to be baked into the build at a fixed 10 ft interval. Shipping
// the grid instead means the interval is a slider, the shading and the lines
// come from the same numbers, and the 3D viewer is reading the same surface the
// map is drawing. It also made the payload smaller: the grid is 158 KB where
// the pre-cut 10 ft contours alone were 253 KB.
//
// Everything here is an INTERPOLATED 1954 survey. It is context for the hazard
// layer, not a substitute -- no rock appears in a depth contour.

const NODATA = 255;

export class DepthGrid {
  constructor(raw, proj) {
    this.nx = raw.nx;
    this.ny = raw.ny;
    this.lon0 = raw.lon0;
    this.lat0 = raw.lat0;
    this.dlon = raw.dlon;
    this.dlat = raw.dlat;
    this.maxFt = raw.max_ft;
    this.gridM = raw.grid_m;
    this.nodata = raw.nodata ?? NODATA;

    const bin = atob(raw.depths_b64);
    this.d = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) this.d[i] = bin.charCodeAt(i);

    if (this.d.length !== this.nx * this.ny) {
      throw new Error(`depth grid is ${this.d.length} bytes, expected ${this.nx * this.ny}`);
    }

    // Grid corners in projected metres. Row 0 is the south edge, so +row is
    // +north, which matches the projection's +y. Precomputed because the
    // renderer needs them on every frame.
    const [x0, y0] = proj.fwd(this.lon0, this.lat0);
    const [x1, y1] = proj.fwd(this.lon0 + this.dlon * (this.nx - 1), this.lat0 + this.dlat * (this.ny - 1));
    this.x0 = x0;
    this.y0 = y0;
    this.stepX = (x1 - x0) / (this.nx - 1);
    this.stepY = (y1 - y0) / (this.ny - 1);
  }

  at(col, row) {
    if (col < 0 || row < 0 || col >= this.nx || row >= this.ny) return null;
    const v = this.d[row * this.nx + col];
    return v === this.nodata ? null : v;
  }

  // Depth in feet under a projected point, or null outside the lake. Nearest
  // cell, not bilinear: a 25 m grid off 1954 transects does not support
  // pretending to sub-cell precision.
  sampleXY(x, y) {
    const col = Math.round((x - this.x0) / this.stepX);
    const row = Math.round((y - this.y0) / this.stepY);
    return this.at(col, row);
  }

  worldX(col) {
    return this.x0 + col * this.stepX;
  }

  worldY(row) {
    return this.y0 + row * this.stepY;
  }
}

// --- contouring ------------------------------------------------------------

// Marching squares. Cells with any nodata corner are skipped, which is why the
// lines stop cleanly at the shore instead of shooting across land -- the
// shoreline is already the 0 ft boundary, so nothing is lost by not contouring
// across it.
function segmentsAtLevel(g, level) {
  const segs = [];
  const nx = g.nx;

  for (let row = 0; row < g.ny - 1; row++) {
    for (let col = 0; col < nx - 1; col++) {
      const i = row * nx + col;
      const a = g.d[i];              // SW
      const b = g.d[i + 1];          // SE
      const c = g.d[i + nx + 1];     // NE
      const dd = g.d[i + nx];        // NW
      if (a === NODATA || b === NODATA || c === NODATA || dd === NODATA) continue;

      let code = 0;
      if (a >= level) code |= 1;
      if (b >= level) code |= 2;
      if (c >= level) code |= 4;
      if (dd >= level) code |= 8;
      if (code === 0 || code === 15) continue;

      const x0 = g.worldX(col);
      const y0 = g.worldY(row);
      const sx = g.stepX;
      const sy = g.stepY;

      // Crossing point on each edge, linearly interpolated between the two
      // corner depths so the lines are smooth rather than stair-stepped.
      const lerp = (v0, v1) => (level - v0) / (v1 - v0);
      const south = () => [x0 + sx * lerp(a, b), y0];
      const east = () => [x0 + sx, y0 + sy * lerp(b, c)];
      const north = () => [x0 + sx * lerp(dd, c), y0 + sy];
      const west = () => [x0, y0 + sy * lerp(a, dd)];

      switch (code) {
        case 1: case 14: segs.push([west(), south()]); break;
        case 2: case 13: segs.push([south(), east()]); break;
        case 3: case 12: segs.push([west(), east()]); break;
        case 4: case 11: segs.push([east(), north()]); break;
        case 6: case 9:  segs.push([south(), north()]); break;
        case 7: case 8:  segs.push([west(), north()]); break;
        // Saddles. Which way the two lines connect is genuinely ambiguous from
        // four corners; both are drawn as the unambiguous pair of segments,
        // since for a depth line on a map either resolution reads the same.
        case 5:  segs.push([west(), south()], [east(), north()]); break;
        case 10: segs.push([south(), east()], [north(), west()]); break;
      }
    }
  }
  return segs;
}

// Chain segments end-to-end into polylines. Stroking one long path instead of
// thousands of two-point paths is what keeps this smooth on a phone, and it
// lets the line have proper joins instead of visible breaks at every cell.
function chain(segs) {
  const key = (p) => `${Math.round(p[0] * 10)},${Math.round(p[1] * 10)}`;
  const ends = new Map();
  for (const s of segs) {
    for (const p of [s[0], s[1]]) {
      const k = key(p);
      if (!ends.has(k)) ends.set(k, []);
      ends.get(k).push(s);
    }
  }

  const used = new Set();
  const lines = [];

  for (const seed of segs) {
    if (used.has(seed)) continue;
    used.add(seed);
    const line = [seed[0], seed[1]];

    // Walk forward from the tail, then backward from the head.
    for (const dir of [0, 1]) {
      for (;;) {
        const tip = dir === 0 ? line[line.length - 1] : line[0];
        const cands = ends.get(key(tip)) || [];
        const next = cands.find((s) => !used.has(s));
        if (!next) break;
        used.add(next);
        const other = key(next[0]) === key(tip) ? next[1] : next[0];
        if (dir === 0) line.push(other);
        else line.unshift(other);
      }
    }
    if (line.length > 2) lines.push(line);
  }
  return lines;
}

// Contours at an explicit list of depths. Cached on the grid: the slider
// changes this and nothing else does, so recomputing per frame would burn
// battery for no reason.
export function contoursAtLevels(g, levels) {
  const key = levels.join(",");
  g._cache = g._cache || new Map();
  const hit = g._cache.get(key);
  if (hit) return hit;

  const out = [];
  for (const level of levels) {
    if (level <= 0 || level > g.maxFt) continue;
    for (const line of chain(segmentsAtLevel(g, level))) {
      out.push({ depth: level, pts: line });
    }
  }
  g._cache.set(key, out);
  return out;
}

// Contours at every multiple of `interval` ft.
export function contoursAt(g, interval) {
  const levels = [];
  for (let level = interval; level <= g.maxFt; level += interval) levels.push(level);
  return contoursAtLevels(g, levels);
}

// The depth-tint band edges the chart theme paints. Stroking these as real
// lines is what a paper chart does -- the tint boundary IS a depth contour --
// and it also hides the soft edge the upscaled raster would otherwise show
// where one band meets the next.
export const CHART_BAND_EDGES = [6, 12, 18];

// --- shaded relief ---------------------------------------------------------

// Two ways to colour water, because the two themes are answering different
// questions.
//
// `night` is a continuous ramp: shallow reads warm and light, deep reads dark
// blue, so the eye gets bottom shape before it reads a single number. Anchored
// at the depths that matter to a boat rather than spread evenly over the range
// -- most of the contrast is spent between 0 and 15 ft, where the outboard is.
//
// `chart` is discrete tint bands over white, the way a paper NOAA chart does
// it. Bands beat a gradient for the safety question, because "am I inside the
// blue" is a yes/no you can answer at a glance in the sun, where "is this blue
// slightly darker than that blue" is not. The band edges are NOAA's usual
// shoal tints in feet.
const RAMP = [
  [0, [126, 196, 178]],
  [3, [104, 178, 176]],
  [6, [72, 148, 172]],
  [10, [46, 118, 162]],
  [15, [32, 94, 146]],
  [25, [22, 72, 122]],
  [40, [16, 54, 96]],
  [60, [11, 40, 74]],
  [80, [8, 30, 58]],
];

const CHART_BANDS = [
  [6, [130, 189, 224]],   // 0-6 ft: deepest tint, the "do not go" band
  [12, [176, 214, 236]],  // 6-12 ft
  [18, [211, 232, 245]],  // 12-18 ft
  [Infinity, [246, 251, 254]], // deeper than 18 ft: chart white
];

export const PALETTES = {
  night: { kind: "ramp", stops: RAMP },
  chart: { kind: "bands", stops: CHART_BANDS },
};

// CSS gradient for the legend, built from the same stops the water is drawn
// with. Two hand-kept copies of a colour ramp drift; this one cannot.
export function rampCss(maxFt, theme = "night") {
  const pal = PALETTES[theme] || PALETTES.night;
  if (pal.kind === "bands") {
    // Hard stops, so the legend shows bands rather than implying a gradient
    // the water does not have.
    const parts = [];
    let prev = 0;
    for (const [edge, c] of pal.stops) {
      const to = Math.min(edge === Infinity ? maxFt : edge, maxFt);
      const a = Math.round((prev / maxFt) * 100);
      const b = Math.round((to / maxFt) * 100);
      parts.push(`rgb(${c.join(",")}) ${a}%`, `rgb(${c.join(",")}) ${b}%`);
      prev = to;
      if (to >= maxFt) break;
    }
    return `linear-gradient(90deg, ${parts.join(", ")})`;
  }
  const stops = pal.stops
    .filter((s) => s[0] <= maxFt)
    .map(([d, c]) => `rgb(${c.join(",")}) ${Math.round((d / maxFt) * 100)}%`);
  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

function bandColor(ft) {
  for (const [edge, c] of CHART_BANDS) if (ft < edge) return c;
  return CHART_BANDS[CHART_BANDS.length - 1][1];
}

function rampColor(ft) {
  let i = 0;
  while (i < RAMP.length - 1 && ft > RAMP[i + 1][0]) i++;
  if (i === RAMP.length - 1) return RAMP[i][1];
  const [d0, c0] = RAMP[i];
  const [d1, c1] = RAMP[i + 1];
  const t = (ft - d0) / (d1 - d0);
  return [
    c0[0] + (c1[0] - c0[0]) * t,
    c0[1] + (c1[1] - c0[1]) * t,
    c0[2] + (c1[2] - c0[2]) * t,
  ];
}

// One offscreen canvas at grid resolution, drawn scaled by the renderer. Built
// once per (theme, shallowFt) pair rather than per frame -- 120k pixels is
// cheap once and wasteful sixty times a second.
export function shadeCanvas(g, shallowFt, theme = "night") {
  const key = `${theme}|${shallowFt}`;
  g._shadeCache = g._shadeCache || new Map();
  const hit = g._shadeCache.get(key);
  if (hit) return hit;
  const chart = theme === "chart";

  const cv = document.createElement("canvas");
  cv.width = g.nx;
  cv.height = g.ny;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(g.nx, g.ny);

  for (let row = 0; row < g.ny; row++) {
    // ImageData row 0 is the TOP of the image = north, while grid row 0 is
    // south. Flipping here means the renderer can draw it as a plain rect.
    const src = (g.ny - 1 - row) * g.nx;
    const dst = row * g.nx;
    for (let col = 0; col < g.nx; col++) {
      const v = g.d[src + col];
      const o = (dst + col) * 4;
      if (v === NODATA) {
        img.data[o + 3] = 0;
        continue;
      }
      const [r, gg, b] = chart ? bandColor(v) : rampColor(v);
      const shallow = shallowFt > 0 && v <= shallowFt;
      // The shallow band is warmed rather than recoloured outright, so it still
      // reads as depth and not as a separate unrelated layer.
      img.data[o] = shallow ? Math.min(255, r * 0.5 + 190) : r;
      img.data[o + 1] = shallow ? Math.min(255, gg * 0.5 + 120) : gg;
      img.data[o + 2] = shallow ? Math.min(255, b * 0.5 + 40) : b;
      img.data[o + 3] = 255;
    }
  }

  ctx.putImageData(img, 0, 0);
  g._shadeCache.set(key, cv);
  return cv;
}

export { NODATA };
