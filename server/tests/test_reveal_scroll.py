"""The page moves only when a step's target is not comfortably on screen.

Every step used to re-centre its target — Show me's cursor, the popup
player's `track … reveal`, hints, the editor's probe — so the screen jumped on
each step even when the target was in plain view. `scrollLocal(el, "reveal")`
leaves a target at least REVEAL_BORDER (80px) inside the visible area exactly
where it is, and otherwise moves the least that brings it to that border, in
the page and in every scrolling box around it. The border is what clears a
footer a product draws over its own scroll area (Salesforce's modal). Driven through the bridge's real commands in
headless Chromium, like test_drag_bridge.
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
  button{display:block;height:30px;width:120px}
  #box{height:400px;overflow:auto;border:1px solid #999}
</style></head><body>
<!-- Both "visible" targets sit clear of the 80px border, but where centring
     WOULD move something: the page button low in the viewport, the box
     button low in its box. -->
<div id="box" style="margin-top:20px">
  <button id="box-visible" style="margin-top:200px">In box, visible</button>
  <button id="box-hidden" style="margin-top:900px">In box, hidden</button>
  <div style="height:300px"></div>
</div>
<button id="top-btn" style="margin-top:180px">Visible</button>
<button id="far" style="margin-top:2000px">Far below</button>
<div id="tall" style="height:3000px;margin-top:100px">tall</div>
<script>__BRIDGE__</script>
</body></html>"""

# A modal shaped like Salesforce's New Lead: the form scrolls in #body and
# the footer is drawn over its last 60px.
MODAL = """<!DOCTYPE html><html><head><style>
  body{margin:0;font:14px sans-serif}
  button{display:block;height:30px;width:120px}
</style></head><body>
<div id="modal" style="position:relative;width:400px;height:400px;margin-top:20px">
  <div id="body" style="height:400px;overflow:auto">
    <button id="mid" style="margin-top:150px">Mid form</button>
    <button id="under-footer" style="margin-top:175px">Under the footer</button>
    <div style="height:600px"></div>
  </div>
  <div id="footer" style="position:absolute;left:0;right:0;bottom:0;height:60px;background:#ccc">Save</div>
</div>
<script>__BRIDGE__</script>
</body></html>"""

VIEW = {"width": 1000, "height": 800}
BORDER = 80


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class RevealScroll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch()
        except Exception as e:  # pragma: no cover - chromium not installed
            cls._pw.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport=VIEW)
        self.page.set_content(PAGE.replace("__BRIDGE__", BRIDGE.read_text()))

    def tearDown(self):
        self.page.close()

    def send(self, cmd, sel, wait=400):
        target = {"selector": sel}
        if cmd == "track":
            msg = {"cmd": "track", "target": target, "reveal": True, "seq": 1}
        elif cmd == "probe":
            msg = {"cmd": "probe", "target": target}
        else:
            msg = {"cmd": cmd, "action": {"type": "click", "target": target}}
        self.page.evaluate("m => window.postMessage(m, '*')", msg)
        self.page.wait_for_timeout(wait)

    def state(self):
        return self.page.evaluate(
            "() => ({y: window.scrollY, box: document.getElementById('box').scrollTop})")

    def rect(self, sel):
        return self.page.evaluate(
            "s => { const r = document.querySelector(s).getBoundingClientRect();"
            " return {top: r.top, bottom: r.bottom}; }", sel)

    def test_a_visible_target_moves_nothing_for_any_command(self):
        for cmd in ("track", "hint", "probe", "showme"):
            with self.subTest(cmd=cmd):
                self.send(cmd, "#top-btn")
                self.assertEqual(self.state(), {"y": 0, "box": 0})

    def test_a_visible_target_inside_a_scroll_box_moves_nothing(self):
        self.send("track", "#box-visible")
        self.assertEqual(self.state(), {"y": 0, "box": 0})

    def test_an_off_screen_target_is_brought_in_minimally_not_centred(self):
        self.send("track", "#far")
        r = self.rect("#far")
        # fully on screen, near the bottom edge with its margin — not centred
        self.assertGreaterEqual(r["top"], 0)
        self.assertLessEqual(r["bottom"], VIEW["height"])
        self.assertAlmostEqual(VIEW["height"] - r["bottom"], BORDER, delta=2)

    def test_a_target_hidden_in_a_scroll_box_scrolls_the_box(self):
        self.send("track", "#box-hidden")
        s = self.state()
        self.assertGreater(s["box"], 0)
        self.assertEqual(s["y"], 0, "the page itself needed no movement")
        box = self.rect("#box")
        btn = self.rect("#box-hidden")
        self.assertGreaterEqual(btn["top"], box["top"])
        self.assertAlmostEqual(box["bottom"] - btn["bottom"], BORDER, delta=3)

    def test_a_target_taller_than_the_view_with_its_top_visible_stays(self):
        self.page.evaluate("window.scrollTo(0, document.getElementById('tall').offsetTop - 300)")
        before = self.state()["y"]
        self.send("track", "#tall")
        self.assertEqual(self.state()["y"], before)

    def test_show_me_after_track_moves_nothing_more(self):
        """The popup player reveals before its first report so the card is
        placed once; Show me's cursor uses the same rule, so its own scroll
        must then be a no-op."""
        self.send("track", "#far")
        after_track = self.state()
        self.send("showme", "#far", wait=600)
        self.assertEqual(self.state(), after_track)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class RevealUnderAFooter(RevealScroll):
    """A field inside its scroll box but under a footer drawn over it is
    inside the border, so it is moved — to the border, which clears it."""

    def setUp(self):
        self.page = self.browser.new_page(viewport=VIEW)
        self.page.set_content(MODAL.replace("__BRIDGE__", BRIDGE.read_text()))

    def body_scroll(self):
        return self.page.evaluate("document.getElementById('body').scrollTop")

    def covered(self, sel):
        return self.page.evaluate(
            "s => { const e = document.querySelector(s), r = e.getBoundingClientRect();"
            " const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            " return !(hit === e || e.contains(hit)); }", sel)

    def test_a_field_under_the_footer_is_scrolled_clear(self):
        self.assertTrue(self.covered("#under-footer"), "fixture: starts hidden")
        self.send("track", "#under-footer")
        self.assertFalse(self.covered("#under-footer"))
        body, btn = self.rect("#body"), self.rect("#under-footer")
        self.assertAlmostEqual(body["bottom"] - btn["bottom"], BORDER, delta=3)

    def test_a_field_clear_of_the_border_still_moves_nothing(self):
        self.send("track", "#mid")
        self.assertEqual(self.body_scroll(), 0)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)

    # the page-fixture tests do not apply to this page
    test_a_visible_target_moves_nothing_for_any_command = None
    test_a_visible_target_inside_a_scroll_box_moves_nothing = None
    test_an_off_screen_target_is_brought_in_minimally_not_centred = None
    test_a_target_hidden_in_a_scroll_box_scrolls_the_box = None
    test_a_target_taller_than_the_view_with_its_top_visible_stays = None
    test_show_me_after_track_moves_nothing_more = None
