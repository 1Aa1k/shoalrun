/*
 * A guided tour: a dimmed page with a hole cut around one real control, and a
 * card next to it saying what that control is for.
 *
 * No dependencies and no build step, because the three apps this ships into are
 * a single-file offline PWA, a Vite/React app and a server-rendered page, and
 * the only thing all three agree on is a script tag.
 *
 * Usage:
 *
 *   Tour.start([
 *     { target: '#solve', title: 'Solve', body: 'Builds the whole year.' },
 *     { target: '.rules', title: 'Rules', body: '...', before: () => openTab('rules') },
 *   ], { id: 'rota' });
 *
 * `Tour.auto(steps, opts)` is the usual entry point: it runs on `?tour=1`, and
 * otherwise only on a first visit (remembered in localStorage under opts.id).
 *
 * Step fields:
 *   target   CSS selector for the element to point at. Omit for a centred card
 *            with no spotlight. Right for an opening or closing step.
 *   title    Short heading.
 *   body     One or two sentences. Plain text; no HTML is injected.
 *   before   Optional. Called before the step shows, to switch a tab, open a
 *            panel, scroll something into view. May return a promise.
 *   place    'auto' (default) | 'top' | 'bottom' | 'left' | 'right' | 'center'
 *   pad      Spotlight padding in px around the target. Default 8.
 *   wait     ms to wait for `target` to appear before giving up. Default 2000.
 *
 * A step whose target never appears is skipped rather than shown pointing at
 * nothing. Apps change, and a tour that hard-fails on a renamed button is
 * worse than a tour that is one step shorter.
 */
