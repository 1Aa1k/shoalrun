// What to say when the fix does not arrive.
//
// On a phone this is the most likely thing to go wrong on a first open, and it
// used to be a grey half-line at the bottom of the screen reading
// "GPS: User denied Geolocation" -- the browser's words, in the smallest type
// on the page, next to a map that otherwise looks like it is working.
//
// A hazard app that does not know where you are is not degraded, it is off. So
// the banner says so at banner size, and the status line says the one thing
// that fixes it.

/** Positional error codes, per the Geolocation API. */
export const GPS_DENIED = 1;
export const GPS_UNAVAILABLE = 2;
export const GPS_TIMEOUT = 3;

/**
 * @param {{code?: number, message?: string}} err
 * @returns {{banner: string|null, status: string, level: "warn"|""}}
 *   `banner` is null when the alert strip should be left alone -- a timeout is
 *   usually a phone that has not got a fix yet, not a broken app, and taking
 *   over the hazard banner for it would cry wolf.
 */
export function gpsFailure(err) {
  const code = err && err.code;

  if (code === GPS_DENIED) {
    return {
      banner: "LOCATION OFF",
      status:
        "This cannot warn you about anything without your location. Allow it " +
        "for this site in your browser settings, then reload.",
      level: "warn",
    };
  }

  if (code === GPS_UNAVAILABLE) {
    return {
      banner: "NO POSITION",
      status:
        "The phone cannot get a position right now. Under a roof or against a " +
        "hill it can take a minute; out on the water it should not.",
      level: "warn",
    };
  }

  if (code === GPS_TIMEOUT) {
    return {
      banner: null,
      status: "Still looking for a position. This can take a minute on a cold start.",
      level: "warn",
    };
  }

  return {
    banner: null,
    status: `GPS: ${(err && err.message) || "unknown error"}`,
    level: "warn",
  };
}

// A fix that stopped arriving is worse than one that never came. watchPosition
// does not always report an error when the stream dies -- the phone drops into
// low power, Safari suspends the tab under a phone call, the boat runs under a
// canopy -- and the last banner stays up. On the water a stale "clear ahead"
// reads as a live "clear ahead", which is the one lie this app must not tell.
//
// Under this many milliseconds the fix is treated as current; iOS delivers one
// a second when it has a lock, so anything past this is a stall, not jitter.
export const FIX_STALE_MS = 8000;

/**
 * Banner text for a fix that has gone quiet, or null while it is fresh.
 * Seconds under a minute, then minutes -- the number is there so a glance can
 * tell "just stalled" from "has been dead the whole way across".
 * @param {number|null} lastT   timestamp of the last fix, ms
 * @param {number} now
 */
export function staleBanner(lastT, now) {
  if (lastT == null) return null;
  const age = now - lastT;
  if (age < FIX_STALE_MS) return null;
  const s = Math.floor(age / 1000);
  return s < 60 ? `NO FIX ${s}s` : `NO FIX ${Math.floor(s / 60)}m`;
}
