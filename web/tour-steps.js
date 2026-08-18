/*
 * The walk-through, for somebody sent the link who has never seen the app and
 * is not standing next to the person who built it.
 *
 * The engine is `tour.js`. This is only the script: what to point at, in what
 * order, and what to say. Steps that need a drawer open ask for it in `before`
 * by pressing the app's own button, so the tour drives the app the way a thumb
 * would and nothing here duplicates app state.
 *
 * Called from the end of app.js, after the boot overlay has gone, so the first
 * ring lands on a drawn lake rather than a loading bar.
 */

/* Both drawers are a class on one element. Setting it directly rather than
 * clicking would leave the Layers button unlit, so press the real control and
 * let the app keep its own state consistent. */
function tourDrawer(open) {
  return function () {
    const panel = el("panel");
    const isOpen = panel.classList.contains("open");
    if (isOpen !== open) el("btnLayers").click();
    return new Promise((resolve) => setTimeout(resolve, 260));
  };
}

function tourView(name) {
  return function () {
    const tab = document.querySelector(`#tabs button[data-view="${name}"]`);
    if (tab) tab.click();
    return new Promise((resolve) => setTimeout(resolve, 320));
  };
}

const TOUR_STEPS = [
  {
    title: "Every rock on this lake",
    body:
      "Close to 5,000 candidates, found by watching six aerial flights over " +
      "twelve years, because nobody charts inland Maine lakes. Open this once " +
      "with signal and it works forever after with none. Arrow keys move, Esc quits.",
    place: "center",
    before: tourView("map"),
  },
  {
    target: "#alert",
    title: "The part that matters at speed",
    body:
      "This is the only thing you need to look at while moving. It goes loud " +
      "when the boat is heading at something, with the distance and how long " +
      "you have. Until there is a GPS fix it says so rather than pretending.",
    place: "bottom",
  },
  {
    target: ".caveat",
    title: "Read this once, honestly",
    body:
      "This is a navigation aid, not a chart. Every candidate came off a " +
      "satellite and 72 per cent of them have never been checked against the " +
      "water. Empty map is not the same as clear water.",
  },
  {
    target: "#legend",
    title: "Four kinds of thing",
    body:
      "A shoal never breaks the surface, which is the one that gets people. A " +
      "rock does. Ledges and islands you can see coming. What sorts them is " +
      "near-infrared: water absorbs it almost completely, any dry surface " +
      "reflects it.",
    place: "top",
  },
  {
    target: "#btnDetailAll",
    title: "Verified, or everything",
    body:
      "Verified only draws the 48 confirmed above the waterline and the 1,311 " +
      "that return infrared. Every candidate adds the other 3,549, which " +
      "persisted across six flights and mean something nobody has established. " +
      "Either way all of them still set off the alarm.",
    before: tourDrawer(true),
    place: "top",
  },
  {
    target: "#btnSwept",
    title: "Water you have already driven",
    body:
      "Driven water paints the track you have taken. It is the only thing on " +
      "this map proven by a boat rather than inferred from a satellite, and it " +
      "grows every time you go out.",
    place: "top",
  },
  {
    target: "#btnFlag",
    title: "Tell it what you hit",
    body:
      "Press this over something and it drops a report where you are. Tap any " +
      "marker to confirm it is really there, or that it is not. That is how the " +
      "unverified 72 per cent gets smaller, and it is the only thing that does.",
    before: tourDrawer(false),
    place: "left",
  },
  {
    target: '#tabs button[data-view="3d"]',
    title: "The bottom, in three dimensions",
    body:
      "The same lake as a solid you can fly through, built from the 1954 " +
      "soundings and lidar of the land around it. Useful for seeing where a " +
      "shoal sits relative to the deep water beside it.",
    place: "top",
  },
  {
    target: "#tierUnverified",
    title: "Where the numbers come from",
    body:
      "The Info tab has the whole method and every count, including what failed. " +
      "Nothing here is a claim without the working behind it.",
    before: tourView("info"),
  },
  {
    title: "That is the tour",
    body:
      "Add it to your home screen and it behaves like an app, with no signal " +
      "needed once it is on. Anything wrong or missing, say so. The Tour button " +
      "replays this any time.",
    place: "center",
    before: tourView("map"),
  },
];

function startTour() {
  if (!window.Tour) return;
  window.Tour.auto(TOUR_STEPS, { id: "shoalrun", launchLabel: "Tour", delay: 500 });
}
