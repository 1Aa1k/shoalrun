// GLSL for the 3D viewer, kept apart from the renderer because it is the part
// that has to be read as shader code rather than skimmed as JavaScript.
//
// Sources are built by a function rather than declared as constants because the
// precision qualifier is a RUNTIME decision. A vertex shader defaults to highp;
// a fragment shader declaring mediump silently disagrees about any shared
// uniform or varying, which links on SwiftShader and hard-fails on AMD/ANGLE
// with "declared as type float16_t and type float". Substituting one value into
// both stages is what makes that mismatch impossible rather than merely absent.

export function shaderSources(P) {
  return {
    bottom_vert: `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute float aDepth;
uniform mat4 uMVP;
uniform float uExag;
varying ${P} float vDepth;
varying ${P} vec3 vNormal;
void main() {
  vDepth = aDepth;
  // Exaggerating Y steepens the true slope, so the normal has to be corrected
  // the opposite way or the lighting stays flat while the terrain gets sharper.
  // The risers are already vertical, and scaling leaves them vertical.
  vNormal = normalize(vec3(aNormal.x * uExag, aNormal.y, aNormal.z * uExag));
  gl_Position = uMVP * vec4(aPos.x, aPos.y * uExag, aPos.z, 1.0);
}`,

    bottom_frag: `
precision ${P} float;
varying ${P} float vDepth;
varying ${P} vec3 vNormal;
uniform float uShallow;

// Same ramp as the 2D map, so the two views agree about what 15 ft looks like.
vec3 ramp(float ft) {
  vec3 c;
  if (ft < 3.0)       c = mix(vec3(0.494,0.769,0.698), vec3(0.408,0.698,0.690), ft / 3.0);
  else if (ft < 6.0)  c = mix(vec3(0.408,0.698,0.690), vec3(0.282,0.580,0.675), (ft-3.0)/3.0);
  else if (ft < 10.0) c = mix(vec3(0.282,0.580,0.675), vec3(0.180,0.463,0.635), (ft-6.0)/4.0);
  else if (ft < 15.0) c = mix(vec3(0.180,0.463,0.635), vec3(0.125,0.369,0.573), (ft-10.0)/5.0);
  else if (ft < 25.0) c = mix(vec3(0.125,0.369,0.573), vec3(0.086,0.282,0.478), (ft-15.0)/10.0);
  else if (ft < 40.0) c = mix(vec3(0.086,0.282,0.478), vec3(0.063,0.212,0.376), (ft-25.0)/15.0);
  else if (ft < 60.0) c = mix(vec3(0.063,0.212,0.376), vec3(0.043,0.157,0.290), (ft-40.0)/20.0);
  else                c = mix(vec3(0.043,0.157,0.290), vec3(0.031,0.118,0.227), min((ft-60.0)/20.0, 1.0));
  return c;
}

void main() {
  vec3 base = ramp(vDepth);
  if (uShallow > 0.0 && vDepth <= uShallow) base = mix(base, vec3(1.0, 0.72, 0.25), 0.55);
  vec3 n = normalize(vNormal);
  float diff = max(dot(n, normalize(vec3(0.45, 0.8, 0.4))), 0.0);
  // Ambient floor is high: this is a bottom surface being read for shape, and
  // a physically honest light would put half the basin in unreadable shadow.
  gl_FragColor = vec4(base * (0.5 + 0.7 * diff), 1.0);
}`,

// Land, trees, rocks and the hull all shade the same way: a normal, a per-vertex
// brightness, and a flat base colour from a uniform.
    lit_vert: `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute float aShade;
uniform mat4 uMVP;
uniform float uExag;
varying ${P} vec3 vNormal;
varying ${P} float vShade;
void main() {
  vNormal = aNormal;
  vShade = aShade;
  gl_Position = uMVP * vec4(aPos.x, aPos.y * uExag, aPos.z, 1.0);
}`,

    lit_frag: `
precision ${P} float;
varying ${P} vec3 vNormal;
varying ${P} float vShade;
uniform vec3 uColorA;
uniform vec3 uColorB;
void main() {
  vec3 n = normalize(vNormal);
  float diff = max(dot(n, normalize(vec3(0.45, 0.8, 0.4))), 0.0);
  // vShade doubles as a blend between two tones, which is what stops a few
  // thousand identical cones reading as a single green mass.
  vec3 base = mix(uColorA, uColorB, clamp(vShade - 0.6, 0.0, 1.0));
  gl_FragColor = vec4(base * (0.45 + 0.75 * diff), 1.0);
}`,

    flat_vert: `
precision highp float;
attribute vec3 aPos;
uniform mat4 uMVP;
uniform float uY;
uniform float uExag;
void main() {
  gl_Position = uMVP * vec4(vec3(aPos.x, aPos.y * uExag + uY, aPos.z), 1.0);
}`,

    flat_frag: `
precision ${P} float;
uniform vec4 uColor;
void main() { gl_FragColor = uColor; }`,

// Water. Long swell displaces the surface; the chop that actually reads as
// water is done per-pixel, because the quads are 25 m across and geometry that
// coarse cannot carry a 4 m wave.
    water_vert: `
precision highp float;
attribute vec3 aPos;
uniform mat4 uMVP;
uniform float uTime;
varying ${P} vec2 vXZ;
void main() {
  float swell =
    sin(aPos.x * 0.013 + uTime * 0.55) * 0.13 +
    sin(aPos.z * 0.009 - uTime * 0.41) * 0.10;
  vXZ = aPos.xz;
  gl_Position = uMVP * vec4(aPos.x, swell, aPos.z, 1.0);
}`,

    water_frag: `
precision ${P} float;
varying ${P} vec2 vXZ;
uniform float uTime;
uniform float uAlpha;
void main() {
  // Two crossed wave trains, differentiated analytically into a surface normal.
  // Cheaper than a normal map and it needs no texture to ship.
  float a = vXZ.x * 0.22 + uTime * 1.3;
  float b = vXZ.y * 0.17 - uTime * 1.05;
  float c = (vXZ.x + vXZ.y) * 0.09 + uTime * 0.6;
  vec3 n = normalize(vec3(
    cos(a) * 0.05 + cos(c) * 0.03,
    1.0,
    cos(b) * 0.045 + cos(c) * 0.03
  ));
  vec3 light = normalize(vec3(0.45, 0.8, 0.4));
  float spec = pow(max(dot(n, light), 0.0), 24.0);
  vec3 base = mix(vec3(0.129, 0.310, 0.416), vec3(0.216, 0.451, 0.553), n.x * 3.0 + 0.5);
  gl_FragColor = vec4(base + spec * 0.55, uAlpha);
}`,

    sky_vert: `
precision highp float;
attribute vec2 aXY;
varying ${P} float vY;
void main() { vY = aXY.y * 0.5 + 0.5; gl_Position = vec4(aXY, 0.0, 1.0); }`,

    sky_frag: `
precision ${P} float;
varying ${P} float vY;
void main() {
  // Light band held low and wide: the horizon sits near the middle of the
  // screen at boat pitch, and that is exactly where it has to be readable.
  vec3 low  = vec3(0.482, 0.553, 0.596);
  vec3 high = vec3(0.098, 0.145, 0.216);
  gl_FragColor = vec4(mix(low, high, smoothstep(0.28, 1.0, vY)), 1.0);
}`,
  };
}