(function (root) {
  'use strict';

  var NS = 'sr-tour';
  var MOBILE = 560;

  /* Every selector is anchored on an id, so a host stylesheet cannot reach in.
   * This is not neatness: shoalrun styles `body.chart button` (0,1,1), which
   * outranks any class selector here and turned the tour's buttons the colour
   * of its own chart mode. An id wins that outright without !important, and
   * !important is worse to debug when the next app disagrees. */
  var CSS = [
    '#' + NS + '{position:fixed;top:0;right:0;bottom:0;left:0;z-index:2147483000;',
    'pointer-events:none;',
    'font:400 16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}',
    '#' + NS + ' .t-mask{position:absolute;top:0;right:0;bottom:0;left:0;pointer-events:auto;}',
    '#' + NS + ' .t-mask svg{position:absolute;top:0;left:0;width:100%;height:100%;display:block;}',
    '#' + NS + ' .t-ring{position:fixed;border:2px solid #fff;border-radius:6px;background:none;',
    'pointer-events:none;box-shadow:0 0 0 1px rgba(0,0,0,.55);transition:all .28s cubic-bezier(.4,0,.2,1);}',
    '#' + NS + ' .t-card{position:fixed;width:390px;max-width:calc(100vw - 32px);pointer-events:auto;',
    'background:#111;color:#fff;border:1px solid #3d3d3d;border-radius:10px;padding:22px 22px 18px;',
    'box-shadow:0 18px 50px rgba(0,0,0,.6);font-family:inherit;text-align:left;',
    'transition:top .28s cubic-bezier(.4,0,.2,1),left .28s cubic-bezier(.4,0,.2,1);}',
    '#' + NS + ' .t-card h3{margin:0 0 9px;font-family:inherit;font-size:20px;font-weight:600;',
    'line-height:1.3;letter-spacing:-.01em;color:#fff;text-transform:none;}',
    '#' + NS + ' .t-card p{margin:0;color:#e2e2e2;font-family:inherit;font-size:17px;',
    'font-weight:400;line-height:1.62;text-transform:none;}',
    '#' + NS + ' .t-foot{display:flex;align-items:center;gap:10px;margin-top:20px;border:0;padding:0;background:none;}',
    '#' + NS + ' .t-count{font-family:inherit;font-size:13px;font-weight:600;line-height:1;',
    'letter-spacing:.14em;color:#9c9c9c;',
    'font-variant-numeric:tabular-nums;text-transform:uppercase;margin-right:auto;}',
    '#' + NS + ' button{appearance:none;font-family:inherit;font-size:15.5px;font-weight:600;',
    'line-height:1;letter-spacing:normal;',
    'padding:12px 20px;border-radius:6px;cursor:pointer;border:1px solid #4a4a4a;',
    'background:#111;color:#dcdcdc;text-transform:none;box-shadow:none;height:auto;width:auto;}',
    '#' + NS + ' button:hover{color:#fff;border-color:#666;background:#1c1c1c;}',
    '#' + NS + ' button.t-go{background:#fff;color:#000;border-color:#fff;}',
    '#' + NS + ' button.t-go:hover{background:#e2e2e2;border-color:#e2e2e2;color:#000;}',
    '#' + NS + ' button.t-skip{position:absolute;top:10px;right:10px;width:36px;height:36px;padding:0;',
    'border:0;background:none;color:#a8a8a8;font-size:24px;font-weight:400;border-radius:5px;}',
    '#' + NS + ' button.t-skip:hover{color:#fff;background:#242424;border:0;}',
    '#' + NS + '-launch{position:fixed;right:16px;bottom:16px;z-index:2147482000;',
    'font:600 14.5px/1 ui-sans-serif,system-ui,-apple-system,sans-serif;letter-spacing:.08em;',
    'text-transform:uppercase;padding:14px 22px;border-radius:999px;cursor:pointer;height:auto;',
    'touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;',
    'background:#fff;color:#000;border:0;box-shadow:0 6px 22px rgba(0,0,0,.45);}',
    '#' + NS + '-launch:hover{background:#e2e2e2;color:#000;}',
    '@media (max-width:' + MOBILE + 'px){',
    '#' + NS + ' .t-card{width:auto;left:12px;right:12px;max-width:none;}',
    '#' + NS + '-launch{right:12px;bottom:12px;}}',
    '@media (prefers-reduced-motion:reduce){#' + NS + ' .t-ring,#' + NS + ' .t-card{transition:none;}}'
  ].join('');

  var state = null;

  /*
   * The visible viewport, which on a phone is not `window.innerHeight`.
   *
   * Safari on iOS puts its address bar at the BOTTOM of the screen, over the
   * bottom of the page, and innerHeight reports the layout viewport underneath
   * it. Anything positioned from the bottom with that number lands behind the
   * browser's own chrome. shoalrun already shipped that bug once and its notes
   * are explicit that `env(safe-area-inset-bottom)` does not rescue it: that
   * reports the home indicator, not the toolbar.
   *
   * visualViewport is the browser's own measurement of what is actually on
   * screen, so it is the only number that is right during a pinch-zoom, with a
   * keyboard up, or under that address bar. innerHeight is the fallback for
   * anything too old to have it.
   */
  function vv() {
    return window.visualViewport || null;
  }
  function vpW() {
    var v = vv();
    return (v && v.width) || window.innerWidth;
  }
  function vpH() {
    var v = vv();
    return (v && v.height) || window.innerHeight;
  }
  /* How far the visible viewport's top sits below the layout viewport's top.
   * A fixed element is placed against the layout viewport, so every coordinate
   * handed to one has to be shifted by this. */
  function vpTop() {
    var v = vv();
    return (v && v.offsetTop) || 0;
  }

  function el(tag, cls) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  function injectCss() {
    if (document.getElementById(NS + '-css')) return;
    var s = el('style');
    s.id = NS + '-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* An element counts as found only once it has a box. Tab panels and lazily
   * rendered panels exist in the DOM before they occupy any space, and pointing
   * at a 0x0 rect puts the card in the top-left corner over nothing. */
  function visible(sel) {
    if (!sel) return null;
    var nodes = document.querySelectorAll(sel);
    for (var i = 0; i < nodes.length; i++) {
      var r = nodes[i].getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return nodes[i];
    }
    return null;
  }

  function waitFor(sel, ms) {
    return new Promise(function (resolve) {
      var found = visible(sel);
      if (found) return resolve(found);
      var stop = Date.now() + (ms || 2000);
      var timer = setInterval(function () {
        var n = visible(sel);
        if (n || Date.now() > stop) {
          clearInterval(timer);
          resolve(n);
        }
      }, 80);
    });
  }

  function scrollIntoView(node) {
    var r = node.getBoundingClientRect();
    var vh = vpH();
    /* A target taller than most of the screen cannot be centred usefully: half
     * of it ends up off one edge, and on a phone the card then covers a good
     * part of the rest. Align its top instead, so what is on screen starts at
     * the beginning of the thing rather than in the middle of it. */
    var block = r.height > vh * 0.55 ? 'start' : 'center';
    if (block === 'center' && r.top >= 0 && r.bottom <= vh) return Promise.resolve();
    if (block === 'start' && r.top >= 0 && r.top < vh * 0.25) return Promise.resolve();
    node.scrollIntoView({ block: block, inline: 'center', behavior: 'smooth' });
    /* No scrollend everywhere yet, so settle on a timer. */
    return new Promise(function (r2) { setTimeout(r2, 380); });
  }

  function build() {
    var root = el('div');
    root.id = NS;

    var mask = el('div', 't-mask');
    mask.innerHTML =
      '<svg><defs><mask id="' + NS + '-hole">' +
      '<rect x="0" y="0" width="100%" height="100%" fill="#fff"/>' +
      '<rect class="t-cut" x="0" y="0" width="0" height="0" rx="6" fill="#000"/>' +
      '</mask></defs>' +
      '<rect x="0" y="0" width="100%" height="100%" fill="rgba(0,0,0,.68)" mask="url(#' + NS + '-hole)"/></svg>';

    var ring = el('div', 't-ring');
    ring.style.display = 'none';

    var card = el('div', 't-card');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-live', 'polite');

    var skip = el('button', 't-skip');
    skip.type = 'button';
    skip.setAttribute('aria-label', 'Close the tour');
    skip.textContent = '\u00d7';

    var h = el('h3');
    var p = el('p');
    var foot = el('div', 't-foot');
    var count = el('span', 't-count');
    var back = el('button');
    back.type = 'button';
    back.textContent = 'Back';
    var next = el('button', 't-go');
    next.type = 'button';
    next.textContent = 'Next';

    foot.appendChild(count);
    foot.appendChild(back);
    foot.appendChild(next);
    card.appendChild(skip);
    card.appendChild(h);
    card.appendChild(p);
    card.appendChild(foot);

    root.appendChild(mask);
    root.appendChild(ring);
    root.appendChild(card);
    document.body.appendChild(root);

    return { root: root, mask: mask, ring: ring, card: card, h: h, p: p,
             count: count, back: back, next: next, skip: skip,
             cut: mask.querySelector('.t-cut') };
  }

  function place(ui, rect, pref) {
    var cw = ui.card.offsetWidth || 320;
    var ch = ui.card.offsetHeight || 160;
    var vw = vpW();
    var vh = vpH();
    var gap = 14;

    var mobile = vw <= MOBILE;

    if (!rect || pref === 'center') {
      /* On a phone the card spans the width via CSS, so only the vertical
       * placement is ours to set; left must be cleared or a previous step's
       * value fights the stylesheet. */
      ui.card.style.left = mobile ? '' : Math.round((vw - cw) / 2) + 'px';
      ui.card.style.top = Math.round((vh - ch) / 2) + 'px';
      return;
    }

    if (mobile) {
      /* The card is nearly the width of the screen, so it cannot sit beside
       * the target: it goes above or below, and the choice is whichever half
       * the target is NOT in. Pinning it to the bottom unconditionally, which
       * is what the stylesheet used to do, covers every control in the lower
       * half of the screen -- including the helm buttons the tour is pointing
       * at on a boat. */
      ui.card.style.left = '';

      /* A target too tall to clear either way cannot be fully uncovered. It is
       * top-aligned by then, so the bottom is the end that hides least of it. */
      if (rect.height > vh - ch - gap * 2 - 24) {
        ui.card.style.top = Math.round(vh - ch - 12) + 'px';
        return;
      }

      var below = rect.top + rect.height / 2 < vh / 2;
      ui.card.style.top = below
        ? Math.round(Math.min(rect.bottom + gap, vh - ch - 12)) + 'px'
        : Math.round(Math.max(rect.top - ch - gap, 12)) + 'px';
      return;
    }

    var order = pref && pref !== 'auto'
      ? [pref, 'bottom', 'top', 'right', 'left']
      : ['bottom', 'top', 'right', 'left'];

    for (var i = 0; i < order.length; i++) {
      var top, left;
      if (order[i] === 'bottom') { top = rect.bottom + gap; left = rect.left + rect.width / 2 - cw / 2; }
      else if (order[i] === 'top') { top = rect.top - ch - gap; left = rect.left + rect.width / 2 - cw / 2; }
      else if (order[i] === 'right') { left = rect.right + gap; top = rect.top + rect.height / 2 - ch / 2; }
      else { left = rect.left - cw - gap; top = rect.top + rect.height / 2 - ch / 2; }

      if (top >= 8 && top + ch <= vh - 8 && left >= 8 && left + cw <= vw - 8) {
        ui.card.style.top = Math.round(top) + 'px';
        ui.card.style.left = Math.round(left) + 'px';
        return;
      }
    }

    /* Nothing fit. Clamp the first choice rather than leaving it off-screen. */
    var t = Math.min(Math.max(rect.bottom + gap, 8), vh - ch - 8);
    var l = Math.min(Math.max(rect.left + rect.width / 2 - cw / 2, 8), vw - cw - 8);
    ui.card.style.top = Math.round(t) + 'px';
    ui.card.style.left = Math.round(l) + 'px';
  }

  function paint(ui, node, pad, pref) {
    if (!node) {
      ui.cut.setAttribute('width', 0);
      ui.cut.setAttribute('height', 0);
      ui.ring.style.display = 'none';
      place(ui, null, 'center');
      return;
    }
    var r = node.getBoundingClientRect();
    var x = r.left - pad, y = r.top - pad;
    var w = r.width + pad * 2, h = r.height + pad * 2;

    ui.cut.setAttribute('x', x);
    ui.cut.setAttribute('y', y);
    ui.cut.setAttribute('width', w);
    ui.cut.setAttribute('height', h);

    ui.ring.style.display = '';
    ui.ring.style.left = x + 'px';
    ui.ring.style.top = y + 'px';
    ui.ring.style.width = w + 'px';
    ui.ring.style.height = h + 'px';

    place(ui, { left: x, top: y, right: x + w, bottom: y + h, width: w, height: h }, pref);
  }

  function show(i) {
    if (!state) return;
    if (i < 0) i = 0;
    if (i >= state.steps.length) return finish(true);

    state.i = i;
    var step = state.steps[i];
    var ui = state.ui;

    /* Resolve the target before writing any of the step's text. Painting first
     * and skipping afterwards means a step whose control is absent is on screen
     * for as long as the lookup takes, with the previous step's ring still
     * drawn beside it, pointing at the wrong thing while it reads like the
     * right one. */
    Promise.resolve(step.before ? step.before() : null)
      .catch(function () { /* a panel that will not open is not worth stopping for */ })
      .then(function () { return step.target ? waitFor(step.target, step.wait) : null; })
      .then(function (node) {
        if (!state || state.i !== i) return;
        if (step.target && !node) {
          /* The control is not in this build, or not in this state. Skip rather
           * than ring empty space: apps change, and a tour that hard-fails on a
           * renamed button is worse than one that is a step shorter. */
          return i + 1 < state.steps.length ? show(i + 1) : finish(true);
        }
        return (node ? scrollIntoView(node) : Promise.resolve()).then(function () {
          if (!state || state.i !== i) return;
          ui.h.textContent = step.title || '';
          ui.p.textContent = step.body || '';
          ui.count.textContent = (i + 1) + ' / ' + state.steps.length;
          ui.back.style.visibility = i === 0 ? 'hidden' : 'visible';
          ui.next.textContent = i === state.steps.length - 1 ? 'Done' : 'Next';
          state.node = node;
          state.pad = step.pad == null ? 8 : step.pad;
          state.place = step.place || 'auto';
          state.rect = null;
          paint(ui, node, state.pad, state.place);
        });
      });
  }

  function reflow() {
    if (!state) return;
    paint(state.ui, state.node, state.pad, state.place);
  }

  /* Resize and scroll events do not cover it. A panel that reflows under its
   * own animation, or a rail that expands when a selection lands, moves the
   * target without firing either, and the ring is then drawn round empty space
   * a few pixels away from the thing it is pointing at. Watching the rect is
   * the only check that catches every cause, and comparing it first means the
   * usual frame does nothing but four number comparisons. */
  function track() {
    if (!state) return;
    state.raf = requestAnimationFrame(track);
    if (!state.node) return;
    var r = state.node.getBoundingClientRect();
    var last = state.rect;
    if (last && r.top === last.top && r.left === last.left &&
        r.width === last.width && r.height === last.height) return;
    state.rect = { top: r.top, left: r.left, width: r.width, height: r.height };
    paint(state.ui, state.node, state.pad, state.place);
  }

  function finish(completed) {
    if (!state) return;
    var s = state;
    state = null;
    if (s.raf) cancelAnimationFrame(s.raf);
    window.removeEventListener('resize', reflow);
    window.removeEventListener('scroll', reflow, true);
    if (vv() && vv().removeEventListener) {
      vv().removeEventListener('resize', reflow);
      vv().removeEventListener('scroll', reflow);
    }
    document.removeEventListener('keydown', s.keys, true);
    if (s.ui.root.parentNode) s.ui.root.parentNode.removeChild(s.ui.root);
    if (s.opts.id) {
      try { localStorage.setItem(NS + ':' + s.opts.id, completed ? 'done' : 'skipped'); } catch (e) {}
    }
    if (typeof s.opts.onEnd === 'function') s.opts.onEnd(completed);
  }

  function start(steps, opts) {
    if (!steps || !steps.length) return;
    if (state) finish(false);
    injectCss();

    var ui = build();
    state = { steps: steps, i: -1, ui: ui, opts: opts || {}, node: null, pad: 8,
              place: 'auto', rect: null, raf: 0 };

    ui.next.addEventListener('click', function () { show(state.i + 1); });
    ui.back.addEventListener('click', function () { show(state.i - 1); });
    ui.skip.addEventListener('click', function () { finish(false); });
    ui.mask.addEventListener('click', function () { finish(false); });

    state.keys = function (e) {
      if (!state) return;
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); show(state.i + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); show(state.i - 1); }
    };
    document.addEventListener('keydown', state.keys, true);
    window.addEventListener('resize', reflow);
    /* Capture phase: the scrolling element is usually an inner pane, not the
     * window, and a bubbling listener never hears about those. */
    window.addEventListener('scroll', reflow, true);
    /* The address bar collapsing or a keyboard opening changes what is visible
     * without firing either of the above. */
    if (vv() && vv().addEventListener) {
      vv().addEventListener('resize', reflow);
      vv().addEventListener('scroll', reflow);
    }

    show(0);
    track();
  }

  function seen(id) {
    try { return !!localStorage.getItem(NS + ':' + id); } catch (e) { return false; }
  }

  function asked() {
    try { return new URLSearchParams(window.location.search).get('tour'); } catch (e) { return null; }
  }

  /*
   * Start when the link says to (`?tour=1`), and otherwise only for somebody who
   * has never seen it. `?tour=0` suppresses it, which is what a screenshot run
   * wants. Always leaves a launcher behind so it can be replayed.
   */
  function auto(steps, opts) {
    opts = opts || {};
    var q = asked();
    var run = q === '1' || q === 'yes' || (q === null && opts.firstVisit !== false && !seen(opts.id));
    if (q === '0' || q === 'no') run = false;

    var go = function () {
      if (opts.launcher !== false) launcher(steps, opts);
      if (run) setTimeout(function () { start(steps, opts); }, opts.delay == null ? 400 : opts.delay);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
    else go();
  }

  /* How much of the layout viewport the browser is currently sitting on top of.
   * On iOS Safari the bottom of the layout viewport is behind the address bar,
   * so anything anchored to `bottom` has to be lifted by this much. Zero on
   * every desktop and on Android. */
  function hiddenPx() {
    return Math.max(0, window.innerHeight - vpH() - vpTop());
  }

  function launcher(steps, opts) {
    if (document.getElementById(NS + '-launch')) return;
    var b = el('button');
    b.id = NS + '-launch';
    b.type = 'button';
    b.textContent = opts.launchLabel || 'Tour';
    injectCss();
    document.body.appendChild(b);

    /* There is no corner that is free on every screen -- bottom-right covered
     * the Info tab on a phone -- so rather than guess a better one, let it be
     * moved and remember where. Stored as a distance from the bottom-right
     * corner rather than as x/y: the lift above still applies unchanged, and a
     * rotation or a keyboard shrinking the viewport pulls the button back in
     * with the corner instead of flinging it off the far edge. */
    var POS = NS + ':pos:' + (opts.id || 'default');
    var pos = null;
    try { pos = JSON.parse(localStorage.getItem(POS) || 'null'); } catch (e) {}
    if (!pos || typeof pos.r !== 'number' || typeof pos.b !== 'number') pos = null;

    /* Clamping writes back into `pos` rather than into a local, so what gets
     * persisted on drop is somewhere the button can actually be. */
    var lift = function () {
      var w = b.offsetWidth || 96;
      var h = b.offsetHeight || 44;
      var maxR = Math.max(8, vpW() - w - 8);
      var maxB = Math.max(8, vpH() - h - 8);
      if (pos) {
        pos.r = Math.min(Math.max(pos.r, 8), maxR);
        pos.b = Math.min(Math.max(pos.b, 8), maxB);
      }
      b.style.right = (pos ? pos.r : 16) + 'px';
      b.style.bottom = ((pos ? pos.b : 16) + hiddenPx()) + 'px';
    };
    lift();

    /* A press that travels is a move; a press that does not is a tap that opens
     * the tour. Same problem as the map: the browser fires `click` either way
     * and does not tell them apart. */
    var DRAG_SLOP = 8;
    var grab = null;
    var moved = false;

    b.addEventListener('pointerdown', function (e) {
      if (e.button != null && e.button !== 0) return;
      grab = {
        id: e.pointerId,
        x: e.clientX,
        y: e.clientY,
        r: pos ? pos.r : 16,
        b: pos ? pos.b : 16,
        far: 0
      };
      moved = false;
      if (b.setPointerCapture) b.setPointerCapture(e.pointerId);
    });

    b.addEventListener('pointermove', function (e) {
      if (!grab || e.pointerId !== grab.id) return;
      var dx = e.clientX - grab.x;
      var dy = e.clientY - grab.y;
      grab.far = Math.max(grab.far, Math.hypot(dx, dy));
      if (grab.far <= DRAG_SLOP) return;
      e.preventDefault();
      moved = true;
      /* Right and bottom grow the other way from x and y. */
      pos = { r: grab.r - dx, b: grab.b - dy };
      lift();
    });

    var drop = function (e) {
      if (!grab || e.pointerId !== grab.id) return;
      grab = null;
      if (!moved || !pos) return;
      try { localStorage.setItem(POS, JSON.stringify({ r: Math.round(pos.r), b: Math.round(pos.b) })); } catch (e2) {}
    };
    b.addEventListener('pointerup', drop);
    b.addEventListener('pointercancel', drop);

    b.addEventListener('click', function (e) {
      if (moved) { moved = false; e.preventDefault(); e.stopPropagation(); return; }
      start(steps, opts);
    });

    var v = vv();
    if (v && v.addEventListener) {
      v.addEventListener('resize', lift);
      v.addEventListener('scroll', lift);
    } else {
      window.addEventListener('resize', lift);
    }
    window.addEventListener('resize', lift);
  }

  root.Tour = { start: start, auto: auto, stop: function () { finish(false); }, seen: seen };
})(typeof window !== 'undefined' ? window : this);
