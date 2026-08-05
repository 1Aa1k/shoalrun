// Canvas renderer. Deliberately no map tiles: the lake has no cell service, so
// the shoreline is drawn from the vector polygon that ships inside the app.
// That makes the map work at full fidelity offline with nothing cached.
//
// Water is shaded from the interpolated depth grid rather than filled flat. On
// a lake whose whole hazard story is "the south end is shallow and rocky, the
// north end is a 70 ft basin", a single blue fill was throwing away the one
// piece of context that makes the hazard cloud legible at a glance.

import { shadeCanvas } from "./depth.js";

// Two themes, because a boat display has two jobs that want opposite things.
//
// `night` is the dark screen-native look: low light output, glowing shoreline,
// good at dusk and after dark when the phone is the brightest thing in the boat.
//
// `chart` reproduces a NOAA paper chart -- buff land, black shoreline, discrete
// blue shoal tints over white water, magenta hazards, printed spot soundings.
// It is not decoration. In direct sun a dark screen is unreadable and a light
// one is not, and the conventions themselves are worth borrowing: anyone who
// has read a chart already knows that magenta means danger and that the number
// on the water is a measured depth.
const THEMES = {
  night: {
    chartMode: false,
    background: "#10161c",
    water: "#0e2f4a",       // fallback fill where the depth grid has no coverage
    land: "#10161c",
    landHatch: null,
    building: "#7c8b98",
    pier: "#9a815c",
    shore: "#7fc4e8",
    shoreWidth: 1.2,
    shoreGlow: "rgba(127, 196, 232, 0.18)",
    track: [126, 214, 255],
    swept: [86, 196, 138],
    boat: "#ffffff",
    boatEdge: "#06121f",
    corridor: [255, 208, 92],
    contour: (t, major) =>
      `rgba(${140 + t * 60}, ${190 + t * 40}, ${225 + t * 25}, ${(0.16 + t * 0.26) * (major ? 1.6 : 1)})`,
    label: "rgba(190, 214, 232, 0.9)",
    labelKnockout: true,
    sounding: null,          // night theme does not print soundings
    exposed: "#9fb1bf",
    island: "#63788a",
    rock: "#ff6a5a",
    drawdown: "#ff5b4a",
    shoal: "#ffc043",
    confirmed: "#ff3521",
  },
  chart: {
    chartMode: true,
    background: "#e3d5ac",
    water: "#f6fbfe",
    land: "#e8dab0",         // NOAA buff
    landHatch: "rgba(158, 138, 96, 0.30)",
    building: "#5d4a2e",
    pier: "#4a3a22",
    shore: "#1c1c1c",
    shoreWidth: 1.6,
    shoreGlow: null,
    track: [40, 90, 150],
    swept: [46, 132, 90],
    boat: "#111111",
    boatEdge: "#ffffff",
    corridor: [190, 40, 150],
    // Chart contours are a single restrained blue-grey at every depth. Depth is
    // carried by the tint bands and the printed numbers, so making the lines
    // fight for it too just makes the sheet noisy.
    contour: (t, major) => `rgba(56, 104, 140, ${major ? 0.75 : 0.42})`,
    label: "#2b5f80",
    labelKnockout: false,
    sounding: "#33556b",
    // Magenta is the chart convention for danger, and holding to it means the
    // classes read by shape rather than by inventing a second colour language.
    exposed: "#7a6440",
    island: "#8a7346",
    rock: "#c8188c",
    drawdown: "#c8188c",
    shoal: "#c8188c",
    confirmed: "#a3006b",
  },
};

const MAX_MARKER_PX = 18;

const CLASS_KEY = {
  island: "island",
  exposed: "exposed",
  rock: "rock",
  drawdown: "drawdown",
  shoal: "shoal",
};

// Kept for the legend, which needs the night palette's hazard colours.
const COLORS = THEMES.night;
const CLASS_COLOR = {
  island: THEMES.night.island,
  exposed: THEMES.night.exposed,
  rock: THEMES.night.rock,
  drawdown: THEMES.night.drawdown,
  shoal: THEMES.night.shoal,
};

// Depth lines are labelled only past this zoom. Below it the labels collide
// with each other and with the hazard markers, and the markers win.
const LABEL_MIN_SCALE = 0.18;

