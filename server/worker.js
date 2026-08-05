// Sync backend. One endpoint, one stored blob per lake, no accounts.
//
// Deliberately the smallest thing that works. This is a personal tool for one
// lake, not a service: adding user accounts, a database and a login screen
// would be more moving parts to break than the thing they support, and every
// one of them is another way for a phone in a boat to fail.
//
// Runs on Cloudflare Workers with one KV namespace. Free tier covers this by a
// wide margin -- a busy season on one lake is a few thousand writes.
//
// SECURITY MODEL, stated plainly because it is weak on purpose. The lake code
// is a shared password. Anyone holding it can read and write that lake's data.
// That is the correct trade for a family and their guests sharing where they
// have driven a boat; it is NOT appropriate for anything private, and the data
// here is exactly one thing -- where boats have been on a lake, plus reports of
// rocks. No names, no emails, no accounts. Do not extend this to hold anything
// you would mind a guest's cousin reading.
//
// Deploy:
//   npx wrangler kv namespace create SHOALRUN
//   npx wrangler deploy
// then in the app, join with the lake code and this Worker's URL.

// Bounds. Every one of these exists because the request body is untrusted:
// a phone with a corrupted store, or anyone at all with the URL, can post
// whatever they like, and the failure has to be a rejection rather than an
// out-of-memory or a KV write that costs real money.
const MAX_BODY = 8 * 1024 * 1024;   // KV values cap at 25 MB; stay well under
const MAX_CELLS = 400_000;          // 400k cells at 5 m is 10 km2 of driving
const MAX_FLAGS = 5_000;
const CODE_RE = /^[A-Z0-9]{4,12}$/;

const json = (o, status = 200) =>
  new Response(JSON.stringify(o), {
    status,
    headers: {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      "access-control-allow-headers": "content-type",
      "access-control-allow-methods": "POST, OPTIONS",
    },
  });

/** Merge two coverage grids. Same rule as the client: the STRONGER claim wins.
 *
 * A slow pass proves more depth than a planing one, so it must not be
 * overwritten by a later fast pass over the same water. Merging by timestamp
 * alone would silently weaken claims already earned. */
function mergeSwept(a, b) {
  const out = new Map(a || []);
  for (const [k, v] of b || []) {
    const prev = out.get(k);
    if (!prev) out.set(k, v);
    else if (prev.planing && !v.planing) out.set(k, v);
    else if (prev.planing === v.planing && v.t > prev.t) out.set(k, v);
  }
  return out;
}

/** Merge flags by id, letting a review decision win over a pending one.
 *
 * Order matters: a confirmation or rejection made by the person who knows the
 * lake must not be reverted by a stale phone re-uploading its own pending copy
 * of the same flag when it finally gets signal. */
function mergeFlags(a, b) {
  const out = new Map((a || []).map((f) => [f.id, f]));
  for (const f of b || []) {
    const prev = out.get(f.id);
    if (!prev) {
      out.set(f.id, f);
    } else if (prev.status === "pending" && f.status !== "pending") {
      out.set(f.id, f);
    } else if (prev.status !== "pending" && f.status !== "pending") {
      out.set(f.id, (f.reviewedT || 0) > (prev.reviewedT || 0) ? f : prev);
    }
  }
  return [...out.values()];
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return json({});
    const url = new URL(req.url);
    if (!url.pathname.endsWith("/sync")) return json({ error: "not found" }, 404);
    if (req.method !== "POST") return json({ error: "POST only" }, 405);

    const len = Number(req.headers.get("content-length") || 0);
    if (len > MAX_BODY) return json({ error: "too large" }, 413);

    let body;
    try {
      body = await req.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }

    const code = String(body.code || "").toUpperCase();
    if (!CODE_RE.test(code)) return json({ error: "bad lake code" }, 400);

    const inCells = body?.swept?.cells;
    const inFlags = body?.flags;
    // Reject rather than truncate. Silently dropping half a boat's coverage
    // would leave the user believing water was shared when it was not, and a
    // false "proven" is the dangerous direction of wrong.
    if (inCells && (!Array.isArray(inCells) || inCells.length > MAX_CELLS)) {
      return json({ error: "too many cells" }, 413);
    }
    if (inFlags && (!Array.isArray(inFlags) || inFlags.length > MAX_FLAGS)) {
      return json({ error: "too many flags" }, 413);
    }

    const key = `lake:${code}`;
    const prev = (await env.SHOALRUN.get(key, "json")) || { cells: [], flags: [] };

    const cells = mergeSwept(prev.cells, inCells);
    if (cells.size > MAX_CELLS) return json({ error: "lake full" }, 507);
    const flags = mergeFlags(prev.flags, inFlags);

    const next = { cells: [...cells.entries()], flags, t: Date.now() };
    await env.SHOALRUN.put(key, JSON.stringify(next));

    // Hand back the merged world. The client folds this into its own state, so
    // one round trip both publishes and catches up.
    return json({
      swept: { cell: body?.swept?.cell || 5, cells: next.cells },
      flags: next.flags,
      merged: cells.size,
    });
  },
};
