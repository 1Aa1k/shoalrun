// How tall the page actually is, according to the browser rather than to CSS.
//
// Safari on iOS puts its address bar at the bottom of the screen, over the
// bottom of the page, and CSS viewport units only approximate where that leaves
// off. `visualViewport.height` is not an approximation: it is the height of the
// part of the page the user can actually see, excluding the browser's own
// chrome, and it updates as that chrome moves.
//
// This matters more here than on an ordinary page because everything this app
// puts at the bottom is a control -- the tab bar, the legend, the helm buttons,
// the drive pad in boat mode. On a page you scroll, a bar over the last 50 px
// is a nuisance. Here it is the difference between being able to steer and not.

/**
 * Whether a viewport resize should be applied to the layout.
 *
 * The on-screen keyboard shrinks the visual viewport too, and reflowing the
 * whole app -- resizing a WebGL canvas, re-fitting the map -- because somebody
 * tapped into the lake code field is both ugly and slow. The keyboard is
 * temporary and nothing behind it needs to move.
 *
 * @param {string|undefined} activeTagName document.activeElement's tagName
 */
export function shouldApplyResize(activeTagName) {
  return !/^(INPUT|TEXTAREA|SELECT)$/.test(activeTagName || "");
}

/**
 * Publish the visible height as `--vvh` on the root element, and keep it
 * current. Returns the update function, or null where the API is missing --
 * in which case the CSS fallback (100svh) is already in effect.
 */
export function trackVisibleHeight(win = window, doc = document) {
  const vv = win.visualViewport;
  if (!vv) return null;

  const apply = () => {
    if (!shouldApplyResize(doc.activeElement && doc.activeElement.tagName)) return;
    doc.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
  };

  vv.addEventListener("resize", apply);
  // Safari moves the bar on scroll, and the page itself never scrolls, so this
  // is the event that fires when the bar slides in and out.
  vv.addEventListener("scroll", apply);
  // A rotate changes it too, and does not always fire a visualViewport resize.
  win.addEventListener("orientationchange", () => setTimeout(apply, 250));

  apply();
  return apply;
}
