"""The recorder captures a form the product just unlocked.

Mailwing's Duplicate turns a sent campaign's greyed-out fields editable,
rewrites their values from script, swaps a banner's text and adds one small
toast. The change detector ignored every part of that: `disabled` was not an
observed attribute, script-set values and text-node swaps are not element
additions, and a toast is under the post-action bar. No snapshot was taken, so
the next steps replayed on the locked copy and You try asked the learner to
type into a field that takes no typing. And had a capture been triggered,
both page signatures (panel.js) would have called it a duplicate, since
neither compared lock state.

So a lock/unlock flip on a rendered control inside the post-action window
now asks for a capture on its own, and both signatures carry which controls
are locked. This runs the real recorder, and the real signature functions,
in headless Chromium — same placement rationale as test_recorder_drag.py.
Skipped when Playwright, its Chromium, or the extension checkout is missing.
"""
import os
import re
import unittest
from pathlib import Path

from captcher import paths

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover
    sync_playwright = None

EXTENSION_DIR = paths.extension_checkout(Path(__file__).resolve().parents[1] / "captcher", os.environ.get("CAPTCHER_EXTENSION_DIR", ""))
RECORDER = EXTENSION_DIR / "recorder.js"
PANEL = EXTENSION_DIR / "sidepanel" / "panel.js"

PAGE = """<!DOCTYPE html><html><head><title>Mailwing</title><style>
  body{margin:0;font:14px sans-serif;padding:20px}
  input,select{display:block;margin:8px 0;width:300px}
  #hiddenForm{display:none}
</style></head><body>
<div id="info">Sent Mar 14 to 6,140 recipients. Duplicate this campaign to make changes.</div>
<button id="duplicateBtn">Duplicate</button>
<button id="reassertBtn">Reassert</button>
<button id="hiddenBtn">Unlock hidden</button>
<div class="build">
  <input id="bName" disabled value="March restock">
  <select id="bAud" disabled><option>All subscribers</option><option>Lapsed 90 days</option></select>
</div>
<div id="hiddenForm"><input id="hid" disabled value="x"></div>
<script>
  const fields = () => document.querySelectorAll(".build input, .build select");
  // Duplicate, as Mailwing does it: unlock, rewrite values, swap text, toast
  document.getElementById("duplicateBtn").addEventListener("click", () => {
    fields().forEach(e => { e.disabled = false; });
    document.getElementById("bName").value = "March restock — copy";
    document.getElementById("info").textContent = "Draft · Edits are saved as you type.";
    const t = document.createElement("div"); t.textContent = "Draft copy created";
    document.body.appendChild(t);
  });
  // A render that re-asserts the state the fields already have
  document.getElementById("reassertBtn").addEventListener("click", () => {
    fields().forEach(e => { e.setAttribute("disabled", ""); });
  });
  // Unlocking something that is not on screen
  document.getElementById("hiddenBtn").addEventListener("click", () => {
    document.getElementById("hid").disabled = false;
  });
  window.__sent = [];
  const portListeners = [];
  window.__grant = message => { for (const fn of portListeners) fn(message); };
  window.chrome = { runtime: {
    connect({ name }) {
      return {
        onMessage: { addListener(fn) { portListeners.push(fn); } },
        onDisconnect: { addListener() {} },
        postMessage() {},
        disconnect() {},
      };
    },
    sendMessage(m) { window.__sent.push(m); return Promise.resolve({ auto: false, capturing: false }); },
  } };
</script>
<script>__SENSITIVE__</script>
<script>__RECORDER__</script>
</body></html>"""


def _page_signature_src():
    js = PANEL.read_text(encoding="utf-8")
    start = js.index("const SIGNATURE_TIME_RE =")
    end = js.index("// The live-DOM twin of pageSignature")
    return js[start:end]


def _live_signature_fn():
    js = PANEL.read_text(encoding="utf-8")
    m = re.search(r"func: (\(timeRe\) => \{.*?\n      \}),\n      args: \[SIGNATURE_TIME_RE\]",
                  js, re.S)
    if not m:
        raise AssertionError("liveSignatureOf's injected function not found in panel.js")
    return m.group(1)


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipUnless(RECORDER.is_file(), f"no recorder.js at {RECORDER}")
class RecorderUnlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {e}")
        cls.html = (PAGE.replace("__SENSITIVE__", RECORDER.with_name("sensitive.js").read_text(encoding="utf-8"))
                    .replace("__RECORDER__", RECORDER.read_text(encoding="utf-8")))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1000, "height": 700})
        self.page.set_content(self.html)
        self.page.evaluate("window.__grant({type: 'start', sessionId: 'unlock-test'})")
        self.page.wait_for_timeout(1700)     # past the detector's ARM_DELAY_MS

    def tearDown(self):
        self.page.evaluate("window.__grant({type: 'stop'})")
        self.page.close()

    def reminders(self):
        self.page.wait_for_timeout(1000)     # QUIET_MS plus slack
        return [m for m in self.page.evaluate("window.__sent") if m.get("type") == "rc-remind"]

    def test_duplicate_unlocking_the_form_asks_for_a_capture(self):
        self.page.click("#duplicateBtn")
        sent = self.reminders()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["reason"], "update")
        self.assertEqual(sent[0]["detail"]["controls"], 2)

    def test_re_asserting_the_same_lock_state_is_not_a_change(self):
        self.page.click("#reassertBtn")
        self.assertEqual(self.reminders(), [])

    def test_unlocking_a_control_that_is_not_on_screen_is_not_a_change(self):
        self.page.click("#hiddenBtn")
        self.assertEqual(self.reminders(), [])

    def test_the_serialized_signature_sees_a_form_unlock(self):
        sig = self.page.evaluate(
            "src => { eval(src + '; window.__ps = pageSignature;'); "
            "const locked = document.documentElement.outerHTML; "
            "document.querySelectorAll('.build input, .build select').forEach(e => e.disabled = false); "
            "const open = document.documentElement.outerHTML; "
            "return [window.__ps(locked), window.__ps(open)]; }",
            _page_signature_src())
        self.assertNotEqual(sig[0], sig[1])

    def test_the_live_signature_sees_a_form_unlock(self):
        sig = self.page.evaluate(
            "([fn, timeRe]) => { const live = eval(fn); const before = live(timeRe); "
            "document.querySelectorAll('.build input, .build select').forEach(e => e.disabled = false); "
            "return [before, live(timeRe)]; }",
            [_live_signature_fn(), r"\b\d{1,2}:\d{2}\b"])
        self.assertNotEqual(sig[0], sig[1])


if __name__ == "__main__":
    unittest.main()
