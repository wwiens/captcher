"""The pairing handshake, run for real under node.

`test_pairing.py` covers the Python half and compares the message-name strings
across the two repos. That is not enough on its own: the names can match while
the conversation still does not happen — a source check that rejects the
extension's own message, a reply posted to the wrong target, a claim that writes
storage before it knows the claim succeeded. None of those produce an error
anywhere. Pairing just silently never completes, which looks identical to "the
extension isn't installed".

So this runs the real files — the page's `static/pair.js`, the extension's
`connect.js` and `announce.js`, plus `background.js`'s `claimPairing` — against
a fake window/postMessage bus and a fake `chrome.runtime`, and asserts they
actually talk to each other.

Skipped when node is unavailable, or when the extension repo is not checked out
beside this one (the same posture as PairingWireContractTests).

This simulates the handshake rather than comparing strings, because matching
strings is not the property that matters — three files each individually
well-formed can still never meet. Both repositories were renamed in separate
passes once, and every message on the wire went unheard while nothing anywhere
raised: pairing simply never completed, which is indistinguishable from the
extension not being installed. A drift like that has to fail here, or it fails
in front of a user with no error to go on.
"""
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

NODE = shutil.which("node")
APP_ROOT = Path(__file__).resolve().parents[1] / "captcher"
import os
from captcher import paths

EXT_ROOT = paths.extension_checkout(APP_ROOT, os.environ.get("CAPTCHER_EXTENSION_DIR", ""))

# A window that behaves like the real one in the ways that matter here:
# postMessage is asynchronous, it refuses a target that is not the origin, and
# every delivered event carries a `source` the listener can check.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
// `node -e` shifts argv differently from a script file, so take the two paths
// from the end rather than counting from the front.
const [APP, EXT] = process.argv.slice(-2);

const ORIGIN = "https://app.captcher.app";

