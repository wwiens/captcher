"""The side panel's connect surface, driven under node.

The panel now decides everything about connecting for itself — what is in the
active tab, whether it is a Captcher server, whether to navigate there first,
and it pairs with a Captcher tab the moment it sees one, with nothing pressed.
The surface is a readout of one step at a time plus a button that appears only
when a person has something to do, which is only an improvement if the state
behind it is right; get it wrong and the failure is a page paired twice, a
signed-out tab re-asked on every switch, or a surface somebody is stranded on
after a pairing that in fact worked.

`sidepanel/panel.js` is loaded whole into a fake DOM with a fake `chrome`, and
the checks below open tabs, press what there is to press, and assert what
happens. What is being pinned is the connect state machine, not the capture
engine.

Skipped when node is unavailable, or when the extension repo is not checked out
beside this one.

The harness asserts what the server actually sends. That direction is the
point: the two repositories were renamed in separate passes once, and the probe
key drifted — `/api/extension/ping` answered one name while the side panel read
another, so no server was ever recognised and the panel offered to open one
instead of pairing with the tab in front of it. Nothing raised.

A test written the other way round — pinning whatever the extension happens to
read — would have gone green by agreeing with the broken half. Assert the
server, and the half that has fallen behind is the half that fails.
"""
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

NODE = shutil.which("node")
import os
from captcher import paths

EXT_ROOT = paths.extension_checkout(Path(__file__).resolve().parents[1] / "captcher", os.environ.get("CAPTCHER_EXTENSION_DIR", ""))

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const EXT = process.argv[process.argv.length - 1];

// A DOM stub with just enough surface for panel.js to load. Deliberately dumb:
// what is under test is which state the panel decides it is in, not layout.
function elStub(id) {
  const e = {
    id, dataset: {}, style: {}, children: [], value: "", textContent: "",
    className: "", disabled: false, hidden: false, checked: false,
    scrollTop: 0, scrollHeight: 0, firstChild: null, _on: {},
    addEventListener: (t, f) => { (e._on[t] = e._on[t] || []).push(f); },
    removeEventListener: () => {},
    click() { (e._on.click || []).forEach(f => f({})); },
    setAttribute() {}, getAttribute() { return null; }, focus() {}, remove() {},
    replaceChildren(...items) { e.children = items; },
    appendChild(c) { e.children.push(c); }, insertBefore() {}, prepend() {},
    querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    matches: () => false, hidePopover() {}, showPopover() {},
  };
  return e;
}

