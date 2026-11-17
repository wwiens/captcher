"""You try on a frozen page whose next control the app would have unlocked.

A snapshot never runs the page's own script. When a click switched a form from
read-only to editable (Mailwing's Duplicate) and the recorder took no new
snapshot after it, the next steps play on the locked copy: Show me still
fills the field, because setValue ignores `disabled`, and You try leaves the
learner in front of a field that takes no typing. The bridge unlocks the
step's own target — and nothing else. Driven in headless Chromium, like
test_drag_bridge.py; skipped without Playwright.
"""
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover - environment without playwright
    sync_playwright = None

BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"

PAGE = """<!DOCTYPE html><html><body>
<input id="name" disabled value="March restock">
<input id="other" disabled value="left alone">
<fieldset id="fs" disabled><select id="aud"><option>All</option><option>Lapsed 90 days</option></select></fieldset>
<input id="note" readonly value="">
<button id="save" disabled>Save</button>
<script>
  window.__msgs = [];
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type) window.__msgs.push(e.data);
  });
</script>
<script>__BRIDGE__</script>
</body></html>"""


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class LockedControlsInTheBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:      # no chromium downloaded for this playwright
            cls.pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {e}")
        cls.html = PAGE.replace("__BRIDGE__", BRIDGE.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(self.html)
        self.page.wait_for_function("window.__msgs.some(m => m.type === 'bridge-ready')")
        self.page.evaluate("window.postMessage({cmd: 'config', speed: 20}, '*')")

    def tearDown(self):
        self.page.close()

    def expect(self, action):
        self.page.evaluate("a => window.postMessage({cmd: 'expect', action: a}, '*')", action)
        self.page.wait_for_timeout(50)

    def succeeded(self):
        self.page.wait_for_function(
            "window.__msgs.some(m => m.type === 'success')", timeout=5000)
        return True

    def test_a_disabled_text_field_takes_typing_once_its_step_is_armed(self):
        self.expect({"type": "input", "target": {"selector": "#name"}, "min_length": 5})
        self.page.fill("#name", "April restock announcement")
        self.assertTrue(self.succeeded())

    def test_only_the_steps_own_target_is_unlocked(self):
        self.expect({"type": "input", "target": {"selector": "#name"}})
        self.assertFalse(self.page.is_disabled("#name"))
        self.assertTrue(self.page.is_disabled("#other"))
        self.assertTrue(self.page.is_disabled("#save"))

    def test_a_select_in_a_disabled_fieldset_can_be_chosen(self):
        self.expect({"type": "select", "target": {"selector": "#aud"},
                     "value": "Lapsed 90 days"})
        self.page.select_option("#aud", "Lapsed 90 days")
        self.assertTrue(self.succeeded())

    def test_a_readonly_field_takes_typing(self):
        self.expect({"type": "input", "target": {"selector": "#note"}, "min_length": 3})
        self.page.fill("#note", "hello")
        self.assertTrue(self.succeeded())

    def test_a_disabled_button_fires_its_click(self):
        """A browser dispatches no click at all on a disabled button."""
        self.expect({"type": "click", "target": {"selector": "#save"}})
        self.page.click("#save")
        self.assertTrue(self.succeeded())

    def test_show_me_unlocks_what_it_types_into(self):
        self.page.evaluate("window.postMessage({cmd: 'showme', action: "
                           "{type: 'input', target: {selector: '#name'}, demo_value: 'Hi'}}, '*')")
        self.page.wait_for_function(
            "window.__msgs.some(m => m.type === 'showme-done')", timeout=10000)
        self.assertFalse(self.page.is_disabled("#name"))
        self.assertEqual(self.page.input_value("#name"), "Hi")


if __name__ == "__main__":
    unittest.main()
