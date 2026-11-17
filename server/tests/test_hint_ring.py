"""The hint ring shows on the target, and the target itself never fades.

A captured app styles its own buttons, often with a box-shadow behind a
selector more specific than the bridge's `.br-hint`. When the ring was a plain
`box-shadow` declaration, such a page silently replaced it and no ring drew;
and the pulse animated the element's `opacity`, fading the very control the
learner is told to use. The replay check caught both on a real capture.

Runs the bridge in headless Chromium through Playwright; skipped when either is
missing, like the other browser tests.
"""
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover - environment without playwright
    sync_playwright = None

BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"

PAGE = """<!DOCTYPE html><html><head><style>
  .topbar .btn-primary{background:#087f73;color:#fff;border:0;padding:10px 16px;
                       box-shadow:0 2px 3px rgba(8,127,115,.125)}
</style></head><body>
<header class="topbar"><button class="btn-primary" id="new">+ New deal</button></header>
<script>
  window.__msgs = [];
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type) window.__msgs.push(e.data);
  });
</script>
<script>__BRIDGE__</script>
</body></html>"""

RING = "rgb(41, 182, 255)"


class HintRingSource(unittest.TestCase):
    """Static half: runs everywhere, browser or not."""

    def test_the_pulse_animates_the_ring_not_the_element(self):
        src = BRIDGE.read_text(encoding="utf-8")
        for name in ("brSpot", "brPulse"):
            start = src.index("@keyframes " + name)
            frames = src[start:src.index("}}", start)]
            self.assertNotIn("opacity", frames, name)
            self.assertIn("box-shadow", frames, name)


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class HintRingInTheBrowser(unittest.TestCase):
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

    def _hinted(self, **context):
        page = self.browser.new_page(viewport={"width": 600, "height": 300}, **context)
        self.addCleanup(page.close)
        page.set_content(self.html)
        page.wait_for_function("window.__msgs.some(m => m.type === 'bridge-ready')")
        page.evaluate("window.postMessage({cmd: 'hint', action: {type: 'click', "
                      "target: {selector: '#new'}}}, '*')")
        page.wait_for_function("document.querySelector('#new').classList.contains('br-hint')")
        return page

    def _samples(self, page):
        return page.evaluate("""async () => {
            const el = document.querySelector('#new'), out = [];
            for (let i = 0; i < 8; i++) {
                const s = getComputedStyle(el);
                out.push({opacity: s.opacity, shadow: s.boxShadow});
                await new Promise(r => setTimeout(r, 200));
            }
            return out;
        }""")

    def test_the_ring_beats_the_pages_own_box_shadow_and_the_button_stays_opaque(self):
        samples = self._samples(self._hinted())
        self.assertEqual({s["opacity"] for s in samples}, {"1"})
        late = samples[1:]           # past the 0.14s arrival
        self.assertTrue(all("41, 182, 255" in s["shadow"] for s in late), late)

    def test_under_reduced_motion_the_ring_holds_still(self):
        samples = self._samples(self._hinted(reduced_motion="reduce"))
        self.assertEqual({s["opacity"] for s in samples}, {"1"})
        self.assertTrue(all(RING in s["shadow"] for s in samples), samples)


if __name__ == "__main__":
    unittest.main()
