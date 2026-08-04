// Canvas renderer. Deliberately no map tiles: the lake has no cell service, so
// the shoreline is drawn from the vector polygon that ships inside the app.
// That makes the map work at full fidelity offline with nothing cached.

const COLORS = {
  water: "#0b2942",
  waterDeep: "#081f33",
  land: "#141a1f",
  shore: "#3f6d94",
  track: "rgba(120, 200, 255, 0.35)",
  boat: "#ffffff",
  corridor: "rgba(255, 210, 90, 0.13)",
  corridorEdge: "rgba(255, 210, 90, 0.35)",
  exposed: "#8b9aa6",
  drawdown: "#ff5b4a",
  shoal: "#ffb02e",
  confirmed: "#ff2d1a",
  absent: "#3a4750",
};

const CLASS_COLOR = {
  exposed: COLORS.exposed,
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

    if (state.track && state.track.length > 1) this._drawTrack(state.track);
    if (state.corridor) this._drawCorridor(state.corridor);
    this._drawRocks(state);
    if (state.fix) this._drawBoat(state);
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
      const [sx, sy] = this.toScreen(r.x, r.y);
      if (sx < -30 || sy < -30 || sx > this.w + 30 || sy > this.h + 30) continue;

      const mark = state.marks.get(r.id);
      const verdict = mark && mark.verdict;
      if (verdict === "absent") {
        // Kept visible but greyed: the user said it is not there, and hiding it
        // outright would make a mistaken dismissal permanent and invisible.
        ctx.globalAlpha = 0.35;
      }

      const color =
        verdict === "confirmed" ? COLORS.confirmed : CLASS_COLOR[r.cls] || COLORS.exposed;

      // Radius from footprint, floored so a one-pixel rock is still tappable.
      const rad = Math.max(4, Math.sqrt(r.area_m2 / Math.PI) * this.scale);

      ctx.beginPath();
      ctx.arc(sx, sy, rad, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.globalAlpha *= verdict === "confirmed" ? 0.9 : 0.55;
      ctx.fill();
      ctx.globalAlpha = verdict === "absent" ? 0.35 : 1;
      ctx.lineWidth = verdict === "confirmed" ? 2.5 : 1.5;
      ctx.strokeStyle = color;
      ctx.stroke();

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
