// Automatic sync. Nothing for the user to do, ever.
//
// The first version handed coverage between boats as a JSON file. That is fine
// for someone who knows what a JSON file is and hopeless for everybody else,
// which is most of the people this is meant to protect. A guest who has to
// export, AirDrop and import will simply not do it, and a sharing feature
// nobody uses is worse than none because it looks like the problem is solved.
//
// So: no buttons. The app queues changes locally and drains the queue whenever
// it finds itself online. On the lake it is a no-op, because there is no
// signal; back at the landing or at the house it catches up by itself.
//
// JOINING is a short lake code typed once, not an account. No email, no
// password, no profile -- none of which this needs, and every one of which is
// another thing to go wrong on a phone in a boat.
//
// NOTHING LEAVES THE DEVICE UNTIL A CODE IS SET. With no code configured every
// function here is inert and the app is exactly as local as it was before. That
// is deliberate: uploading a family's movements without them asking is not a
// default anyone should ship.

const LS_CODE = "shoalrun_lake_code";
const LS_ENDPOINT = "shoalrun_sync_endpoint";
const LS_WHO = "shoalrun_who";

export function lakeCode() {
  return localStorage.getItem(LS_CODE) || null;
}

export function endpoint() {
  return localStorage.getItem(LS_ENDPOINT) || null;
}

/** Stable per-device id, so reports can be counted as independent people. */
export function whoAmI() {
  let w = localStorage.getItem(LS_WHO);
  if (!w) {
    w = `d${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(LS_WHO, w);
  }
  return w;
}

export function joinLake(code, url) {
  localStorage.setItem(LS_CODE, code.trim().toUpperCase());
  if (url) localStorage.setItem(LS_ENDPOINT, url.trim());
}

export function leaveLake() {
  localStorage.removeItem(LS_CODE);
  localStorage.removeItem(LS_ENDPOINT);
}

export function isConfigured() {
  return Boolean(lakeCode() && endpoint());
}

/**
 * Try to exchange local state for everyone else's.
 *
 * Returns null when there is nothing to do -- offline, or no lake joined --
 * rather than throwing, because "no signal" is the normal case on this lake and
 * normal cases should not look like errors.
 *
 * @param {{swept: object, flags: Array}} local
 * @param {(url:string, init:object)=>Promise} doFetch injected for testing
 */
export async function syncNow(local, doFetch = fetch) {
  if (!isConfigured()) return null;
  if (typeof navigator !== "undefined" && navigator.onLine === false) return null;

  const body = JSON.stringify({
    code: lakeCode(),
    who: whoAmI(),
    swept: local.swept,
    flags: local.flags,
  });

  try {
    const res = await doFetch(`${endpoint()}/sync`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    // A failed sync is not an error worth showing. The data is still on the
    // phone and will go up next time there is signal; surfacing a red banner
    // every time the boat leaves cell range would train the user to ignore
    // banners, and one of those banners is the hazard alert.
    return null;
  }
}

/**
 * Sync when it is likely to work: on load, and whenever the phone regains
 * signal. No timer -- polling a dead network on a boat costs battery and
 * achieves nothing.
 */
export function autoSync(getLocal, onResult) {
  const run = async () => {
    const r = await syncNow(getLocal());
    if (r && onResult) onResult(r);
  };
  if (typeof addEventListener === "function") {
    addEventListener("online", run);
    // Coming back to the app is the other moment worth trying -- a phone that
    // regained signal while asleep never fires "online".
    addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") run();
    });
  }
  run();
  return run;
}
