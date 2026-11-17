// Extension pairing, the page's half. Loaded by the shared toolbar, so it is
// present on every signed-in page — the extension can start this from wherever
// somebody happens to be, rather than sending them to a particular page first.
//
// Full rationale in PAIRING.md. The short version: this page is the only party
// that can prove a session, and the extension is the only party that can hold a
// durable token, so the page mints a one-time code and hands it over.
//
// The page never initiates. The extension always does — it injects `connect.js`
// on a user gesture and asks. That asymmetry is deliberate: on a clean install
// the extension has no content script on this origin at all, so a page-side
// button would be talking to nobody exactly when pairing is needed. One path,
// and it is the one that works from cold.
//
// This file therefore does exactly one thing: answer `pair-request` with a
// code, or with the reason there isn't one.
(function () {
  "use strict";

  // A wire contract with connect.js in the extension repo. Renaming one end
  // breaks pairing by SILENCE — the request is never heard, or the answer never
  // recognised, and neither side raises anything. Change both in one commit;
  // tests/test_pairing.py::PairingWireContractTests is what catches drift.
  const PAIR_REQUEST = "captcher:pair-request";
  const PAIR_OFFER = "captcher:pair-offer";

  // Only this window. Without it an iframe — an ad, an embedded widget, an
  // embedded walkthrough — could ask this page to mint a pairing code and read
  // the answer off the bus.
  function isOurs(event) {
    return event.source === window && event.origin === location.origin;
  }

  // Targeted at this origin, never "*": the payload is a credential.
  function offer(payload) {
    window.postMessage(
      Object.assign({ source: "captcher-app", type: PAIR_OFFER }, payload),
      location.origin);
  }

  window.addEventListener("message", async function (event) {
    if (!isOurs(event)) return;
    const message = event.data;
    if (!message || message.type !== PAIR_REQUEST) return;
    if (message.source !== "captcher-extension") return;
    try {
      const response = await fetch("/api/extension/pair", { method: "POST" });
      const data = await response.json();
      if (!response.ok) {
        // Carried back verbatim rather than swallowed. "Log in at /login to
        // connect the extension" is the actual next step, and the extension has
        // no other way to learn it — a signed-out page looks exactly like a
        // page with no listener on it.
        return offer({ error: data.error ||
          "Captcher refused the request (" + response.status + ")." });
      }
      offer({ code: data.code });
    } catch (e) {
      offer({ error: "Captcher could not be reached (" + (e.message || e) + ")." });
    }
  });
})();