function makeWorld(opts) {
  const els = {};
  const get = (id) => els[id] || (els[id] = elStub(id));
  const body = elStub("body");
  const document = {
    body, getElementById: get, createElement: () => elStub("new"),
    addEventListener: () => {}, querySelector: () => null,
    querySelectorAll: () => [], documentElement: elStub("html"),
  };
  const tabs = opts.tabs.slice();
  const tabListeners = { updated: [] };
  const store = Object.assign({}, opts.store);
  const injected = [];   // file injections — connect.js — which is what "asked the page" means
  const probes = [];     // func injections: the origin check that precedes each one
  const docOf = (tab) => tab ? "doc:" + tab.id + ":" + tab.url : null;
  const chrome = {
    sidePanel: { setPanelBehavior() {} },
    storage: {
      local: {
        get: async (k) => {
          const keys = Array.isArray(k) ? k : [k];
          const out = {};
          keys.forEach(key => { out[key] = store[key]; });
          return out;
        },
        set: async (o) => { Object.assign(store, o); },
      },
      onChanged: { addListener() {} },
    },
    tabs: {
      query: async () => tabs.filter(t => t.active),
      get: async (id) => tabs.find(t => t.id === id),
      update: async (id, patch) => {
        const tab = tabs.find(t => t.id === id);
        Object.assign(tab, patch, { status: "loading" });
        setTimeout(() => {
          tab.status = "complete";
          [...tabListeners.updated].forEach(f => f(tab.id, { status: "complete" }, tab));
        }, 5);
        return tab;
      },
      create: async (patch) => {
        const tab = Object.assign({ id: 99, active: true, status: "complete" }, patch);
        tabs.push(tab);
        return tab;
      },
      onActivated: { addListener() {} },
      onUpdated: {
        addListener: f => tabListeners.updated.push(f),
        removeListener: f => {
          const i = tabListeners.updated.indexOf(f);
          if (i >= 0) tabListeners.updated.splice(i, 1);
        },
      },
      onCreated: { addListener() {} }, onRemoved: { addListener() {} },
      sendMessage: async () => {},
    },
    windows: { onFocusChanged: { addListener() {} }, WINDOW_ID_NONE: -1 },
    runtime: {
      onMessage: { addListener: f => { chrome.__msg = f; } },
      sendMessage: async () => {}, getManifest: () => ({ version: "1" }), lastError: null,
    },
    scripting: {
      // Resolves the way Chrome does: one InjectionResult per frame, carrying
      // the documentId the panel pins its next injection to. A func is the
      // panel's origin probe (`() => location.origin`), answered from the tab.
      executeScript: async (o) => {
        const tab = tabs.find(t => t.id === o.target.tabId);
        if (o.func) {
          probes.push(o);
          let result;
          try { result = new URL(tab.url).origin; } catch { result = undefined; }
          return [{ documentId: docOf(tab), frameId: 0, result }];
        }
        injected.push(o);
        if (opts.onInject) opts.onInject(o, chrome);
        return [{ documentId: docOf(tab), frameId: 0, result: undefined }];
      },
      registerContentScripts: async () => {}, unregisterContentScripts: async () => {},
    },
  };
  const ctx = {
    document, chrome, window: { addEventListener() {} }, console,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    fetch: opts.fetch, URL, AbortSignal: { timeout: () => undefined },
    Date, Math, JSON, navigator: { userAgent: "Chrome" },
    zipTool: {}, Blob: class {}, FormData: class {}, atob, btoa,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(EXT + "/server-address.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync(EXT + "/sidepanel/panel.js", "utf8"), ctx);
  return { el: get, body, chrome, store, injected, probes, docOf, tabListeners, tabs: () => tabs };
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Only these origins are Captcher. A ping that says yes to everything would
// make every tab look like a server and hide a routing bug. The extension
// pairs only with the configured server — unconfigured, Captcher on this
// computer — so the remote one here is reachable only once it is configured.
const LOCAL = "http://127.0.0.1:8080";
const REMOTE = "https://captcher.example.org";
const APP_HOSTS = /^(https:\/\/captcher\.example\.org|http:\/\/127\.0\.0\.1:8080)\//;
const ping = async (url) => (APP_HOSTS.test(url)
  ? { ok: true, json: async () => ({ captcher: true, pairing: 1 }) }
  : { ok: false, json: async () => ({}) });
const pingNothing = async () => ({ ok: false, json: async () => ({}) });

// What background.js really does on a successful claim: write storage, then
// answer. Both halves matter — the panel reads storage rather than trusting
// the message.
const succeed = (url) => (o, chrome) => setTimeout(async () => {
  await chrome.storage.local.set({ server: { url, token: "rc_TEST" } });
  chrome.__msg({ type: "pair-status", ok: true, email: "a@b.com", url });
}, 5);

const checks = [];
const check = (name, fn) => checks.push([name, fn]);

check("a fresh install on an unrelated tab offers to open Captcher", async () => {
  const w = makeWorld({ store: {}, fetch: pingNothing,
    tabs: [{ id: 1, active: true, status: "complete", url: "https://news.example.com/" }] });
  await sleep(40);
  if (w.body.dataset.connect !== "open") throw new Error("state " + w.body.dataset.connect);
  const button = w.el("connect-action");
  if (button.textContent !== "Open Captcher") throw new Error("label " + button.textContent);
  if (button.disabled) throw new Error("the only button on the surface is disabled");
  if (button.hidden) throw new Error("the only button on the surface is hidden");
  if (w.injected.length) throw new Error("tried to pair with a page that is not Captcher");
  // The default address has to be named, or "Open Captcher" is a guess the
  // user cannot check before pressing it.
  if (!/127\.0\.0\.1:8080/.test(w.el("connect-steps").children.map(s => s.textContent).join(" "))) {
    throw new Error("note did not name where it would go: " + w.el("connect-note").textContent);
  }
});

check("a Captcher tab pairs by itself, says so, then moves to recording", async () => {
  const w = makeWorld({ store: {}, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: LOCAL + "/account" }],
    onInject: succeed(LOCAL) });
  await sleep(120);
  // Nothing was pressed.
  if (w.injected.length !== 1) throw new Error("injected " + w.injected.length + " times");
  if (w.injected[0].files[0] !== "connect.js") throw new Error("injected " + w.injected[0].files);
  // Pinned to the document whose origin was just checked, so a tab that
  // navigates in between cannot be handed connect.js.
  const pinned = (w.injected[0].target.documentIds || [])[0];
  if (pinned !== w.docOf(w.tabs()[0])) throw new Error("connect.js not pinned to the probed page: " + pinned);
  if (w.body.dataset.connect !== "done") throw new Error("state " + w.body.dataset.connect);
  if (w.body.dataset.configured !== "true") throw new Error("not configured afterwards");
  // …so the confirmation has to be visible, and stay put long enough to read.
  if (w.el("connect-title").textContent !== "Connected") {
    throw new Error("title " + w.el("connect-title").textContent);
  }
  if (!/a@b\.com/.test(w.el("connect-note").textContent) ||
      !/127\.0\.0\.1:8080/.test(w.el("connect-note").textContent)) {
    throw new Error("note " + w.el("connect-note").textContent);
  }
  if (!w.el("connect-action").hidden) throw new Error("a button on a finished surface");
  if (w.body.dataset.surface === "main") throw new Error("moved on before Connected could be read");
  await sleep(1600);
  if (w.body.dataset.surface !== "main") throw new Error("surface " + w.body.dataset.surface);
});

check("Open Captcher navigates the tab and then connects by itself", async () => {
  const w = makeWorld({ store: {}, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: "https://news.example.com/" }],
    onInject: succeed(LOCAL) });
  await sleep(40);
  if (!/127\.0\.0\.1:8080/.test(w.el("connect-steps").children.map(s => s.textContent).join(" "))) {
    throw new Error("did not offer the configured server");
  }
  w.el("connect-action").click();
  await sleep(250);
  if (w.tabs()[0].url !== LOCAL) throw new Error("did not navigate: " + w.tabs()[0].url);
  // Once: the load is seen by both the navigation's own wait and the global
  // tab listener, and the page must not be asked for two codes.
  if (w.injected.length !== 1) throw new Error("connected " + w.injected.length + " times after navigating");
  if (w.body.dataset.connect !== "done") throw new Error("state " + w.body.dataset.connect);
  await sleep(1600);
  if (w.body.dataset.surface !== "main") throw new Error("surface " + w.body.dataset.surface);
});

