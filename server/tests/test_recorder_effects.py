"""The recorder's observed-effects window: what the live page did, recorded.

`extension/recorder.js` opens a MutationObserver window around every
recorded action and reports which NAMED elements' visible text changed as
`{type:"rc-action-effects", aid, effects:[{selector, before, after}]}` — the
one signal that survives serialization loss (an addEventListener page captures
as inert markup). The timing contract under test: typing opens the window at
the first `input` event (snapshot before the page reacts), and the blur-time
`change` action claims it; an observation no action ever claims reports
nothing.

Same placement rationale as test_recorder_redaction.py: the extension has no
suite of its own, and this drives the recorder's real listeners in a node DOM
stub. Skipped when node or the extension tree is unavailable.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from captcher import paths

NODE = shutil.which("node")
EXTENSION_DIR = paths.extension_checkout(Path(__file__).resolve().parents[1] / "captcher", os.environ.get("CAPTCHER_EXTENSION_DIR", ""))
RECORDER = EXTENSION_DIR / "recorder.js"

HARNESS = r"""
const sent = [];


class Element {
  constructor(tag, attrs = {}, value = "") {
    this.tagName = tag.toUpperCase();
    this._attrs = attrs;
    this.value = value;
    this.type = attrs.type || (tag.toLowerCase() === "input" ? "text" : undefined);
    this.classList = [];
    this.children = [];
    this.parentElement = null;
    this.textContent = attrs.textContent || "";
  }
  getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
  hasAttribute(n) { return n in this._attrs; }
  get id() { return this._attrs.id || ""; }
  closest() { return this; }
  contains(other) {
    for (let el = other; el; el = el.parentElement) if (el === this) return true;
    return false;
  }
  get innerText() { return this._attrs.innerText || ""; }
  get title() { return this._attrs.title || ""; }
  get alt() { return this._attrs.alt || ""; }
  getBoundingClientRect() { return { width: 10, height: 10, top: 0, left: 0 }; }
}
const listeners = {};
const registry = {};        // selector string -> matching elements
const candidates = [];      // what the effect-candidate query returns
const document = {
  title: "Invoices",
  readyState: "complete",
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(cb => cb !== fn); },
  querySelectorAll(sel) {
    if (sel in registry) return registry[sel];
    if (sel.includes("data-testid")) return candidates;   // the candidate scan
    return [];
  },
  querySelector() { return null; },
};
document.documentElement = new Element("html");
document.body = new Element("body");
document.body.parentElement = document.documentElement;
for (const el of [document.documentElement, document.body]) el.ownerDocument = document;
document.documentElement.ownerDocument = document;

const moCallbacks = [];
globalThis.MutationObserver = class {
  constructor(cb) { moCallbacks.push(cb); }
  observe() {} disconnect() {}
};
const window = {
  addEventListener() {},
  removeEventListener() {},
  innerWidth: 1200, innerHeight: 800,
  getComputedStyle() { return { position: "static", display: "block", visibility: "visible" }; },
};
window.top = window;
globalThis.window = window;
globalThis.document = document;
globalThis.Element = Element;
globalThis.location = { href: "https://app.example.com/invoices" };
globalThis.CSS = { escape: (s) => String(s) };
globalThis.getComputedStyle = window.getComputedStyle;
// Model the worker's grant/ready/stop port; actions still use sendMessage.
const portMessages = [];
const portListeners = [];
const disconnectListeners = [];
const port = {
  onMessage: { addListener(fn) { portListeners.push(fn); } },
  onDisconnect: { addListener(fn) { disconnectListeners.push(fn); } },
  postMessage(message) { portMessages.push(message); },
  disconnect() { for (const fn of disconnectListeners) fn(); },
};
const grant = message => { for (const fn of portListeners) fn(message); };
globalThis.chrome = { runtime: {
  connect({ name }) {
    if (name !== "sc-recording-frame") throw new Error(`unexpected port: ${name}`);
    return port;
  },
  sendMessage(message) { sent.push(message); return Promise.resolve({}); },
} };
globalThis.self = window;
// Load the real privacy rules, just as the worker does before granting start.
(0, eval)(require("fs").readFileSync(require("path").join(require("path").dirname(RECORDER_), "sensitive.js"), "utf-8"));
(0, eval)(require("fs").readFileSync(RECORDER_, "utf-8"));
require("assert").strictEqual(Object.keys(listeners).length, 0);
grant({ type: "start", sessionId: "recorder-test-session" });
require("assert").deepStrictEqual(portMessages, [{ type: "ready" }]);

