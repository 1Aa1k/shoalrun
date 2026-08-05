// Swept area: water the boat has actually driven through, accumulated across
// every trip.
//
// This is the strongest evidence in the whole tool, and it costs nothing to
// collect. Aerial imagery on this lake was measured to carry no depth
// information at all -- it cannot separate 10 ft of water from 25 ft. But a
// pass that did not hit anything proves the water along that line is at least
// as deep as the boat draws. That proof does not care whether the water is
// stained, whether the sun was glinting, or what year the photo was taken.
//
// Hydrographers call this a swept area and used it to prove channels long
// before sonar. The difference here is that the sweeping happens anyway,
// whenever he goes out.
//
// WHAT A CELL MEANS. "Driven, at least DRAFT_FT deep, at this lake level, on
// this date." Not "safe forever" -- see the staleness rules below.
//
// WHAT AN EMPTY CELL MEANS. Nothing. Unknown is not dangerous. Drawing unswept
// water as hazardous would repeat exactly the overclaiming that had to be
// stripped out of the rock layer.

// Cell size. Chosen against GPS, not against how pretty it renders: a phone on
// open water is good to roughly 3-5 m, so cells finer than that would imply a
// precision the fix does not have.
export const CELL_M = 5;

// Fixes worse than this are discarded rather than recorded. A 30 m fix would
// paint a swathe of lake the boat never touched, and a wrong "proven" mark is
// far more dangerous than a missing one.
export const MAX_ACCURACY_M = 12;

// Half the hull width. The honest claim per pass is the width of the boat --
// NOT the width of the GPS error circle, because we do not know which side of
// that circle the boat was actually on.
export const SWATH_HALF_M = 1.5;

// On plane the boat draws less than at idle, so a fast pass proves LESS depth,
// not more. Passes above this are recorded but flagged, so the display can say
// so rather than quietly overstating what was proven.
export const PLANING_MS = 6.0;

/**
 * Accumulated coverage grid, keyed by cell.
 *
 * Stored as a flat Map of "col,row" -> record so it serialises straight to
 * IndexedDB and stays cheap to merge across trips.
 */
export class SweptGrid {
  constructor(cell = CELL_M) {
    this.cell = cell;
    this.cells = new Map();
  }

  key(x, y) {
    return `${Math.floor(x / this.cell)},${Math.floor(y / this.cell)}`;
  }

  /** Record one accepted fix. Returns false if the fix was too poor to use. */
  addFix(x, y, accuracy, speed, t) {
    if (!(accuracy <= MAX_ACCURACY_M)) return false;
    const k = this.key(x, y);
    const prev = this.cells.get(k);
    const planing = speed > PLANING_MS;
    // Keep the SLOWEST pass through a cell: it is the one that proves the most
    // depth. Overwriting it with a later fast pass would weaken a claim we had
    // already earned.
    if (!prev || (prev.planing && !planing)) {
      this.cells.set(k, { t, accuracy, speed, planing });
    } else if (t > prev.t && prev.planing === planing) {
      // Same quality of evidence, fresher date -- worth keeping for staleness.
      this.cells.set(k, { t, accuracy, speed, planing });
    }
    return true;
  }

  /**
   * Record a leg between two fixes, filling the cells the boat passed through.
   *
   * Without this a fast boat at 1 Hz leaves a dotted line -- at 12 m/s the gaps
   * between fixes are wider than a cell, so most of the water actually driven
   * would read as unknown.
   */
  addLeg(x0, y0, x1, y1, accuracy, speed, t) {
    if (!(accuracy <= MAX_ACCURACY_M)) return 0;
    const d = Math.hypot(x1 - x0, y1 - y0);
    // A leg longer than this is a gap in logging, not a leg. Filling it would
    // invent a straight run the boat may never have made.
    if (d > 120) return 0;
    const n = Math.max(1, Math.ceil(d / (this.cell * 0.5)));
    let added = 0;
    for (let i = 0; i <= n; i++) {
      const f = i / n;
      if (this.addFix(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, accuracy, speed, t)) {
        added++;
      }
    }
    return added;
  }

  has(x, y) {
    return this.cells.has(this.key(x, y));
  }

  get(x, y) {
    return this.cells.get(this.key(x, y)) || null;
  }

  get size() {
    return this.cells.size;
  }

  /** Square metres proven. */
  areaM2() {
    return this.cells.size * this.cell * this.cell;
  }

  /** Cells whose evidence predates a given time, e.g. before a drawdown. */
  staleCount(beforeT) {
    let n = 0;
    for (const c of this.cells.values()) if (c.t < beforeT) n++;
    return n;
  }

  toJSON() {
    return { cell: this.cell, cells: [...this.cells.entries()] };
  }

  static fromJSON(o) {
    const g = new SweptGrid(o.cell || CELL_M);
    for (const [k, v] of o.cells || []) g.cells.set(k, v);
    return g;
  }
}

/**
 * Build a grid from logged fixes.
 *
 * @param {Array<{x:number,y:number,accuracy:number,speed:number,t:number}>} fixes
 *        in time order
 */
export function sweptFromFixes(fixes, cell = CELL_M) {
  const g = new SweptGrid(cell);
  let prev = null;
  for (const f of fixes) {
    if (prev && f.t - prev.t < 30000) {
      g.addLeg(prev.x, prev.y, f.x, f.y, f.accuracy, f.speed ?? 0, f.t);
    } else {
      g.addFix(f.x, f.y, f.accuracy, f.speed ?? 0, f.t);
    }
    if (f.accuracy <= MAX_ACCURACY_M) prev = f;
  }
  return g;
}

/**
 * How much of the lake has been proven, and how long the rest would take.
 *
 * The honest framing for the user. At a 2.5 m swath, proving 34.5 km2 means
 * roughly 11,500 km of driving, which is never going to happen -- so the number
 * that matters is not "percent of lake" but whether the water he actually uses
 * is covered.
 */
export function coverageStats(grid, lakeAreaM2) {
  const proven = grid.areaM2();
  const swath = SWATH_HALF_M * 2;
  return {
    provenM2: proven,
    pctOfLake: lakeAreaM2 ? (proven / lakeAreaM2) * 100 : 0,
    kmDriven: proven / swath / 1000,
    kmToFinish: lakeAreaM2 ? (lakeAreaM2 - proven) / swath / 1000 : 0,
  };
}
