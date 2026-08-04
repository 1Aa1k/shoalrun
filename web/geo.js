// Local flat-earth projection. Over a 10 km lake the error from treating
// lat/lon as a plane (scaled for longitude convergence) is centimetres, and it
// avoids shipping a projection library to a boat with no signal.

export function makeProjection(centerLat, centerLon) {
  const R = 6378137;
  const latRad = (centerLat * Math.PI) / 180;
  const mPerDegLat = (Math.PI / 180) * R;
  const mPerDegLon = mPerDegLat * Math.cos(latRad);

  return {
    // lon/lat -> metres east/north of the lake centre
    fwd(lon, lat) {
      return [(lon - centerLon) * mPerDegLon, (lat - centerLat) * mPerDegLat];
    },
    inv(x, y) {
      return [centerLon + x / mPerDegLon, centerLat + y / mPerDegLat];
    },
    mPerDegLat,
    mPerDegLon,
  };
}

// Great-circle distance in metres. Used for alert ranges, where being wrong by
// a metre matters more than it does for drawing.
export function haversine(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const p = Math.PI / 180;
  const dLat = (lat2 - lat1) * p;
  const dLon = (lon2 - lon1) * p;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * p) * Math.cos(lat2 * p) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// Shortest distance from point P to segment AB, all in projected metres.
export function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

// Uniform grid bucket index. The candidate count is in the hundreds, so this
// is not about asymptotics -- it is about keeping the per-GPS-fix scan bounded
// and predictable so the phone does not heat up over a long day on the water.
export class GridIndex {
  constructor(cellSize = 200) {
    this.cell = cellSize;
    this.buckets = new Map();
  }

  _key(x, y) {
    return `${Math.floor(x / this.cell)},${Math.floor(y / this.cell)}`;
  }

  insert(x, y, item) {
    const k = this._key(x, y);
    let b = this.buckets.get(k);
    if (!b) this.buckets.set(k, (b = []));
    b.push(item);
  }

  // Everything within `radius` metres of (x,y), plus slop at the cell edges.
  query(x, y, radius) {
    const out = [];
    const r = Math.ceil(radius / this.cell);
    const cx = Math.floor(x / this.cell);
    const cy = Math.floor(y / this.cell);
    for (let i = cx - r; i <= cx + r; i++) {
      for (let j = cy - r; j <= cy + r; j++) {
        const b = this.buckets.get(`${i},${j}`);
        if (b) out.push(...b);
      }
    }
    return out;
  }
}
