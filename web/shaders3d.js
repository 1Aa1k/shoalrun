// GLSL for the 3D viewer, kept apart from the renderer because it is the part
// that has to be read as shader code rather than skimmed as JavaScript.
//
// Sources are built by a function rather than declared as constants because the
// precision qualifier is a RUNTIME decision. A vertex shader defaults to highp;
// a fragment shader declaring mediump silently disagrees about any shared
// uniform or varying, which links on SwiftShader and hard-fails on AMD/ANGLE
// with "declared as type float16_t and type float". Substituting one value into
// both stages is what makes that mismatch impossible rather than merely absent.

import { gerstnerGLSL } from "./scene3d.js";

export function shaderSources(P) {
  const GERSTNER = gerstnerGLSL();
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

    // Rocks need the vertical exaggeration applied to WHERE they sit but not to
    // HOW BIG they are. Scaling a boulder's height 30x along with the terrain
    // turned every one of them into a needle -- which is what they looked like,
    // and no amount of better geometry would have fixed it. aBase carries the
    // un-exaggerated bottom the rock stands on, so the base follows the seabed
    // up and the rock keeps its true proportions on top of it.
    rock_vert: `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute float aShade;
attribute float aBase;
uniform mat4 uMVP;
uniform float uExag;
varying ${P} vec3 vNormal;
varying ${P} float vShade;
void main() {
  vNormal = aNormal;
  vShade = aShade;
  float y = aBase * uExag + (aPos.y - aBase);
  gl_Position = uMVP * vec4(aPos.x, y, aPos.z, 1.0);
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
    // Coarse lake-wide surface. Runs the same wave function at low
    // tessellation: at distance the waves are sub-pixel anyway, and this only
    // has to agree in colour and rough height with the fine patch in front.
    water_vert: `
precision highp float;
attribute vec3 aPos;
attribute float aDepth;
uniform mat4 uMVP;
uniform float uTime;
uniform float uSwell;
uniform float uWave;
varying ${P} vec2 vXZ;
varying ${P} float vShore;
varying ${P} vec3 vNrm;
varying ${P} float vFoam;
varying ${P} float vEdge;
${GERSTNER}
void main() {
  vEdge = 1.0;
  // Waves die out in the shallows. The surface sits at y=0 and the shelf under
  // a 0 ft block sits at y=0 too, so an unattenuated wave dips through the
  // bottom and the depth test punches holes in the water.
  float fade = smoothstep(0.0, 8.0, aDepth) * uSwell * uWave;
  vec3 disp; vec3 nrm; float crest;
  gerstner(aPos.xz, uTime, fade, disp, nrm, crest);
  vXZ = aPos.xz + disp.xz;
  vShore = smoothstep(0.0, 8.0, aDepth);
  vNrm = nrm;
  vFoam = crest;
  // Held just clear of the bottom regardless, so the two surfaces cannot
  // z-fight where the shelf comes all the way up to the waterline.
  gl_Position = uMVP * vec4(aPos.x + disp.x, disp.y + 0.06, aPos.z + disp.z, 1.0);
}`,

    // Fine camera-following patch. Local coordinates plus a snapped centre --
    // snapping is what stops the grid swimming beneath the waves as you move.
    waterpatch_vert: `
precision highp float;
attribute vec3 aPos;
attribute float aDepth;
uniform mat4 uMVP;
uniform float uTime;
uniform vec2 uCentre;
uniform float uWave;
varying ${P} vec2 vXZ;
varying ${P} float vShore;
varying ${P} vec3 vNrm;
varying ${P} float vFoam;
varying ${P} float vEdge;
${GERSTNER}
uniform float uHalf;
void main() {
  // Faded at the rim. Without it the boundary between the fine patch and the
  // coarse surface behind it is a hard line across the lake, because the two
  // carry the same wave at different tessellations.
  vEdge = 1.0 - smoothstep(0.72, 1.0, max(abs(aPos.x), abs(aPos.z)) / uHalf);
  vec2 world = aPos.xz + uCentre;
  float fade = smoothstep(0.0, 8.0, aDepth) * uWave;
  vec3 disp; vec3 nrm; float crest;
  gerstner(world, uTime, fade, disp, nrm, crest);
  vXZ = world + disp.xz;
  vShore = fade;
  vNrm = nrm;
  vFoam = crest;
  gl_Position = uMVP * vec4(world.x + disp.x, disp.y + 0.06, world.y + disp.z, 1.0);
}`,

    water_frag: `
precision ${P} float;
varying ${P} vec2 vXZ;
varying ${P} float vShore;
varying ${P} vec3 vNrm;
varying ${P} float vFoam;
varying ${P} float vEdge;
uniform float uTime;
uniform float uAlpha;
uniform float uWave;
void main() {
  vec3 n = normalize(vNrm);
  vec3 light = normalize(vec3(0.45, 0.8, 0.4));

  // Deep water is dark and blue; the faces tilted toward the sky pick up its
  // colour, which is most of what makes water read as water rather than paint.
  float sky = clamp(n.y * 0.5 + 0.5, 0.0, 1.0);
  vec3 deep = vec3(0.043, 0.153, 0.239);
  vec3 shallow = vec3(0.176, 0.408, 0.463);
  vec3 base = mix(deep, shallow, vShore * 0.5);
  base = mix(base, vec3(0.298, 0.478, 0.573), pow(1.0 - sky, 2.0) * 0.6);

  float spec = pow(max(dot(n, light), 0.0), 48.0);

  // Foam on the crests only, and only where there is enough water to have
  // made one. Threshold is on the summed wave height, so it appears along the
  // tops of the swells rather than as an even scum.
  // Only the very tops. A low threshold puts foam over most of the swell and
  // the surface reads as milky rather than as water with whitecaps on it.
  float foam = smoothstep(0.24 * uWave, 0.36 * uWave, vFoam) * vShore;
  vec3 col = mix(base + spec * 0.7, vec3(0.88, 0.93, 0.96), foam * 0.8);

  // Thinner over the shallows, the way real water is -- and it keeps the
  // hazards there visible instead of veiling the ones that matter most.
  gl_FragColor = vec4(col, uAlpha * (0.5 + 0.5 * vShore) * vEdge);
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
