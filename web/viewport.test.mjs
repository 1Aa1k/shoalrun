import test from "node:test";
import assert from "node:assert/strict";

import { shouldApplyResize, trackVisibleHeight } from "./viewport.js";

// Safari on iOS puts its address bar at the bottom, over the bottom of the
// page, and everything this app puts down there is a control: the tab bar, the
// legend, the helm buttons, the drive pad. Getting the height wrong does not
// make the page ugly, it makes it unsteerable.

function stubs(height = 700) {
  const listeners = {};
  const root = { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } };
  const doc = { documentElement: root, activeElement: { tagName: "BODY" } };
  const win = {
    visualViewport: {
      height,
      addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn); },
    },
    addEventListener: (ev, fn) => { (listeners[ev] ||= []).push(fn); },
  };
  return { win, doc, root, listeners, fire: (ev) => (listeners[ev] || []).forEach((f) => f()) };
}

test("the visible height is published immediately, not on the first scroll", () => {
  const s = stubs(652);
  trackVisibleHeight(s.win, s.doc);
  assert.equal(s.root.style.props["--vvh"], "652px");
});

test("it follows the bar as it slides in and out", () => {
  const s = stubs(652);
  trackVisibleHeight(s.win, s.doc);
  s.win.visualViewport.height = 745;
  s.fire("scroll");
  assert.equal(s.root.style.props["--vvh"], "745px");
});

// The on-screen keyboard shrinks the visual viewport too. Reflowing the whole
// app -- resizing a WebGL canvas, re-fitting the map -- because somebody tapped
// into the lake code field is slow and pointless; the keyboard is temporary and
// nothing behind it needs to move.
test("the keyboard does not resize the app", () => {
  const s = stubs(652);
  trackVisibleHeight(s.win, s.doc);
  s.doc.activeElement = { tagName: "INPUT" };
  s.win.visualViewport.height = 320;
  s.fire("resize");
  assert.equal(s.root.style.props["--vvh"], "652px");
});

test("and the layout comes back once the field is left", () => {
  const s = stubs(652);
  trackVisibleHeight(s.win, s.doc);
  s.doc.activeElement = { tagName: "INPUT" };
  s.win.visualViewport.height = 320;
  s.fire("resize");
  s.doc.activeElement = { tagName: "BODY" };
  s.win.visualViewport.height = 652;
  s.fire("resize");
  assert.equal(s.root.style.props["--vvh"], "652px");
});

test("shouldApplyResize covers the text-entry elements and nothing else", () => {
  for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
    assert.equal(shouldApplyResize(tag), false, tag);
  }
  for (const tag of ["BODY", "CANVAS", "BUTTON", undefined]) {
    assert.equal(shouldApplyResize(tag), true, String(tag));
  }
});

// Desktop Safari before 13, and any environment without the API, must not throw
// -- the CSS fallback is already correct there.
test("a browser without visualViewport is left to the CSS", () => {
  const s = stubs();
  delete s.win.visualViewport;
  assert.equal(trackVisibleHeight(s.win, s.doc), null);
  assert.equal(s.root.style.props["--vvh"], undefined);
});
