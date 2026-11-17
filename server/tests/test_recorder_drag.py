"""The recorder records a drag — once the page has actually moved something.

`extension/recorder.js` listens for clicks, changes and inputs, so a
card dragged between board columns used to leave nothing in the capture. It
now records {type:"drag", selector, dropSelector} for a native HTML5 drag and
for a pointer-driven one, and only when the dragged element ends up in a
different lane: a refused drop, a reorder within a lane, or a plain click
records no drag. The selectors must be the ones the server's digest would
address the same elements by, so this runs the real recorder in headless
Chromium against a board whose drop handler re-renders the way Cadence does.

Same placement rationale as test_recorder_effects.py: the extension has no
suite of its own. Skipped when Playwright or its Chromium is unavailable.
"""
import os
import unittest
from pathlib import Path

from captcher import paths

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover
    sync_playwright = None

EXTENSION_DIR = paths.extension_checkout(Path(__file__).resolve().parents[1] / "captcher", os.environ.get("CAPTCHER_EXTENSION_DIR", ""))
RECORDER = EXTENSION_DIR / "recorder.js"

PAGE = """<!DOCTYPE html><html><head><title>Board</title><style>
  body{margin:0;font:14px sans-serif}
  .board{display:flex;gap:20px;padding:20px}
  .col{width:200px;min-height:240px;background:#eee;padding:8px}
  .deal,.item{background:#fff;padding:10px;margin:8px 0;border:1px solid #999}
  .lists{display:flex;gap:20px;padding:20px}
  .list{width:180px;min-height:100px;background:#dde;padding:8px;list-style:none;margin:0}
</style></head><body>
<h1>Pipeline</h1>
<div class="board" id="board"></div>
<div class="lists">
  <ul class="list" data-list="a"><li class="item" data-id="x">Plain item</li></ul>
  <ul class="list" data-list="b"></ul>
</div>
<script>
  // a Cadence-shaped board: state + full re-render on every change
  const deals = [{id: 15, co: "Ironvale Steel", stage: "Proposal"},
                 {id: 16, co: "Kestrel Insurance", stage: "Proposal"}];
  const stages = ["Qualifying", "Proposal", "Negotiation", "Locked"];
  function render() {
    document.getElementById("board").innerHTML = stages.map(s => `
      <div class="col" data-stage="${s}">
        <h3>${s}</h3><span class="n" data-testid="count-${s}">${deals.filter(d => d.stage === s).length}</span>
        ${deals.filter(d => d.stage === s).map(d =>
          `<article class="deal" draggable="true" data-id="${d.id}">${d.co}</article>`).join("")}
      </div>`).join("");
  }
  render();
  let dragId = null;
  const board = document.getElementById("board");
  board.addEventListener("dragstart", e => {
    const card = e.target.closest(".deal"); if (card) dragId = +card.dataset.id;
  });
  board.addEventListener("dragover", e => { if (e.target.closest(".col")) e.preventDefault(); });
  board.addEventListener("drop", e => {
    const col = e.target.closest(".col"); if (!col || dragId == null) return;
    e.preventDefault();
    const d = deals.find(x => x.id === dragId);
    if (col.dataset.stage !== "Locked") d.stage = col.dataset.stage;   // Locked refuses the drop
    dragId = null;
    render();
  });
  // a pointer-driven sortable: press, move, release moves the item
  let pressed = null;
  document.querySelector(".lists").addEventListener("mousedown", e => {
    const li = e.target.closest("li"); if (li) pressed = li;
  });
  document.addEventListener("mouseup", e => {
    if (!pressed) return;
    const list = document.elementFromPoint(e.clientX, e.clientY).closest("ul.list");
    if (list && list !== pressed.parentElement) list.appendChild(pressed);
    pressed = null;
  });
  window.__sent = [];
  window.__portMessages = [];
  const portListeners = [];
  const disconnectListeners = [];
  window.__grant = message => { for (const fn of portListeners) fn(message); };
  window.chrome = { runtime: {
    connect({ name }) {
      if (name !== "sc-recording-frame") throw new Error(`unexpected port: ${name}`);
      return {
        onMessage: { addListener(fn) { portListeners.push(fn); } },
        onDisconnect: { addListener(fn) { disconnectListeners.push(fn); } },
        postMessage(message) { window.__portMessages.push(message); },
        disconnect() { for (const fn of disconnectListeners) fn(); },
      };
    },
    sendMessage(m) { window.__sent.push(m); return Promise.resolve({}); },
  } };
</script>
<script>__SENSITIVE__</script>
<script>__RECORDER__</script>
</body></html>"""


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipUnless(RECORDER.is_file(), f"no recorder.js at {RECORDER}")
class RecorderDragTests(unittest.TestCase):
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
        self.page = self.browser.new_page(viewport={"width": 1000, "height": 600})
        self.page.set_content(self.html)
        self.assertEqual(self.page.evaluate("window.__sent"), [])
        # The recorder is inert until the worker grants this document a session.
        self.page.evaluate("window.__grant({type: 'start', sessionId: 'recorder-test-session'})")
        self.assertEqual(self.page.evaluate("window.__portMessages"), [{"type": "ready"}])

    def tearDown(self):
        self.page.evaluate("window.__grant({type: 'stop'})")
        self.page.close()

    def drag(self, src, dst):
        a = self.page.locator(src).bounding_box()
        b = self.page.locator(dst).bounding_box()
        self.page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] - 20, steps=10)
        self.page.mouse.up()
        self.page.wait_for_timeout(900)     # past the recorder's settle window

    def actions(self, a_type=None):
        sent = self.page.evaluate("window.__sent")
        acts = [m["action"] for m in sent if m.get("type") == "rc-action"]
        return [a for a in acts if a_type is None or a["type"] == a_type]

    def effects(self):
        return [m for m in self.page.evaluate("window.__sent") if m.get("type") == "rc-action-effects"]

    def test_a_native_drag_between_columns_is_recorded_by_both_ends(self):
        self.drag('article[data-id="15"]', 'div[data-stage="Negotiation"]')
        drags = self.actions("drag")
        self.assertEqual(len(drags), 1)
        d = drags[0]
        self.assertEqual(d["selector"], 'article[data-id="15"]')
        self.assertEqual(d["dropSelector"], 'div[data-stage="Negotiation"]')
        self.assertEqual(d["dropLabel"], "Negotiation")
        self.assertEqual(d["element"], "article")
        self.assertEqual(d["label"], "Ironvale Steel")
        self.assertEqual(d["heading"], "Proposal")   # nearest preceding heading: the column
        self.assertEqual(self.actions("click"), [])

    def test_the_effects_of_the_drop_are_reported_against_it(self):
        # The board re-renders wholesale on drop, so the counters the observer
        # snapshotted are detached by the time it flushes; it finds their
        # replacements by data-testid and reports the change. The window
        # opened before the page's drop handler ran (the before-values are
        # pre-drop) and was settled by the drag's own aid.
        self.drag('article[data-id="15"]', 'div[data-stage="Negotiation"]')
        d = self.actions("drag")[0]
        # Drag settling (600ms) starts a separate effects settle (300ms).
        # Waiting exactly 900ms races the latter timer; wait for its message.
        self.page.wait_for_function(
            "aid => window.__sent.some(m => m.type === 'rc-action-effects' && m.aid === aid)",
            arg=d["aid"], timeout=5000)
        reports = [e for e in self.effects() if e["aid"] == d["aid"]]
        self.assertEqual(len(reports), 1)
        changed = {e["selector"]: (e["before"], e["after"]) for e in reports[0]["effects"]}
        self.assertEqual(changed.get('span[data-testid="count-Negotiation"]'), ("0", "1"))
        self.assertEqual(changed.get('span[data-testid="count-Proposal"]'), ("2", "1"))
        self.assertNotIn('span[data-testid="count-Qualifying"]', changed)   # unchanged: not reported

    def test_a_refused_drop_records_nothing(self):
        self.drag('article[data-id="15"]', 'div[data-stage="Locked"]')
        self.assertEqual(self.actions("drag"), [])

    def test_a_reorder_within_the_lane_records_nothing(self):
        self.drag('article[data-id="15"]', 'article[data-id="16"]')
        self.assertEqual(self.actions("drag"), [])

    def test_a_pointer_drag_is_recorded_once_the_page_moves_the_item(self):
        self.drag('li[data-id="x"]', 'ul[data-list="b"]')
        drags = self.actions("drag")
        self.assertEqual(len(drags), 1)
        self.assertEqual(drags[0]["selector"], 'li[data-id="x"]')
        self.assertEqual(drags[0]["dropSelector"], 'ul[data-list="b"]')
        self.assertEqual(drags[0]["element"], "li")

    def test_a_click_is_a_click(self):
        self.page.click('article[data-id="15"]')
        self.page.wait_for_timeout(900)
        self.assertEqual(self.actions("drag"), [])
        self.assertEqual(len(self.actions("click")), 1)


if __name__ == "__main__":
    unittest.main()
