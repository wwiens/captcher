"""The capture recorder must never carry a credential off the page.

`extension/recorder.js` reports every click, select and text input to
the panel, and whatever it puts in an action's `value` ends up in three places
at once: `walkthrough.json` on this server's disk, the authoring prompt sent to
the model, and the screen — `ingest.convert()` stores it as the step's
`demo_value` (ingest.py), which is exactly what Show-me mode types on replay.

It leaked passwords two ways, and both are regression-tested here:

  1. the `change` listener recorded every `<input>` whose type was not
     checkbox/radio/button/submit/reset/file/image — a list that never
     excluded `password`;
  2. `labelOf()` falls back to `el.value` when an input has no aria-label or
     title, so merely *clicking* a password box recorded its contents as the
     step's label.

The captured HTML itself was never the problem: the vendored SingleFile engine
skips `type=password` when it stamps live input values for serialization
(`setInputValues`). The recorder was the only leak, which is why this test
drives the recorder's real listeners rather than checking a snapshot.

This test lives in the server repo, not the extension's, for the same reason
tests/test_ingest_contract.py does: the extension has no suite of its own, and
the extension directory resolves the same way the other cross-repo contract
tests resolve it (paths.extension_checkout). Skipped when node or the
extension tree is unavailable.
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

# A value distinctive enough that finding it anywhere in the output is proof.
SECRET = "hunter2-correct-horse"
OTP = "133707"

# A DOM stub with just enough surface for recorder.js's IIFE to install its
# listeners, plus a driver that fires events at them and reports what the
# recorder tried to send. Anything the recorder touches that is missing here
# surfaces as a node error, not a silent pass — the test asserts on exit code.
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
  }
  getAttribute(n) { return n in this._attrs ? this._attrs[n] : null; }
  closest() { return this; }              // the recorder's click target lookup
  get innerText() { return this._attrs.innerText || ""; }
  get title() { return this._attrs.title || ""; }
  get alt() { return this._attrs.alt || ""; }
  getBoundingClientRect() { return { width: 10, height: 10, top: 0, left: 0 }; }
}
const listeners = {};
const document = {
  title: "Sign in",
  documentElement: new Element("html"),
  body: new Element("body"),
  readyState: "complete",
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(cb => cb !== fn); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
};
document.documentElement.ownerDocument = document;
const window = {
  addEventListener() {},
  removeEventListener() {},
  innerWidth: 1200, innerHeight: 800,
  getComputedStyle() {
    return { position: "static", display: "block", visibility: "visible" };
  },
};
window.top = window;            // top frame: the recorder bails out of subframes
globalThis.window = window;
globalThis.document = document;
globalThis.Element = Element;
globalThis.location = { href: "https://app.example.com/login" };
globalThis.CSS = { escape: (s) => String(s) };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
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

function fire(type, target) {
  target.ownerDocument = document;
  for (const fn of listeners[type] || []) fn({ target, type });
}
const mk = (attrs, value) => {
  const el = new Element("input", attrs, value);
  if (attrs.type) el.type = attrs.type;
  return el;
};

fire("change", mk({ type: "password", name: "password" }, SECRET_));
fire("change", mk({ type: "text", autocomplete: "one-time-code" }, OTP_));
fire("change", mk({ type: "text", name: "email" }, "someone@example.com"));
fire("change", mk({ type: "password" }, SECRET_));   // second field, same secret
fire("click", mk({ type: "password" }, SECRET_));    // the labelOf() path
grant({ type: "stop" }); // clear the real lease and recorder timers
console.log(JSON.stringify(sent));
"""


@unittest.skipUnless(NODE, "node is not installed")
@unittest.skipUnless(RECORDER.is_file(), f"no recorder.js at {RECORDER}")
class RecorderRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (f"const SECRET_ = {json.dumps(SECRET)};\n"
                  f"const OTP_ = {json.dumps(OTP)};\n"
                  f"const RECORDER_ = {json.dumps(str(RECORDER))};\n" + HARNESS)
        proc = subprocess.run([NODE, "-e", script],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(
                "the recorder harness failed to run — recorder.js may now touch "
                f"a DOM API the stub does not provide:\n{proc.stderr}")
        cls.messages = json.loads(proc.stdout)
        cls.actions = [m["action"] for m in cls.messages
                       if m.get("type") == "rc-action"]

    def test_no_secret_survives_anywhere_in_the_recorded_actions(self):
        """The blunt assertion, and the one that matters: the credential must
        not appear in any field of anything the recorder sends — not just the
        ones this test knows to look at."""
        blob = json.dumps(self.actions)
        self.assertNotIn(SECRET, blob,
                         "a password value reached the recorded actions")
        self.assertNotIn(OTP, blob,
                         "a one-time code reached the recorded actions")

    def test_the_password_step_is_still_recorded(self):
        """Redaction must not drop the action. A walkthrough that starts with a
        login has to keep that login as a step, or the replay makes no sense."""
        inputs = [a for a in self.actions if a["type"] == "input"]
        self.assertEqual(len(inputs), 4, f"expected 4 input actions, got {inputs}")
        secret_steps = [a for a in inputs if a.get("sensitive")]
        self.assertEqual(len(secret_steps), 3,
                         "password and one-time-code fields should be flagged")
        for action in secret_steps:
            # non-empty, and long enough to clear the min_length of 3 that
            # ingest.convert() stamps on every input step
            self.assertGreaterEqual(len(action["value"]), 3)

    def test_ordinary_fields_still_record_their_real_value(self):
        """The redaction must be narrow. Demo values are the point of an input
        step; blanking them all would be a different bug."""
        ordinary = [a for a in self.actions
                    if a["type"] == "input" and not a.get("sensitive")]
        self.assertEqual([a["value"] for a in ordinary], ["someone@example.com"])

    def test_clicking_a_password_field_does_not_label_the_step_with_it(self):
        """labelOf() falls back to el.value for inputs with no aria-label or
        title. That made a bare click on a password box record the password."""
        clicks = [a for a in self.actions if a["type"] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertNotIn(SECRET, clicks[0].get("label") or "")


if __name__ == "__main__":
    unittest.main()