function makeWindow() {
  const listeners = [];
  const win = {
    location: { origin: ORIGIN, href: ORIGIN + "/account" },
    addEventListener: (t, fn) => { if (t === "message") listeners.push(fn); },
    removeEventListener: (t, fn) => {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    // A wildcard target would hand the pairing result to whatever is listening
    // if the app is ever framed. Fail loudly rather than quietly allowing it.
    postMessage: (data, target) => {
      if (target !== ORIGIN) throw new Error("postMessage target was " + target);
      setTimeout(() => [...listeners].forEach(fn => fn({ source: win, origin: ORIGIN, data })), 0);
    },
    deliver: (event) => [...listeners].forEach(fn => fn(event)),
  };
  win.window = win;
  return win;
}

function makeContext(win, { sendMessage, fetchImpl }) {
  const ctx = {
    window: win, self: win, location: win.location,
    document: { documentElement: { dataset: {} }, dispatchEvent: () => {},
                addEventListener: () => {} },
    chrome: { runtime: { getManifest: () => ({ version: "9.9.9" }), sendMessage } },
    fetch: fetchImpl, setTimeout, clearTimeout, console, URL,
    CustomEvent: class { constructor(type) { this.type = type; } },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  ctx.addEventListener = win.addEventListener;
  ctx.removeEventListener = win.removeEventListener;
  ctx.postMessage = win.postMessage;
  return ctx;
}

const load = (ctx, path) => vm.runInContext(fs.readFileSync(path, "utf8"), ctx);
const mintOk = async (url) => {
  if (url !== "/api/extension/pair") throw new Error("unexpected fetch " + url);
  return { ok: true, status: 201, json: async () => ({ code: "amp_pair_TEST", expires_in: 120 }) };
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const checks = [];
const check = (name, fn) => checks.push([name, fn]);

// ---- the whole conversation: extension asks, page answers, worker claims ---
check("pairing completes end to end", async () => {
  const sent = [];
  const win = makeWindow();
  const ctx = makeContext(win, {
    fetchImpl: mintOk,
    sendMessage: async (msg) => {
      sent.push(msg);
      return msg.type === "pair-claim" ? { ok: true, email: "a@b.com", url: ORIGIN } : undefined;
    },
  });
  load(ctx, APP + "/static/pair.js");
  load(ctx, EXT + "/connect.js");
  await sleep(80);
  if (!sent.some(m => m.type === "pair-claim" && m.code === "amp_pair_TEST")) {
    throw new Error("connect.js never claimed a minted code: " + JSON.stringify(sent));
  }
  const status = sent.find(m => m.type === "pair-status");
  if (!status || !status.ok) throw new Error("no successful pair-status: " + JSON.stringify(sent));
});

// ---- a signed-out page has to say so, not just go quiet -------------------
check("a page that cannot mint reports why", async () => {
  const sent = [];
  const win = makeWindow();
  const ctx = makeContext(win, {
    fetchImpl: async () => ({ ok: false, status: 401,
      json: async () => ({ error: "Log in at /login to connect the extension." }) }),
    sendMessage: async (msg) => { sent.push(msg); return undefined; },
  });
  load(ctx, APP + "/static/pair.js");
  load(ctx, EXT + "/connect.js");
  await sleep(80);
  if (sent.some(m => m.type === "pair-claim")) throw new Error("claimed without a code");
  const status = sent.find(m => m.type === "pair-status");
  if (!status || status.ok) throw new Error("no failing pair-status: " + JSON.stringify(sent));
  if (!/Log in/.test(status.error)) {
    throw new Error("the server's reason was not carried back: " + status.error);
  }
});

// ---- the announcer is presence-only, and stays that way -------------------
check("the announcer sets the marker and relays nothing", async () => {
  let relayed = false;
  const win = makeWindow();
  const ctx = makeContext(win, {
    fetchImpl: mintOk,
    sendMessage: async () => { relayed = true; return { ok: true }; },
  });
  load(ctx, EXT + "/announce.js");
  if (ctx.document.documentElement.dataset.captcherCapture !== "9.9.9") {
    throw new Error("the presence marker was not set");
  }
  win.deliver({ source: win, origin: ORIGIN,
                data: { source: "captcher-app", type: "captcher:pair-offer", code: "cap_pair_X" } });
  await sleep(30);
  if (relayed) throw new Error("announce.js is taking part in pairing again");
});

// ---- what the page must refuse to mint for --------------------------------
check("the page mints only for the extension, in this window", async () => {
  let minted = 0;
  const win = makeWindow();
  const ctx = makeContext(win, {
    fetchImpl: async (url) => { minted += 1; return mintOk(url); },
    sendMessage: async () => undefined,
  });
  load(ctx, APP + "/static/pair.js");

  const request = (extra) => win.deliver({
    origin: extra.origin || ORIGIN,
    source: extra.source === undefined ? win : extra.source,
    data: { source: "captcher-extension", type: "captcher:pair-request", ...extra.data },
  });
  // An iframe — an ad, an embedded widget, an embedded walkthrough — asking
  // this page to mint a credential it can then read off the bus.
  request({ source: { framed: true } });
  // Right window, wrong origin (a message forwarded from elsewhere).
  request({ origin: "https://evil.example.com" });
  // Right window, not the extension's message.
  request({ data: { source: "somebody-else" } });
  await sleep(40);
  if (minted !== 0) throw new Error("the page minted " + minted + " code(s) it should not have");

  request({});
  await sleep(40);
  if (minted !== 1) throw new Error("the page did not mint for a legitimate request");
});

// ---- background.js: where the server address comes from, and when it is written
function backgroundWorld(fetchImpl, manifest = { version: "0.1.0" }) {
  const stored = { server: { url: "https://old.example.com", token: "rc_OLD" } };
  const ctx = {
    chrome: {
      sidePanel: { setPanelBehavior: () => {} },
      runtime: { onMessage: { addListener: () => {} }, onInstalled: { addListener: () => {} },
                 onStartup: { addListener: () => {} }, getManifest: () => manifest },
      storage: { local: { get: async (k) => ({ [k]: stored[k] }),
                          set: async (o) => Object.assign(stored, o) },
                 onChanged: { addListener: () => {} } },
      scripting: { registerContentScripts: async () => {},
                   unregisterContentScripts: async () => {} },
      tabs: { sendMessage: async () => {} },
    },
    fetch: fetchImpl, setTimeout, clearTimeout, console, URL,
    AbortSignal: { timeout: () => undefined },
    navigator: { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/120" },
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    Uint8Array, Map, Set,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  load(ctx, EXT + "/server-address.js");
  load(ctx, EXT + "/background.js");
  return { ctx, stored };
}

// What Chrome reports for connect.js: the top frame of a tab on the configured
// server — unconfigured, the default, Captcher on this computer.
const SERVER = "http://127.0.0.1:8080";
const APP_SENDER = '{ origin: "' + SERVER + '", tab: { id: 1 }, frameId: 0 }';

const claimOk = async () => ({ ok: true, status: 201,
  json: async () => ({ ok: true, token: "rc_NEW", email: "a@b.com", device_id: "d1" }) });

check("the server address comes from the sender, never the message", async () => {
  let requested = null;
  const w = backgroundWorld(async (url) => { requested = url; return claimOk(); });
  const result = await vm.runInContext(
    'claimPairing("cap_pair_X", ' + APP_SENDER + ')', w.ctx);
  if (!result.ok || result.url !== SERVER) {
    throw new Error("claim returned " + JSON.stringify(result));
  }
  if (requested !== SERVER + "/api/extension/claim") {
    throw new Error("claimed against " + requested);
  }
  if (w.stored.server.url !== SERVER || w.stored.server.token !== "rc_NEW") {
    throw new Error("storage holds " + JSON.stringify(w.stored));
  }
});

check("a refused claim leaves a working connection alone", async () => {
  const w = backgroundWorld(async () => ({ ok: false, status: 400,
    json: async () => ({ error: "That pairing code has expired or was already used." }) }));
  const result = await vm.runInContext(
    'claimPairing("amp_pair_BAD", ' + APP_SENDER + ')', w.ctx);
  if (result.ok) throw new Error("a refused claim reported success");
  if (w.stored.server.url !== "https://old.example.com" || w.stored.server.token !== "rc_OLD") {
    throw new Error("a failed claim overwrote the connection: " + JSON.stringify(w.stored));
  }
});

check("a claim with no page origin is refused before the network", async () => {
  let called = false;
  const w = backgroundWorld(async () => { called = true; return claimOk(); });
  const result = await vm.runInContext('claimPairing("cap_pair_X", {})', w.ctx);
  if (result.ok || called) throw new Error("claimed without an origin");
  if (w.stored.server.token !== "rc_OLD") throw new Error("storage was touched");
});

check("an empty code is refused before the network", async () => {
  let called = false;
  const w = backgroundWorld(async () => { called = true; return claimOk(); });
  const result = await vm.runInContext(
    'claimPairing("", ' + APP_SENDER + ')', w.ctx);
  if (result.ok || called) throw new Error("an empty code reached the network");
});

check("the device label names something a person would recognise", async () => {
  let body = null;
  const w = backgroundWorld(async (url, options) => { body = JSON.parse(options.body); return claimOk(); });
  await vm.runInContext('claimPairing("cap_pair_X", ' + APP_SENDER + ')', w.ctx);
  if (body.code !== "cap_pair_X") throw new Error("the code was not sent");
  if (!/Chrome on macOS/.test(body.label)) throw new Error("label was " + body.label);
});

// ---- background.js: who may pair at all -----------------------------------
// A pairing code is a credential, so only the configured Captcher server may
// hand one over: another site, a frame inside the app, or a server other than
// the configured one must be refused before anything reaches the network or
// storage.
const refusedBeforeNetwork = async (sender, manifest) => {
  let called = false;
  const w = backgroundWorld(async () => { called = true; return claimOk(); }, manifest);
  const result = await vm.runInContext('claimPairing("cap_pair_X", ' + sender + ')', w.ctx);
  if (result.ok || called) throw new Error("claimed for " + sender);
  if (w.stored.server.token !== "rc_OLD") throw new Error("storage was touched");
};

check("a claim from another site is refused before the network", () =>
  refusedBeforeNetwork('{ origin: "https://evil.example.com", tab: { id: 1 }, frameId: 0 }'));

check("a claim from a frame inside the app is refused before the network", () =>
  refusedBeforeNetwork('{ origin: "' + SERVER + '", tab: { id: 1 }, frameId: 3 }'));

check("only the configured server pairs, and off this machine only over https", async () => {
  await refusedBeforeNetwork('{ origin: "http://127.0.0.1:9000", tab: { id: 1 }, frameId: 0 }');
  await refusedBeforeNetwork('{ origin: "https://app.captcher.app", tab: { id: 1 }, frameId: 0 }');
  const w = backgroundWorld(claimOk);
  w.stored.serverAddress = "https://captcher.example.org";
  const result = await vm.runInContext(
    'claimPairing("cap_pair_X", { origin: "https://captcher.example.org", tab: { id: 1 }, frameId: 0 })', w.ctx);
  if (!result.ok || w.stored.server.url !== "https://captcher.example.org") {
    throw new Error("the configured server did not pair: " + JSON.stringify(result));
  }
});


(async () => {
  const failures = [];
  for (const [name, fn] of checks) {
    try {
      await fn();
    } catch (e) {
      failures.push(name + ": " + e.message);
    }
  }
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok " + checks.length);
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless((EXT_ROOT / "announce.js").is_file(),
                     "the extension repo is not checked out beside this one")


class PairingHandshakeTests(unittest.TestCase):
    def test_the_three_files_actually_talk_to_each_other(self):
        result = subprocess.run(
            [NODE, "-e", textwrap.dedent(HARNESS), str(APP_ROOT), str(EXT_ROOT)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"handshake simulation failed:\n{result.stderr}")
        self.assertIn("ok ", result.stdout)


if __name__ == "__main__":
    unittest.main()
