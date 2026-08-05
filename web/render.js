// Canvas renderer. Deliberately no map tiles: the lake has no cell service, so
// the shoreline is drawn from the vector polygon that ships inside the app.
// That makes the map work at full fidelity offline with nothing cached.

const COLORS = {
  water: "#0b2942",
  waterDeep: "#081f33",
  land: "#141a1f",
  building: "#6b7a86",
  pier: "#8a7250",
  shore: "#3f6d94",
  track: "rgba(120, 200, 255, 0.35)",
  boat: "#ffffff",
  corridor: "rgba(255, 210, 90, 0.13)",
  corridorEdge: "rgba(255, 210, 90, 0.35)",
  exposed: "#8b9aa6",
  island: "#5a6b78",
  rock: "#e0574a",
  drawdown: "#ff5b4a",
  shoal: "#ffb02e",
  confirmed: "#ff2d1a",
  absent: "#3a4750",
};

const MAX_MARKER_PX = 18;

const CLASS_COLOR = {
  island: COLORS.island,
  exposed: COLORS.exposed,
  rock: COLORS.rock,
  drawdown: COLORS.drawdown,
  shoal: COLORS.shoal,
};

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

  draw(state) {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);

    // Land background, then water punched out. Drawing it this way means the
    // 74 islands come free as polygon holes instead of needing a second pass.
    ctx.fillStyle = COLORS.land;
    ctx.fillRect(0, 0, this.w, this.h);

    ctx.save();
    ctx.fillStyle = COLORS.water;
    ctx.beginPath();
    for (const poly of state.lake) {
      for (const ring of poly) {
        for (let i = 0; i < ring.length; i++) {
          const [sx, sy] = this.toScreen(ring[i][0], ring[i][1]);
          if (i === 0) ctx.moveTo(sx, sy);
          else ctx.lineTo(sx, sy);
        }
        ctx.closePath();
      }
    }
    ctx.fill("evenodd");
    ctx.restore();

    // Shoreline stroke on top for a crisp edge at any zoom.
    ctx.strokeStyle = COLORS.shore;
    ctx.lineWidth = 1;
    for (const poly of state.lake) {
      for (const ring of poly) {
        this._ring(ring);
        ctx.stroke();
      }
    }

    if (state.contours) this._drawContours(state.contours);
    if (state.structures) this._drawStructures(state.structures);
    if (state.track && state.track.length > 1) this._drawTrack(state.track);
    if (state.corridor) this._drawCorridor(state.corridor);
    this._drawRocks(state);
    if (state.fix) this._drawBoat(state);
  }

  // Depth contours, drawn beneath everything else. Deeper lines are brighter so
  // the deep basin reads at a glance without needing every line labelled.
  _drawContours(contours) {
    const ctx = this.ctx;
    ctx.lineWidth = 1;
    for (const c of contours) {
      const t = Math.min(1, c.depth / 70);
      ctx.strokeStyle = `rgba(${90 + t * 70}, ${140 + t * 70}, ${190 + t * 60}, ${0.22 + t * 0.3})`;
      ctx.beginPath();
      for (let i = 0; i < c.pts.length; i++) {
        const [sx, sy] = this.toScreen(c.pts[i][0], c.pts[i][1]);
        if (i === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      }
      ctx.stroke();
    }
  }

  // Structures are drawn on the land side, under the hazards. Names appear only
  // when zoomed in enough to read them without covering the water.
  _drawStructures(items) {
    const ctx = this.ctx;
    const showNames = this.scale > 0.25;
    for (const s of items) {
      if (s.type === "Point") {
        const [x, y] = this.toScreen(s.pts[0][0], s.pts[0][1]);
        if (x < -20 || y < -20 || x > this.w + 20 || y > this.h + 20) continue;
        ctx.fillStyle = COLORS.building;
        ctx.fillRect(x - 2, y - 2, 4, 4);
        if (showNames && s.name) {
          ctx.fillStyle = "rgba(200,210,220,0.75)";
          ctx.font = "10px sans-serif";
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
        ctx.fillStyle = COLORS.building;
        ctx.globalAlpha = 0.55;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = COLORS.building;
        ctx.lineWidth = 1;
        ctx.stroke();
      } else {
        ctx.strokeStyle = s.kind === "pier" ? COLORS.pier : COLORS.building;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (showNames && s.name) {
        const [x, y] = this.toScreen(s.pts[0][0], s.pts[0][1]);
        ctx.fillStyle = "rgba(200,210,220,0.8)";
        ctx.font = "10px sans-serif";
        ctx.fillText(s.name, x + 5, y - 4);
      }
    }
  }

  _drawTrack(track) {
    const ctx = this.ctx;
    ctx.strokeStyle = COLORS.track;
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.beginPath();
    for (let i = 0; i < track.length; i++) {
      const [sx, sy] = this.toScreen(track[i].x, track[i].y);
      if (i === 0) ctx.moveTo(sx, sy);
      else ctx.lineTo(sx, sy);
    }
    ctx.stroke();
  }

  _drawCorridor(c) {
    const ctx = this.ctx;
    const [ax, ay] = this.toScreen(c.ax, c.ay);
    const [bx, by] = this.toScreen(c.bx, c.by);
    const w = c.halfW * this.scale;
    const ang = Math.atan2(by - ay, bx - ax);
    const nx = Math.cos(ang + Math.PI / 2) * w;
    const ny = Math.sin(ang + Math.PI / 2) * w;

    ctx.fillStyle = COLORS.corridor;
    ctx.strokeStyle = COLORS.corridorEdge;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(ax + nx, ay + ny);
    ctx.lineTo(bx + nx, by + ny);
    ctx.lineTo(bx - nx, by - ny);
    ctx.lineTo(ax - nx, ay - ny);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }

  _drawRocks(state) {
    const ctx = this.ctx;
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
          ? COLORS.confirmed
          : verdict === "confirmed"
          ? COLORS.confirmed
          : r.verdict === "shoal_confirmed"
          ? COLORS.shoal
          : r.verdict === "rock_confirmed"
          ? COLORS.rock
          : CLASS_COLOR[r.cls] || COLORS.exposed;

      // Radius from true footprint, but clamped at both ends. Floored so a
      // single-pixel rock stays tappable; capped because an unmapped island of
      // 96,700 m2 is a 175 m radius, which at any useful zoom renders as a disc
      // that covers the screen and hides the hazards next to it. Beyond the cap
      // the marker stops meaning "this big" and starts meaning "look here".
      const rad = Math.min(MAX_MARKER_PX, Math.max(4, Math.sqrt(r.area_m2 / Math.PI) * this.scale));

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
        ctx.beginPath();
        ctx.arc(sx, sy, rad + 4, 0, Math.PI * 2);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.6;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
  }

  _drawBoat(state) {
    const ctx = this.ctx;
    const [sx, sy] = this.toScreen(state.fix.x, state.fix.y);

    // Accuracy circle, so the user can see when the fix itself is the problem.
    if (state.fix.accuracy) {
      ctx.beginPath();
      ctx.arc(sx, sy, state.fix.accuracy * this.scale, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255,255,255,0.07)";
      ctx.fill();
    }

    ctx.save();
    ctx.translate(sx, sy);
    // Heading is a bearing in world space; the view may itself be rotated.
    ctx.rotate(-(state.heading ?? Math.PI / 2) + Math.PI / 2 - this.rotation);
    ctx.beginPath();
    ctx.moveTo(0, -11);
    ctx.lineTo(7, 9);
    ctx.lineTo(0, 5);
    ctx.lineTo(-7, 9);
    ctx.closePath();
    ctx.fillStyle = COLORS.boat;
    ctx.fill();
    ctx.strokeStyle = "#0b2942";
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