export class MapView {
  constructor(canvas, proj) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.proj = proj;
    this.scale = 0.08; // px per metre
    this.center = { x: 0, y: 0 };
    this.rotation = 0; // radians; course-up when following
    this.follow = true;
    this.courseUp = true;
    this.theme = "night";
    this._resize();
    window.addEventListener("resize", () => this._resize());
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.w = r.width;
    this.h = r.height;
    this.dpr = dpr;
  }

  // Projected metres -> screen px, honouring pan/zoom/rotation.
  toScreen(x, y) {
    const dx = (x - this.center.x) * this.scale;
    const dy = (y - this.center.y) * this.scale;
    const c = Math.cos(this.rotation);
    const s = Math.sin(this.rotation);
    // Screen y grows downward, so north (+y) must be negated.
    return [this.w / 2 + (dx * c - dy * s), this.h / 2 - (dx * s + dy * c)];
  }

  toWorld(sx, sy) {
    const px = (sx - this.w / 2) / this.scale;
    const py = -(sy - this.h / 2) / this.scale;
    const c = Math.cos(-this.rotation);
    const s = Math.sin(-this.rotation);
    return { x: this.center.x + (px * c - py * s), y: this.center.y + (px * s + py * c) };
  }

  _ring(ring) {
    const ctx = this.ctx;
    ctx.beginPath();
    for (let i = 0; i < ring.length; i++) {
      const [sx, sy] = this.toScreen(ring[i][0], ring[i][1]);
      if (i === 0) ctx.moveTo(sx, sy);
      else ctx.lineTo(sx, sy);
    }
    ctx.closePath();
  }

  // The whole lake as one screen-space path. Built once per frame and reused as
  // fill, clip and stroke -- the depth raster is clipped to it so the water
  // edge stays as crisp as the vector shoreline instead of showing the 25 m
  // stair-step of the grid.
  _lakePath(lake) {
    const p = new Path2D();
    for (const poly of lake) {
      for (const ring of poly) {
        for (let i = 0; i < ring.length; i++) {
          const [sx, sy] = this.toScreen(ring[i][0], ring[i][1]);
          if (i === 0) p.moveTo(sx, sy);
          else p.lineTo(sx, sy);
        }
        p.closePath();
      }
    }
    return p;
  }

  get t() {
    return THEMES[this.theme] || THEMES.night;
  }

  draw(state) {
    const ctx = this.ctx;
    const T = this.t;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);

    // Land background, then water punched out. Drawing it this way means the
    // 74 islands come free as polygon holes instead of needing a second pass.
    ctx.fillStyle = T.land;
    ctx.fillRect(0, 0, this.w, this.h);

    const lakePath = this._lakePath(state.lake);

    ctx.save();
    ctx.fillStyle = T.water;
    ctx.fill(lakePath, "evenodd");
    ctx.restore();

    if (state.grid && state.showDepth) {
      ctx.save();
      ctx.clip(lakePath, "evenodd");
      this._drawDepthRaster(state.grid, state.shallowFt);
      ctx.restore();
    }

    // Soft inner glow along the shore, then the crisp line on top. The glow is
    // what stops a dark shoreline on dark land from disappearing in sunlight.
    // A paper chart has no such thing, so the chart theme skips it.
    if (T.shoreGlow) {
      ctx.save();
      ctx.clip(lakePath, "evenodd");
      ctx.strokeStyle = T.shoreGlow;
      ctx.lineWidth = 7;
      ctx.stroke(lakePath);
      ctx.restore();
    }

    ctx.strokeStyle = T.shore;
    ctx.lineWidth = T.shoreWidth;
    ctx.stroke(lakePath);

    if (state.contours && state.showContours) this._drawContours(state.contours, state.contourInterval);
    // Printed soundings are the measured 1954 numbers, not the interpolated
    // surface -- on a chart the sounding is data and the contour is inference,
    // and with 260 points over 34 km2 that distinction is the whole story.
    if (T.chartMode && state.soundings && state.showSoundings) this._drawSoundings(state.soundings);
    if (state.structures && state.showCamps) this._drawStructures(state.structures);
    // Proven water goes UNDER everything else. It is context for reading the
    // hazards, not a hazard itself, and it must never obscure a marker.
    if (state.swept && state.showSwept !== false) this._drawSwept(state.swept);
    if (state.track && state.track.length > 1) this._drawTrack(state.track);
    if (state.corridor) this._drawCorridor(state.corridor);
    this._drawRocks(state);
    if (state.fix) this._drawBoat(state);
  }

  // NOAA prints soundings as bare numbers on the water. They are decluttered by
  // zoom rather than all drawn at once, because 260 numbers on a phone screen
  // is not a chart, it is a wall.
  _drawSoundings(pts) {
    const ctx = this.ctx;
    const T = this.t;
    if (this.scale < 0.12) return;
    const stride = this.scale > 0.4 ? 1 : this.scale > 0.22 ? 2 : 4;

    ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = T.sounding;
    for (let i = 0; i < pts.length; i += stride) {
      const p = pts[i];
      const [sx, sy] = this.toScreen(p.x, p.y);
      if (sx < 8 || sy < 8 || sx > this.w - 8 || sy > this.h - 8) continue;
      ctx.fillText(String(p.ft), sx, sy);
    }
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
  }

  // The depth surface as an image. It is an axis-aligned grid in world space,
  // so the view transform reduces to translate/rotate/scale rather than a
  // per-pixel resample -- which is what makes this affordable every frame.
  _drawDepthRaster(grid, shallowFt) {
    const ctx = this.ctx;
    const img = shadeCanvas(grid, shallowFt, this.theme);

    // North-west corner of the raster, since the image's first row is north.
    const [sx, sy] = this.toScreen(grid.worldX(0), grid.worldY(grid.ny - 1));
    const wMetres = grid.stepX * (grid.nx - 1);
    const hMetres = grid.stepY * (grid.ny - 1);

    ctx.save();
    ctx.globalAlpha = this.t.chartMode ? 1 : 0.92;
    // Chart tints are flat areas with hard edges. Smoothing turns each band
    // boundary into an airbrushed halo, which is the one thing that stops the
    // result reading as a chart -- so the chart theme takes the 25 m blocks.
    ctx.imageSmoothingEnabled = !this.t.chartMode;
    ctx.imageSmoothingQuality = "high";
    ctx.translate(sx, sy);
    ctx.rotate(-this.rotation);
    ctx.scale((wMetres * this.scale) / img.width, (hMetres * this.scale) / img.height);
    ctx.drawImage(img, 0, 0);
    ctx.restore();
  }

  // Depth contours, generated at runtime from the grid so the interval is a
  // slider rather than a build-time constant. Deeper lines are brighter so the
  // deep basin reads at a glance without needing every line labelled.
  _drawContours(contours, interval) {
    const ctx = this.ctx;
    const T = this.t;
    ctx.lineWidth = 1;
    ctx.lineJoin = "round";

    const labelled = [];
    // Label every other line when they are close together, so a 2 ft interval
    // does not paper the lake in numbers.
    const labelEvery = interval <= 5 ? 2 : 1;
    let idx = 0;

    for (const c of contours) {
      const t = Math.min(1, c.depth / 70);
      // Major lines every 5 intervals get more weight, the way a paper chart
      // does it -- it gives the eye a ladder to count by.
      const major = c.major || Math.round(c.depth / interval) % 5 === 0;
      ctx.strokeStyle = T.contour(t, major);
      ctx.lineWidth = major ? 1.4 : 0.9;

      ctx.beginPath();
      let onScreen = false;
      let mid = null;
      for (let i = 0; i < c.pts.length; i++) {
        const [sx, sy] = this.toScreen(c.pts[i][0], c.pts[i][1]);
        if (sx > 0 && sy > 0 && sx < this.w && sy < this.h) {
          onScreen = true;
          if (!mid && i > 0) mid = [sx, sy, this.toScreen(c.pts[i - 1][0], c.pts[i - 1][1])];
        }
        if (i === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      }
      ctx.stroke();

      if (onScreen && mid && major && this.scale > LABEL_MIN_SCALE && idx++ % labelEvery === 0) {
        labelled.push({ depth: c.depth, at: mid });
      }
    }

    if (!labelled.length) return;

    // Labels last, so no line is drawn through a number.
    ctx.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const l of labelled) {
      const [sx, sy, prev] = l.at;
      const text = `${l.depth}`;
      // Rotate the label to sit along the line, flipped when it would read
      // upside down.
      let ang = Math.atan2(sy - prev[1], sx - prev[0]);
      if (ang > Math.PI / 2 || ang < -Math.PI / 2) ang += Math.PI;

      ctx.save();
      ctx.translate(sx, sy);
      ctx.rotate(ang);
      const wpx = ctx.measureText(text).width;
      // Knock a hole in the line behind the text rather than drawing a box.
      // Only safe on the dark theme: on the chart theme, punching through
      // exposes whatever is beneath the canvas rather than the buff paper.
      if (T.labelKnockout) {
        ctx.globalCompositeOperation = "destination-out";
        ctx.fillStyle = "#000";
        ctx.fillRect(-wpx / 2 - 3, -6, wpx + 6, 12);
        ctx.globalCompositeOperation = "source-over";
      } else {
        ctx.fillStyle = "rgba(255,255,255,0.82)";
        ctx.fillRect(-wpx / 2 - 3, -6, wpx + 6, 12);
      }
      ctx.fillStyle = T.label;
      ctx.fillText(text, 0, 0);
      ctx.restore();
    }
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
  }

  // Structures are drawn on the land side, under the hazards. Names appear only
  // when zoomed in enough to read them without covering the water.
  _drawStructures(items) {
    const ctx = this.ctx;
    const T = this.t;
    const showNames = this.scale > 0.25;
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    for (const s of items) {
      if (s.type === "Point") {
        const [x, y] = this.toScreen(s.pts[0][0], s.pts[0][1]);
        if (x < -20 || y < -20 || x > this.w + 20 || y > this.h + 20) continue;
        ctx.fillStyle = T.building;
        ctx.fillRect(x - 2, y - 2, 4, 4);
        if (showNames && s.name) {
          ctx.fillStyle = "rgba(200,210,220,0.75)";
          ctx.fillText(s.name, x + 6, y + 3);
        }
        continue;
      }
      ctx.beginPath();
      let vis = false;
      for (let i = 0; i < s.pts.length; i++) {
        const [x, y] = this.toScreen(s.pts[i][0], s.pts[i][1]);
        if (x > -40 && y > -40 && x < this.w + 40 && y < this.h + 40) vis = true;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      if (!vis) continue;
      if (s.type === "Polygon") {
        ctx.closePath();
        ctx.fillStyle = T.building;
        ctx.globalAlpha = 0.55;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = T.building;
        ctx.lineWidth = 1;
        ctx.stroke();
      } else {
        ctx.strokeStyle = s.kind === "pier" ? T.pier : T.building;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (showNames && s.name) {
        const [x, y] = this.toScreen(s.pts[0][0], s.pts[0][1]);
        ctx.fillStyle = "rgba(200,210,220,0.8)";
        ctx.fillText(s.name, x + 5, y - 4);
      }
    }
  }

  // Track fades with age. Water you drove through an hour ago is weaker
  // evidence than water you drove through a minute ago, and the fade says so
  // without needing a legend.
  // Water the boat has driven through, accumulated over every trip.
  //
  // Drawn as a positive statement -- "this has been proven" -- and never as an
  // absence. Unswept water is left exactly as it is rather than fogged or
  // greyed, because unknown is not the same as dangerous, and shading it would
  // tell the user the opposite. That distinction is the whole point of the
  // layer: on this lake the imagery cannot see depth at all, so a driven track
  // is the only direct evidence there is.
  _drawSwept(swept) {
    const ctx = this.ctx;
    const cell = swept.cell;
    // Below this the cells are sub-pixel and the layer becomes a smear, which
    // would read as a solid claim over water that was never driven.
    const px = cell * this.scale;
    if (px < 1.2) return;
    const T = this.theme;
    const col = T.swept || [86, 196, 138];
    ctx.save();
    for (const [k, v] of swept.cells) {
      const [cx, cy] = k.split(",");
      const x = (+cx + 0.5) * cell;
      const y = (+cy + 0.5) * cell;
      const [sx, sy] = this.toScreen(x, y);
      if (sx < -px || sy < -px || sx > this.w + px || sy > this.h + px) continue;
      // A planing pass proves less depth than a slow one, so it is drawn
      // fainter rather than being counted the same.
      ctx.fillStyle = `rgba(${col[0]},${col[1]},${col[2]},${v.planing ? 0.16 : 0.3})`;
      ctx.fillRect(sx - px / 2, sy - px / 2, px, px);
    }
    ctx.restore();
  }

  _drawTrack(track) {
    const ctx = this.ctx;
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    const n = track.length;
    const step = Math.max(1, Math.floor(n / 400));
    for (let i = step; i < n; i += step) {
      const age = i / n; // 0 = oldest, 1 = newest
      ctx.strokeStyle = `rgba(126, 214, 255, ${0.08 + age * 0.42})`;
      const [ax, ay] = this.toScreen(track[i - step].x, track[i - step].y);
      const [bx, by] = this.toScreen(track[i].x, track[i].y);
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.stroke();
    }
    ctx.lineCap = "butt";
  }

  _drawCorridor(c) {
    const ctx = this.ctx;
    const [ax, ay] = this.toScreen(c.ax, c.ay);
    const [bx, by] = this.toScreen(c.bx, c.by);
    const w = c.halfW * this.scale;
    const ang = Math.atan2(by - ay, bx - ax);
    const nx = Math.cos(ang + Math.PI / 2) * w;
    const ny = Math.sin(ang + Math.PI / 2) * w;

    // Faded toward the far end: the corridor is a projection of where the boat
    // will be in 20 s, and confidence in that falls off with distance.
    const grad = ctx.createLinearGradient(ax, ay, bx, by);
    grad.addColorStop(0, "rgba(255, 208, 92, 0.16)");
    grad.addColorStop(1, "rgba(255, 208, 92, 0.02)");

    ctx.fillStyle = grad;
    ctx.strokeStyle = COLORS.corridorEdge;
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(ax + nx, ay + ny);
    ctx.lineTo(bx + nx, by + ny);
    ctx.lineTo(bx - nx, by - ny);
    ctx.lineTo(ax - nx, ay - ny);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // NOAA hazard symbols. A submerged rock is a cross inside a dotted danger
  // circle; a rock that breaks the surface is an asterisk. Using the real
  // symbols means anyone who reads charts already knows what these are, and
  // the shape carries the class so magenta can mean one thing only: danger.
  _drawChartSymbol(sx, sy, r, color, unconfirmed, radPx) {
    const ctx = this.ctx;
    const submerged = r.cls === "shoal" || r.cls === "drawdown";
    const size = Math.max(5, Math.min(11, radPx));

    ctx.strokeStyle = color;
    ctx.lineWidth = unconfirmed ? 1 : 1.7;
    ctx.lineCap = "round";

    if (submerged) {
      ctx.beginPath();
      ctx.moveTo(sx - size * 0.55, sy);
      ctx.lineTo(sx + size * 0.55, sy);
      ctx.moveTo(sx, sy - size * 0.55);
      ctx.lineTo(sx, sy + size * 0.55);
      ctx.stroke();
      // Dotted danger circle. On a chart this is what says "keep clear of the
      // whole area", not merely "there is a point here".
      ctx.setLineDash([2, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(sx, sy, size, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.beginPath();
      for (let i = 0; i < 3; i++) {
        const a = (i * Math.PI) / 3;
        ctx.moveTo(sx - Math.cos(a) * size * 0.8, sy - Math.sin(a) * size * 0.8);
        ctx.lineTo(sx + Math.cos(a) * size * 0.8, sy + Math.sin(a) * size * 0.8);
      }
      ctx.stroke();
    }
    ctx.lineCap = "butt";
  }

  _drawRocks(state) {
    const ctx = this.ctx;
    const T = this.t;
    for (const r of state.rocks) {
      if (!state.showShore && !r.offshore) continue;
      const [sx, sy] = this.toScreen(r.x, r.y);
      if (sx < -30 || sy < -30 || sx > this.w + 30 || sy > this.h + 30) continue;

      const mark = state.marks.get(r.id);
      const verdict = mark && mark.verdict;
      if (verdict === "absent") {
        // Kept visible but greyed: the user said it is not there, and hiding it
        // outright would make a mistaken dismissal permanent and invisible.
        ctx.globalAlpha = 0.35;
      }

      // Appearance is driven by NAIP verification, not by the raw detector
      // class. An unconfirmed candidate is drawn faint and hollow so the map
      // never gives a 10 m inference the same visual weight as a 0.3 m
      // confirmation -- but it is still drawn, because one September flight
      // cannot prove a rock is absent.
      const human = r.evidence === "human_mapped";
      const unconfirmed = !human && (r.verdict === "open_water" || r.verdict === "unchecked");
      const color =
        human
          ? T.confirmed
          : verdict === "confirmed"
          ? T.confirmed
          : r.verdict === "shoal_confirmed"
          ? T.shoal
          : r.verdict === "rock_confirmed"
          ? T.rock
          : T[CLASS_KEY[r.cls]] || T.exposed;

      // Radius from true footprint, but clamped at both ends. Floored so a
      // single-pixel rock stays tappable; capped because an unmapped island of
      // 96,700 m2 is a 175 m radius, which at any useful zoom renders as a disc
      // that covers the screen and hides the hazards next to it. Beyond the cap
      // the marker stops meaning "this big" and starts meaning "look here".
      const rad = Math.min(MAX_MARKER_PX, Math.max(4, Math.sqrt(r.area_m2 / Math.PI) * this.scale));

      if (T.chartMode) {
        // Islands are landmasses, not point dangers; they are already drawn as
        // land by the shoreline pass, so no symbol goes on top of them.
        if (r.cls !== "island") {
          ctx.globalAlpha *= unconfirmed ? 0.45 : 1;
          this._drawChartSymbol(sx, sy, r, color, unconfirmed, rad);
        }
        ctx.globalAlpha = 1;
        continue;
      }

      ctx.beginPath();
      ctx.arc(sx, sy, rad, 0, Math.PI * 2);
      ctx.fillStyle = color;
      // Islands are obstructions to route around, not point hazards to dodge --
      // drawn hollow so they read as "land here" rather than "rock here".
      ctx.globalAlpha *= unconfirmed ? 0.0 : r.cls === "island" ? 0.12 : verdict === "confirmed" ? 0.9 : 0.75;
      ctx.fill();
      ctx.globalAlpha = verdict === "absent" ? 0.35 : 1;
      ctx.lineWidth = verdict === "confirmed" ? 2.5 : unconfirmed ? 0.8 : 1.8;
      if (unconfirmed) ctx.setLineDash([2, 3]);
      ctx.strokeStyle = color;
      ctx.stroke();
      ctx.setLineDash([]);

      if (verdict === "confirmed") {
        // Halo, plus a hard centre dot. A confirmed hazard should be findable
        // in peripheral vision without reading the map properly.
        ctx.beginPath();
        ctx.arc(sx, sy, rad + 4, 0, Math.PI * 2);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.6;
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.arc(sx, sy, 1.6, 0, Math.PI * 2);
        ctx.fillStyle = "#fff";
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
  }

  _drawBoat(state) {
    const ctx = this.ctx;
    const [sx, sy] = this.toScreen(state.fix.x, state.fix.y);

    // Accuracy circle, so the user can see when the fix itself is the problem.
    if (state.fix.accuracy) {
      const r = state.fix.accuracy * this.scale;
      const g = ctx.createRadialGradient(sx, sy, 0, sx, sy, Math.max(r, 1));
      g.addColorStop(0, "rgba(255,255,255,0.12)");
      g.addColorStop(1, "rgba(255,255,255,0.0)");
      ctx.beginPath();
      ctx.arc(sx, sy, r, 0, Math.PI * 2);
      ctx.fillStyle = g;
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.14)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    ctx.save();
    ctx.translate(sx, sy);
    // Heading is a bearing in world space; the view may itself be rotated.
    ctx.rotate(-(state.heading ?? Math.PI / 2) + Math.PI / 2 - this.rotation);
    ctx.shadowColor = "rgba(0,0,0,0.55)";
    ctx.shadowBlur = 6;
    ctx.beginPath();
    ctx.moveTo(0, -12);
    ctx.lineTo(7.5, 9);
    ctx.lineTo(0, 4.5);
    ctx.lineTo(-7.5, 9);
    ctx.closePath();
    ctx.fillStyle = COLORS.boat;
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.strokeStyle = COLORS.boatEdge;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();
  }

  hitTest(sx, sy, rocks, radiusPx = 22) {
    let best = null;
    let bestD = radiusPx;
    for (const r of rocks) {
      const [x, y] = this.toScreen(r.x, r.y);
      const d = Math.hypot(x - sx, y - sy);
      if (d < bestD) {
        bestD = d;
        best = r;
      }
    }
    return best;
  }
}

export { COLORS, CLASS_COLOR };
