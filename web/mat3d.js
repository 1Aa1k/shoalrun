// Matrix and vector helpers for the 3D viewer. Column of small pure functions,
// pulled out of the renderer so that file stays about rendering.
//
// Convention: row-vector, so a point is transformed as p * M, and composing
// model then view then projection is multiply(model, multiply(view, proj)).

export function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  return [
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) / (near - far), -1,
    0, 0, (2 * far * near) / (near - far), 0,
  ];
}

export function multiply(a, b) {
  const o = new Array(16).fill(0);
  for (let i = 0; i < 4; i++) {
    for (let j = 0; j < 4; j++) {
      for (let k = 0; k < 4; k++) o[i * 4 + j] += a[i * 4 + k] * b[k * 4 + j];
    }
  }
  return o;
}

export const sub3 = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const add3 = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
export const dot3 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross3 = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
export function norm3(v) {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

export function lookAt(eye, target, up) {
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
export function forwardOf(yaw, pitch) {
  return [
    Math.sin(yaw) * Math.cos(pitch),
    Math.sin(pitch),
    -Math.cos(yaw) * Math.cos(pitch),
  ];
}

// Model rotation for something that faces `yaw`. Rows are the images of the
// local axes, so the bow -- local -Z, to agree with forwardOf -- lands exactly
// on forwardOf(yaw). The transpose of this is the inverse rotation and yaws the
// model by MINUS the heading: identical at due north, a boat sliding sideways
// everywhere else, which is why it survived a visual check once already.
export function modelYaw(yaw) {
  const c = Math.cos(yaw);
  const s = Math.sin(yaw);
  return [c, 0, s, 0, 0, 1, 0, 0, -s, 0, c, 0, 0, 0, 0, 1];
}