const fire = (type, target) => {
  target.ownerDocument = document;
  for (const fn of listeners[type] || []) fn({ target, type });
};

// the page: an amount input, a named total, and a named element that never changes
const amount = new Element("input", { type: "text", "data-testid": "amount" }, "10");
const total = new Element("span", { "data-testid": "grand-total", textContent: "$10" });
const still = new Element("span", { "data-testid": "sidebar", textContent: "Menu" });
for (const el of [amount, total, still]) {
  el.parentElement = document.body;
  el.ownerDocument = document;
  candidates.push(el);
}
candidates.push(amount);   // the acted element is a candidate too — must be excluded
registry['input[data-testid="amount"]'] = [amount];
registry['span[data-testid="grand-total"]'] = [total];
registry['span[data-testid="sidebar"]'] = [still];

// --- scenario 1: type, page recomputes, blur records the action -------------
fire("input", amount);                       // window opens; snapshot taken
total.textContent = "$25";                   // the page reacts per keystroke
for (const cb of moCallbacks) cb([{ target: { parentElement: total }, addedNodes: [] }]);
amount.value = "25";
fire("change", amount);                      // the action claims the window

// --- scenario 2: an observation no action ever claims reports nothing ------
setTimeout(() => {
  const orphan = new Element("input", { type: "text", "data-testid": "orphan" }, "x");
  orphan.parentElement = document.body;
  fire("input", orphan);                     // window opens…
  total.textContent = "$99";
  for (const cb of moCallbacks) cb([{ target: { parentElement: total }, addedNodes: [] }]);
  // …and no change event ever fires
}, 450);

setTimeout(() => {
  grant({ type: "stop" }); // clear the real lease and recorder timers
  console.log(JSON.stringify(sent));
}, 900);
"""


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless(RECORDER.is_file(), f"no recorder.js at {RECORDER}")
class RecorderEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = f"const RECORDER_ = {json.dumps(str(RECORDER))};\n" + HARNESS
        proc = subprocess.run([NODE, "-e", script],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError(
                "the recorder harness failed to run — recorder.js may now touch "
                f"a DOM API the stub does not provide:\n{proc.stderr}")
        cls.messages = json.loads(proc.stdout)
        cls.actions = [m["action"] for m in cls.messages
                       if m.get("type") == "rc-action"]
        cls.effects = [m for m in cls.messages
                       if m.get("type") == "rc-action-effects"]

    def test_the_typed_action_carries_an_aid_the_effects_report_matches(self):
        inputs = [a for a in self.actions if a["type"] == "input"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(self.effects[0]["aid"], inputs[0]["aid"])

    def test_the_effect_names_the_changed_element_with_before_and_after(self):
        effects = self.effects[0]["effects"]
        self.assertEqual(effects, [{"selector": 'span[data-testid="grand-total"]',
                                    "before": "$10", "after": "$25"}])

    def test_untouched_and_acted_elements_are_not_reported(self):
        blob = json.dumps(self.effects)
        self.assertNotIn("sidebar", blob)        # its text never changed
        self.assertNotIn('input[data-testid="amount"]', blob)  # the cause itself

    def test_an_unclaimed_observation_reports_nothing(self):
        # scenario 2 mutated the total again, but its action never arrived —
        # exactly one effects report may exist, from scenario 1
        self.assertEqual(len(self.effects), 1)
        self.assertNotIn("$99", json.dumps(self.effects))


if __name__ == "__main__":
    unittest.main()
