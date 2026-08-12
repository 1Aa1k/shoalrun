// Tab switching for the single-page shell.
//
// The map, the 3D bottom and the info page were three HTML files. Handing
// someone three URLs and telling them which one to open on the water is how a
// tool ends up unused, so they are one page now and this is the only thing that
// decides which of them you are looking at.
//
// Two rules the rest of the app leans on:
//
//   1. Only the visible view animates. Both the map and the 3D scene run a
//      requestAnimationFrame loop; leaving both spinning on a phone that is
//      already holding a GPS watch and a wake lock is a battery bill for frames
//      nobody sees. Each loop asks `isActive()` before it draws.
//
//   2. The 3D view initialises on first show, not on load. Its module grabs a
//      WebGL context and builds a 156k-triangle mesh at import time; doing that
//      during boot would stall the map -- the view somebody actually opened --
//      for a second on a phone, and would cost a WebGL context on devices that
//      never open the 3D tab at all. A canvas sized 0x0 also cannot be
//      initialised, so it has to happen after the section is displayed.

const VIEWS = ["map", "3d", "info"];

let current = "map";
let onFirstShow = {};

/** Which view is on screen. Cheap enough to call every frame. */
export function activeView() {
  return current;
}

export function isActive(name) {
  return current === name;
}

/**
 * @param {Record<string, () => void>} firstShow callbacks run once, the first
 *   time their view becomes visible, after it has been given a size.
 */
export function initViews(firstShow = {}) {
  onFirstShow = { ...firstShow };
  const tabs = document.getElementById("tabs");

  tabs.querySelectorAll("button").forEach((b) => {
    b.onclick = () => show(b.dataset.view);
  });

  // The hash carries either a view name or a map position. A position implies
  // the map, which is what applyHash in app.js already assumes.
  const wanted = (location.hash || "").slice(1);
  if (VIEWS.includes(wanted)) show(wanted);
  else show("map");

  addEventListener("hashchange", () => {
    const h = (location.hash || "").slice(1);
    if (VIEWS.includes(h)) show(h);
    else if (h) show("map");
  });
}

export function show(name) {
  if (!VIEWS.includes(name)) return;
  current = name;

  for (const v of VIEWS) {
    document.getElementById(`view-${v}`).classList.toggle("active", v === name);
  }
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.view === name);
  });

  // Close any drawer belonging to the view being left; coming back to a view
  // with a panel still half over the map is disorienting and reads as a bug.
  document.querySelectorAll(".drawer.open").forEach((d) => {
    if (!document.getElementById(`view-${name}`).contains(d)) d.classList.remove("open");
  });

  const once = onFirstShow[name];
  if (once) {
    delete onFirstShow[name];
    // A frame of slack so the section is laid out and the canvas has a size
    // before anything measures it.
    requestAnimationFrame(() => once());
  }

  // Only rewrite the hash when it is a view name. A map position in the hash is
  // somebody's deep link and stomping it would break the back button.
  const h = (location.hash || "").slice(1);
  if (VIEWS.includes(h) || !h) {
    history.replaceState(null, "", name === "map" ? location.pathname + location.search : `#${name}`);
  }
}
