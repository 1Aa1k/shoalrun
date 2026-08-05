# Handing Shoalrun to someone else

Written for the case of giving this to a business on the lake — an outfitter,
a camp, a rental operation. The technical part is easy. The part that needs
thinking about is what you are and are not claiming.

## Read this first

**72% of the hazard map is unverified, and that is a measurement, not a
disclaimer.**

Aerial imagery on this lake was tested against the 260 MDIFW soundings and
cannot distinguish 10 ft of water from 25 ft — AUC 0.507, a coin flip. A
"shoal" detection means bottom seen through water. The bottom is not visible
here; the water is too stained. So of 4,908 candidates:

| tier | count | what stands behind it |
|---|---|---|
| confirmed | 48 | above the waterline, cross-checked at 0.3 m |
| likely | 1,311 | returns infrared, so a dry surface breaking the water |
| unverified | 3,549 | persistent across six flights, meaning unknown |

The parts that hold up are the ones seen *directly* — rock that breaks the
surface. Submerged hazards are not reliably mapped and, from imagery, cannot
be. The two evidence sources worth anything are **driven water** (a pass that
hit nothing proves depth along that line) and **verified reports** (someone who
knows the lake looked).

If you hand this to a business, say that out loud. An operation putting it in
front of paying renters is in a different position from a friend using it on
his own boat, and "the app said the water was clear" is a sentence you do not
want said about a tool with your name on it. The app states its own limits on
the settings panel; do not let anyone remove that text.

Practical framing that is both honest and useful: *this shows you where we have
driven, and where we know rocks are. It does not show you everywhere a rock
is.*

## What you actually hand over

### 1. The app

`dist/index.html` is the whole thing — 2.3 MB, one file, no network calls,
no dependencies. Put it on any static host with HTTPS:

```bash
npx vercel deploy --prod dist/       # or Netlify, GitHub Pages, anything
```

HTTPS is not optional: browsers refuse geolocation without it.

Ship `sw.js` and `manifest.json` alongside (the build already writes them). They
make it installable, which matters more than it sounds — see below.

### 2. Tell people to Add to Home Screen

Not a bookmark. Installing it:

- guarantees offline availability instead of leaving it to cache eviction
- exempts stored tracks from iOS's 7-day purge of unused site data
- gives a full screen without browser chrome, which is legible in sunlight

A guest who bookmarks it may find an empty app three weeks later.

### 3. The sync backend, only if you want sharing

`server/worker.js`, a Cloudflare Worker with one KV namespace. Free tier covers
one lake by a wide margin.

```bash
cd server
npx wrangler kv namespace create SHOALRUN
npx wrangler deploy
```

Then, in the app's settings, join with a lake code and the Worker URL.

**Until a lake code is set, nothing leaves the phone.** That is the default and
it is deliberate. Turning on sync means boat positions go to a server, which is
a decision for the people being tracked to make, not for you to make for them.

### 4. Decide who reviews reports

Guests tap one button to report something. Those reports alert nobody but the
person who made them until someone who knows the lake confirms them. That
reviewer needs to be a named person, not "whoever gets to it" — an unreviewed
queue is the same as no reporting feature.

## The security model, plainly

The lake code is a shared password. Anyone with it can read and write that
lake's data. That is the right trade for a family sharing where they have
driven a boat, and it is wrong for anything else. What is stored is boat tracks
and rock reports — no names, no emails, no accounts. Do not extend it to hold
anything you would mind a guest's cousin reading.

## What would actually make it better

Sonar. A fishfinder with GPS logging, or a castable unit, records real depth
along every track. Sound does not care that the water is stained, which is the
exact reason the optical approach failed here. One season of ordinary boating
would produce a better depth map than the 1954 lead-line survey the app
currently leans on, and the system to receive it is already built.

A drone gets sharper pixels of the same thing and does not address the limit,
which is that light does not come back from the bottom of this water.