check("a failed pairing says why, offers Try again, and does not loop", async () => {
  const w = makeWorld({ store: {}, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: LOCAL + "/" }],
    onInject: (o, chrome) => setTimeout(() => chrome.__msg({
      type: "pair-status", ok: false,
      error: "Unlock this server at /unlock to connect the extension." }), 5) });
  await sleep(120);
  if (w.injected.length !== 1) throw new Error("injected " + w.injected.length + " times");
  if (!/Unlock/.test(w.el("connect-status").textContent)) {
    throw new Error("status " + w.el("connect-status").textContent);
  }
  if (w.el("connect-status").className !== "error") throw new Error("not marked as an error");
  if (w.body.dataset.connect !== "ready") throw new Error("state " + w.body.dataset.connect);
  const button = w.el("connect-action");
  if (button.hidden || button.disabled) throw new Error("no way to try again after a failure");
  if (button.textContent !== "Try again") throw new Error("label " + button.textContent);
  if (w.body.dataset.surface === "main") throw new Error("moved on despite failing");

  // The same page is not asked again on its own — a signed-out tab would
  // otherwise be re-asked, six seconds at a time, on every switch back to it.
  await sleep(40);
  if (w.injected.length !== 1) throw new Error("re-asked the same page by itself");

  // …but a press is a retry,
  button.click();
  await sleep(120);
  if (w.injected.length !== 2) throw new Error("Try again did not inject: " + w.injected.length);

  // …and so is that tab loading again, which is what signing in does.
  [...w.tabListeners.updated].forEach(f => f(1, { status: "complete" }, w.tabs()[0]));
  await sleep(120);
  if (w.injected.length !== 3) throw new Error("a reload did not retry: " + w.injected.length);
});

check("a claim that stores nothing is reported, not celebrated", async () => {
  const w = makeWorld({ store: {}, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: LOCAL + "/" }],
    onInject: (o, chrome) => setTimeout(() => chrome.__msg({
      type: "pair-status", ok: true, email: "a@b.com",
      url: LOCAL }), 5) });   // …but nothing written
  await sleep(120);
  if (w.body.dataset.surface === "main") {
    throw new Error("announced success and moved to a surface where nothing works");
  }
  if (w.el("connect-status").className !== "error") throw new Error("not reported as a failure");
});

