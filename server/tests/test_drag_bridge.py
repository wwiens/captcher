"""bridge.js performs and judges a drag step in a real browser.

Show-me has to carry the element to the zone and leave it there; You-try has
to accept a real drop on the zone, refuse one elsewhere, and work both for an
element the browser drags natively (draggable="true") and for one it does not
(the bridge's pointer fallback). None of that is reachable from node, so this
drives the bridge in headless Chromium through Playwright, on a page that
inlines bridge.js and records what it posts to its parent.

Skipped when Playwright or its Chromium is not installed — the server itself
runs without either.
"""
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover - environment without playwright
    sync_playwright = None

BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"

PAGE = """<!DOCTYPE html><html><head><style>
  body{margin:0;font:14px sans-serif}
  .board{display:flex;gap:20px;padding:20px}
  .col{width:220px;flex:none;min-height:260px;background:#eee;padding:8px}
  .deal,.item{background:#fff;padding:10px;margin:8px 0;border:1px solid #999}
  .lists{display:flex;gap:20px;padding:20px}
  .list{width:200px;min-height:120px;background:#dde;padding:8px;list-style:none;margin:0}
</style></head><body>
<div class="board" id="board">
  <div class="col" data-stage="Qualifying"><h3>Qualifying</h3></div>
  <div class="col" data-stage="Proposal"><h3>Proposal</h3>
    <article class="deal" draggable="true" data-id="15">Ironvale Steel</article>
  </div>
  <div class="col" data-stage="Negotiation"><h3>Negotiation</h3></div>
</div>
<div class="lists">
  <ul class="list" data-list="a"><li class="item" data-id="x">Plain item</li></ul>
  <ul class="list" data-list="b"></ul>
</div>
<script>
  window.__msgs = [];
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type) window.__msgs.push(e.data);
  });
</script>
<script>__BRIDGE__</script>
</body></html>"""

CARD = {"type": "drag", "target": {"selector": 'article[data-id="15"]'},
        "drop_target": {"selector": 'div[data-stage="Negotiation"]'}}
ITEM = {"type": "drag", "target": {"selector": 'li[data-id="x"]'},
        "drop_target": {"selector": 'ul[data-list="b"]'}}


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class DragInTheBridge(unittest.TestCase):
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
        self.page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.page.set_content(self.html)
        self.page.wait_for_function("window.__msgs.some(m => m.type === 'bridge-ready')")
        self.page.evaluate("window.postMessage({cmd: 'config', speed: 20}, '*')")

    def tearDown(self):
        self.page.close()

    def post(self, cmd, action):
        self.page.evaluate("a => window.postMessage({cmd: a.cmd, action: a.action}, '*')",
                           {"cmd": cmd, "action": action})

    def wait_for(self, msg_type):
        self.page.wait_for_function(
            "t => window.__msgs.some(m => m.type === t)", arg=msg_type, timeout=10000)

    def types(self):
        return self.page.evaluate("window.__msgs.map(m => m.type)")

    def column_of(self, selector):
        return self.page.evaluate(
            "s => document.querySelector(s).closest('.col, .list').dataset.stage"
            " || document.querySelector(s).closest('.col, .list').dataset.list", selector)

    def drag_by_mouse(self, src, dst):
        a = self.page.locator(src).bounding_box()
        b = self.page.locator(dst).bounding_box()
        self.page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=12)
        self.page.mouse.up()

    def test_show_me_carries_the_card_into_the_zone(self):
        self.post("showme", CARD)
        self.wait_for("showme-done")
        self.assertEqual(self.column_of('article[data-id="15"]'), "Negotiation")
        self.assertEqual(self.page.locator(".br-ghost").count(), 0)
        self.assertEqual(self.page.locator(".br-lifted").count(), 0)

    def test_show_me_scrolls_an_off_screen_zone_into_view(self):
        # a viewport narrower than the board: Negotiation starts past its edge
        narrow = self.browser.new_page(viewport={"width": 560, "height": 600})
        try:
            narrow.set_content(self.html)
            narrow.wait_for_function("window.__msgs.some(m => m.type === 'bridge-ready')")
            narrow.evaluate("window.postMessage({cmd: 'config', speed: 20}, '*')")
            self.assertEqual(narrow.evaluate("window.scrollX"), 0)
            narrow.evaluate("a => window.postMessage({cmd: 'showme', action: a}, '*')", CARD)
            narrow.wait_for_function("window.__msgs.some(m => m.type === 'showme-done')",
                                     timeout=10000)
            self.assertGreater(narrow.evaluate("window.scrollX"), 0)
            zone_right = narrow.evaluate(
                "document.querySelector('div[data-stage=\"Negotiation\"]').getBoundingClientRect().right")
            self.assertLessEqual(zone_right, 560)
            self.assertEqual(narrow.evaluate(
                "document.querySelector('article[data-id=\"15\"]').closest('.col').dataset.stage"),
                "Negotiation")
        finally:
            narrow.close()

    def test_show_me_reports_a_missing_zone(self):
        self.post("showme", dict(CARD, drop_target={"selector": "div.nowhere"}))
        self.wait_for("showme-done")
        done = [m for m in self.page.evaluate("window.__msgs") if m["type"] == "showme-done"]
        self.assertTrue(done[0].get("missing"))
        self.assertEqual(self.column_of('article[data-id="15"]'), "Proposal")

    def test_you_try_accepts_a_native_drop_on_the_zone(self):
        self.post("expect", CARD)
        self.drag_by_mouse('article[data-id="15"]', 'div[data-stage="Negotiation"]')
        self.wait_for("success")
        self.assertEqual(self.column_of('article[data-id="15"]'), "Negotiation")
        self.assertNotIn("wrong", self.types())

    def test_you_try_refuses_a_drop_on_the_wrong_zone(self):
        self.post("expect", CARD)
        self.drag_by_mouse('article[data-id="15"]', 'div[data-stage="Qualifying"]')
        self.wait_for("wrong")
        self.assertEqual(self.column_of('article[data-id="15"]'), "Proposal")
        self.assertNotIn("success", self.types())
        self.assertEqual(self.page.locator(".br-drop").count(), 0)

    def test_you_try_drags_a_non_draggable_element_with_the_pointer(self):
        self.post("expect", ITEM)
        self.drag_by_mouse('li[data-id="x"]', 'ul[data-list="b"]')
        self.wait_for("success")
        self.assertEqual(self.column_of('li[data-id="x"]'), "b")
        self.assertEqual(self.page.locator(".br-ghost").count(), 0)

    def test_a_click_on_the_target_is_not_a_drag(self):
        self.post("expect", ITEM)
        self.page.click('li[data-id="x"]')
        self.page.wait_for_timeout(200)
        self.assertNotIn("success", self.types())
        self.assertEqual(self.column_of('li[data-id="x"]'), "a")

    def test_hint_rings_both_ends(self):
        self.post("hint", CARD)
        self.page.wait_for_timeout(100)
        hinted = self.page.evaluate(
            "Array.from(document.querySelectorAll('.br-hint')).map(e => e.dataset.id || e.dataset.stage)")
        self.assertEqual(sorted(hinted), ["15", "Negotiation"])


if __name__ == "__main__":
    unittest.main()
