"""bridge.js puts captured scroll offsets back.

The recorder stamps every scrolled element with data-sc-scroll="top left"
just before serialization, because the snapshot keeps the DOM but not where
each scroll box stood. Without the restore, a form scrolled half-way down a
modal replayed at its top and an open dropdown's listbox — positioned against
the screen — landed over the wrong fields (a real Salesforce capture, FIELD-
NOTES.md 2026-10-06). Driven in headless Chromium, like test_drag_bridge.

Also pins that the stamp can never become a step's target selector.
"""
import unittest
from pathlib import Path

from captcher import digest

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover - environment without playwright
    sync_playwright = None

BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"

ROWS = "".join(f"<p style='height:40px;margin:0'>row {i}</p>" for i in range(60))
PAGE = f"""<!DOCTYPE html><html><head><style>
  .box{{height:200px;width:300px;overflow:auto}}
  .wide{{width:2000px}}
</style></head><body>
<div class="box" id="modal" data-sc-scroll="400 0">{ROWS}</div>
<div class="box" id="sideways" data-sc-scroll="0 150"><div class="wide">{ROWS}</div></div>
<div class="box" id="late" data-sc-scroll="300 0"><div id="grow" style="height:100px"></div></div>
<div class="box" id="bad" data-sc-scroll="nonsense">{ROWS}</div>
<div class="box" id="plain">{ROWS}</div>
<div id="host"></div>
<script>
  // A box too short to reach its offset until content arrives at load —
  // the late image/font case. Grows only once the load event fires.
  window.addEventListener("load", function () {{
    document.getElementById("grow").style.height = "1000px";
  }}, {{capture: true}});
  var root = document.getElementById("host").attachShadow({{mode: "open"}});
  root.innerHTML = `<div id="inner" style="height:100px;overflow:auto" data-sc-scroll="250 0">{ROWS}</div>`;
</script>
<script>__BRIDGE__</script>
</body></html>"""


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class ScrollRestore(unittest.TestCase):
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
        self.page = self.browser.new_page()
        self.page.set_content(PAGE.replace("__BRIDGE__", BRIDGE.read_text()),
                              wait_until="load")

    def tearDown(self):
        self.page.close()

    def offsets(self, sel):
        return self.page.evaluate(
            "s => { const e = s === '#inner' ? document.getElementById('host').shadowRoot"
            ".getElementById('inner') : document.querySelector(s);"
            " return [e.scrollTop, e.scrollLeft]; }", sel)

    def test_stamped_boxes_are_scrolled_back(self):
        self.assertEqual(self.offsets("#modal"), [400, 0])
        self.assertEqual(self.offsets("#sideways"), [0, 150])

    def test_inside_an_open_shadow_root(self):
        self.assertEqual(self.offsets("#inner"), [250, 0])

    def test_a_box_too_short_at_first_is_retried_on_load(self):
        self.assertEqual(self.offsets("#late"), [300, 0])

    def test_unstamped_and_malformed_boxes_are_left_alone(self):
        self.assertEqual(self.offsets("#plain"), [0, 0])
        self.assertEqual(self.offsets("#bad"), [0, 0])

    def test_the_retry_never_undoes_a_later_scroll(self):
        """Once the bridge (or the learner) has moved a box, the load retry
        must leave it: it would otherwise yank a revealed target away."""
        self.page.set_content(
            PAGE.replace("__BRIDGE__", BRIDGE.read_text())
                .replace('document.getElementById("grow").style.height = "1000px";',
                         'document.getElementById("grow").style.height = "1000px";'
                         'document.getElementById("late").scrollTop = 20;'),
            wait_until="load")
        self.assertEqual(self.offsets("#late"), [20, 0])


class StampIsNeverASelector(unittest.TestCase):
    def test_digest_skips_data_sc_attributes(self):
        tree = digest.parse_page('<div data-sc-scroll="191 0"><button>Go</button></div>'
                                 '<div><button>Go</button></div>')
        node = next(n for n in tree.nodes if n.attrs.get("data-sc-scroll"))
        self.assertFalse([c for c in digest._local_candidates(node) if "data-sc" in c])

    def test_recorder_pattern_matches_digest(self):
        recorder = (Path(__file__).resolve().parents[2] / "extension" / "recorder.js")
        if not recorder.is_file():
            self.skipTest("extension repo not checked out beside this one")
        src = recorder.read_text(encoding="utf-8")
        line = next(ln for ln in src.splitlines() if "const BOOKKEEPING_DATA =" in ln)
        js = line.split("/", 1)[1].rsplit("/", 1)[0]
        self.assertEqual(js, digest._BOOKKEEPING_DATA.pattern)
