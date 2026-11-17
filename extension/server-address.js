/* The one Captcher server this recorder talks to.
 *
 * Captcher is self-hosted, so there is no address the extension can ship
 * with. Instead there is exactly one CONFIGURED address, chosen in the
 * panel's settings and kept in chrome.storage.local under KEY. It defaults to
 * the address `captcher` serves on out of the box.
 *
 * Everything that decides who to talk to goes through here, in the service
 * worker (importScripts) and the panel (a <script> tag) alike:
 *
 *   - the panel only ever asks the configured origin "are you Captcher?" —
 *     never the other sites somebody browses, which would tell each of them
 *     this extension is installed;
 *   - the worker accepts a pairing code only from a tab on that origin, since
 *     a successful claim makes it the destination of every later upload;
 *   - uploads go only to an acceptable address.
 *
 * ACCEPTABLE means https, or plain http on this machine (loopback). An upload
 * carries a capture token and pages of somebody's product; neither should
 * cross a network in the clear. A server shared on a network belongs behind
 * HTTPS.
 *
 * `localhost`, `127.0.0.1` and `[::1]` on the same scheme and port are the
 * same server: which spelling the browser happens to be on must not decide
 * whether pairing works.
 */
(function (root) {
  "use strict";

  const KEY = "serverAddress";
  const DEFAULT = "http://127.0.0.1:8080";
  const LOOPBACK = ["localhost", "127.0.0.1", "[::1]"];

  function parse(raw) {
    try { return new URL(raw); } catch { return null; }
  }

  function isLoopback(u) {
    return LOOPBACK.includes(u.hostname);
  }

  /** Whether an address may be paired with and uploaded to. */
  function acceptable(raw) {
    const u = parse(raw);
    if (!u || u.username || u.password) return false;
    return u.protocol === "https:" || (u.protocol === "http:" && isLoopback(u));
  }

  /** What a person typed, as an origin — or an error to show them.
   *  "127.0.0.1:8080" and "localhost:9000/" are both fine; a scheme is
   *  assumed (http on loopback, https elsewhere) when none is given. */
  function normalize(raw) {
    let text = String(raw || "").trim();
    if (!text) return { error: "Enter the address Captcher is running at." };
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
      const host = text.split(/[/:]/)[0].toLowerCase();
      const local = LOOPBACK.includes(host) || host === "[";
      text = (local ? "http://" : "https://") + text;
    }
    const u = parse(text);
    if (!u || !/^https?:$/.test(u.protocol)) {
      return { error: "That is not a web address." };
    }
    if (u.username || u.password) {
      return { error: "Leave the user name and password out of the address." };
    }
    if (!acceptable(u.href)) {
      return { error: "Use https for a server on another machine. Plain http is " +
                      "only allowed for this computer (localhost or 127.0.0.1)." };
    }
    return { origin: u.origin };
  }

  /** Whether two addresses name the same Captcher server. */
  function sameServer(a, b) {
    const x = parse(a), y = parse(b);
    if (!x || !y) return false;
    if (x.origin === y.origin) return true;
    return isLoopback(x) && isLoopback(y) &&
           x.protocol === y.protocol && x.port === y.port;
  }

  /** The configured origin, falling back to DEFAULT. Never throws. */
  async function get() {
    try {
      const stored = (await chrome.storage.local.get(KEY))[KEY];
      const parsed = stored ? normalize(stored) : null;
      return parsed && parsed.origin ? parsed.origin : DEFAULT;
    } catch {
      return DEFAULT;
    }
  }

  root.CaptcherServer = Object.freeze({ KEY, DEFAULT, acceptable, normalize, sameServer, get });
})(typeof self !== "undefined" ? self : globalThis);