check("browsing to Captcher connects with no press", async () => {
  const w = makeWorld({ store: {}, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: "https://news.example.com/" }],
    onInject: succeed(LOCAL) });
  await sleep(40);
  if (w.body.dataset.connect !== "open") throw new Error("state " + w.body.dataset.connect);
  if (w.injected.length) throw new Error("paired with a page that is not Captcher");
  w.tabs()[0].url = LOCAL + "/";
  [...w.tabListeners.updated].forEach(f => f(1, { status: "complete" }, w.tabs()[0]));
  await sleep(120);
  if (w.injected.length !== 1) throw new Error("did not pair on arriving: injected " + w.injected.length);
  if (w.body.dataset.connect !== "done") {
    throw new Error("did not notice the navigation: " + w.body.dataset.connect);
  }
});

check("an already-connected panel opens on the recording surface", async () => {
  const w = makeWorld({
    store: { server: { url: LOCAL, token: "rc_T" } },
    fetch: pingNothing,
    tabs: [{ id: 1, active: true, status: "complete", url: "https://news.example.com/" }] });
  await sleep(40);
  if (w.body.dataset.surface !== "main") throw new Error("surface " + w.body.dataset.surface);
  if (w.body.dataset.configured !== "true") throw new Error("not configured");
});


check("a configured server elsewhere is the only one asked or paired", async () => {
  const w = makeWorld({ store: { serverAddress: REMOTE }, fetch: ping,
    tabs: [{ id: 1, active: true, status: "complete", url: LOCAL + "/" }],
    onInject: succeed(REMOTE) });
  await sleep(60);
  // A local Captcher in the tab is not the configured server: not paired.
  if (w.injected.length) throw new Error("paired with a server that is not the configured one");
  if (!/captcher\.example\.org/.test(w.el("connect-steps").children.map(s => s.textContent).join(" "))) {
    throw new Error("did not name the configured server");
  }
  w.tabs()[0].url = REMOTE + "/";
  [...w.tabListeners.updated].forEach(f => f(1, { status: "complete" }, w.tabs()[0]));
  await sleep(120);
  if (w.injected.length !== 1 || w.body.dataset.connect !== "done") {
    throw new Error("did not pair with the configured server: " + w.body.dataset.connect);
  }
});

check("saving a different server drops the old connection; plain http elsewhere is refused", async () => {
  const w = makeWorld({ store: { server: { url: LOCAL, token: "rc_T", email: "" } },
    fetch: pingNothing,
    tabs: [{ id: 1, active: true, status: "complete", url: "https://news.example.com/" }] });
  await sleep(40);
  const submit = (value) => {
    w.el("server-address").value = value;
    (w.el("server-form")._on.submit || []).forEach(f => f({ preventDefault() {} }));
  };
  submit("http://captcher.example.org");
  await sleep(20);
  if (w.store.serverAddress) throw new Error("stored a plain-http remote address");
  if (w.el("connect-status").className !== "error") throw new Error("refusal not reported");
  if (w.store.server.token !== "rc_T") throw new Error("a refused address touched the connection");
  submit("captcher.example.org");
  await sleep(40);
  if (w.store.serverAddress !== REMOTE) throw new Error("stored " + w.store.serverAddress);
  if (w.store.server.token) throw new Error("kept a key for the old server");
  if (w.el("server-address").value !== REMOTE) throw new Error("field shows " + w.el("server-address").value);
});

check("an existing local connection is preserved", async () => {
  const w = makeWorld({store:{server:{url:"http://127.0.0.1:8080",token:"cap_LOCAL"},
    lastServerUrl:"http://127.0.0.1:8080"},fetch:pingNothing,
    tabs:[{id:1,active:true,status:"complete",url:"https://news.example.com/"}]});
  await sleep(40);
  if(w.store.server.token !== "cap_LOCAL" || w.store.server.url !== "http://127.0.0.1:8080") throw new Error("changed local connection");
  if(w.body.dataset.surface !== "main") throw new Error("local setup was reset");
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
  process.exit(0);
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless((EXT_ROOT / "sidepanel" / "panel.js").is_file(),
                     "the extension repo is not checked out beside this one")


class ConnectSurfaceTests(unittest.TestCase):
    def test_the_connect_state_machine(self):
        result = subprocess.run(
            [NODE, "-e", textwrap.dedent(HARNESS), str(EXT_ROOT)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"connect surface failed:\n{result.stderr}")
        self.assertIn("ok ", result.stdout)


if __name__ == "__main__":
    unittest.main()
