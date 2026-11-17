"""Where the popup player's step card goes, and how the learner can move it.

The card used to avoid only the step's target, so for a drag it dodged the
card being picked up and parked on the column it was headed for. Placement
is now a pure function (player2.js bestCardPlacement) that also keeps off
`avoid` boxes — the bridge reports a drag's drop zone alongside the target
({cmd:"track", avoid}) — and the card can be dragged by its header and stays
put until double-clicked. The pure half runs here under node; the wiring is
pinned by inspection, like the rest of test_player2.py.

A <select> is the other target that is bigger than it looks: opening it
drops a menu under the control, and "below the target" — the card's fallback
when the sides are cramped, as they are for a select at the right end of a top
bar — lands on exactly that menu. So the bridge reports the menu's footprint
alongside a tracked select's rect (bridge.js menuFootprint), sized the way
Show-me's faux menu is drawn, and the card keeps off it like any avoid box.

Skipped under node's absence for the placement cases.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parents[1] / "captcher" / "static"


def _static(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _placement_fn():
    js = _static("player2.js")
    start = js.find("function bestCardPlacement(")
    end = js.find("\nfunction positionStepCard(", start)
    if start < 0 or end < 0:
        raise AssertionError("bestCardPlacement / positionStepCard not found in player2.js")
    return js[start:end]


def _menu_fns():
    js = _static("bridge.js")
    start = js.find("  function menuTop(")
    end = js.find("\n  function isMenuSelect(", start)
    if start < 0 or end < 0:
        raise AssertionError("menuTop / menuFootprint not found in bridge.js")
    return js[start:end]


# A 1400x900 stage, the card 300x180. The picked-up card sits at the bottom
# of a 260px-wide column; the next column (the drop zone) is directly to its
# right, so "right of the target" would land on the zone.
HARNESS = """
const out = {};
const pop = [300, 180], stage = [1400, 900], pad = 14;
const anchor = { x: 300, y: 600, w: 240, h: 90 };
const zone = { x: 580, y: 120, w: 260, h: 700 };
out.plain = bestCardPlacement(anchor, [], ...pop, ...stage, pad);
out.avoiding = bestCardPlacement(anchor, [zone], ...pop, ...stage, pad);
// nothing fits: the card is bigger than the room left — it still returns a box
out.cramped = bestCardPlacement({ x: 0, y: 0, w: 1400, h: 900 }, [], ...pop, ...stage, pad);
// The Cadence CRM owner filter: a select at the right end of the top bar,
// with the New deal button to its right. Right is off the stage, above is
// off the stage, so the card falls to "below" — onto the open menu.
const owner = { x: 986, y: 12, w: 104, h: 28 };
const menu = { x: 986, y: 40, w: 128, h: 140 };
out.select_bare = bestCardPlacement(owner, [], ...pop, ...stage, pad);
out.select_menu = bestCardPlacement(owner, [menu], ...pop, ...stage, pad);
// Peoplebase's request-status filter: a select near the LEFT edge, a tall
// card. Left clamps onto the control; right grazes its menu footprint.
out.left_edge = bestCardPlacement({ x: 159, y: 264, w: 101, h: 40 },
                                  [{ x: 159, y: 304, w: 160, h: 132 }],
                                  300, 290, 1600, 952, pad);
console.log(JSON.stringify(out));
"""

# The menu footprint: a 1534x955 viewport, the select 130x35 at the top right
# (the capture behind "Add a New Deal to the Pipeline"), five options at 14px;
# then the same control near the bottom, where the menu has to open upward.
MENU_HARNESS = """
const window = { innerHeight: 955 };
const out = {};
out.top = menuFootprint({ left: 1233, top: 15, width: 130, height: 35 }, 5, 14, 1534);
out.bottom = menuFootprint({ left: 1233, top: 900, width: 130, height: 35 }, 5, 14, 1534);
out.long = menuFootprint({ left: 100, top: 15, width: 200, height: 35 }, 40, 14, 1534);
out.edge = menuFootprint({ left: 1500, top: 15, width: 130, height: 35 }, 5, 14, 1534);
console.log(JSON.stringify(out));
"""


def _overlaps(card, box):
    ox = min(card["x"] + card["w"], box["x"] + box["w"]) - max(card["x"], box["x"])
    oy = min(card["y"] + card["h"], box["y"] + box["h"]) - max(card["y"], box["y"])
    return ox > 0 and oy > 0


@unittest.skipIf(NODE is None, "node is not installed")
class PlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, "-e", _placement_fn() + HARNESS],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_without_avoid_boxes_the_card_goes_right_of_the_target(self):
        p = self.out["plain"]
        self.assertEqual(p["left"], 300 + 240 + 18)

    def test_a_drop_zone_to_the_right_keeps_the_card_off_both_ends(self):
        p = self.out["avoiding"]
        card = {"x": p["left"], "y": p["top"], "w": 300, "h": 180}
        for name, box in (("drop zone", {"x": 580, "y": 120, "w": 260, "h": 700}),
                          ("picked-up card", {"x": 300, "y": 600, "w": 240, "h": 90})):
            ox = min(card["x"] + card["w"], box["x"] + box["w"]) - max(card["x"], box["x"])
            oy = min(card["y"] + card["h"], box["y"] + box["h"]) - max(card["y"], box["y"])
            self.assertTrue(ox <= 0 or oy <= 0, f"the card covers the {name}: {p}")
        # and it did move off the preferred side, which the zone occupies
        self.assertNotEqual(p["left"], 300 + 240 + 18)

    def test_a_select_in_the_top_bar_keeps_the_card_off_its_open_menu(self):
        owner = {"x": 986, "y": 12, "w": 104, "h": 28}
        menu = {"x": 986, "y": 40, "w": 128, "h": 140}
        bare = self.out["select_bare"]
        # this is the bug: with only the control to avoid, the card parks under it
        self.assertTrue(_overlaps({"x": bare["left"], "y": bare["top"], "w": 300, "h": 180}, menu))
        p = self.out["select_menu"]
        card = {"x": p["left"], "y": p["top"], "w": 300, "h": 180}
        self.assertFalse(_overlaps(card, menu), f"the card covers the menu: {p}")
        self.assertFalse(_overlaps(card, owner), f"the card covers the select: {p}")

    def test_the_card_never_covers_the_target_to_spare_an_avoid_box(self):
        p = self.out["left_edge"]
        card = {"x": p["left"], "y": p["top"], "w": 300, "h": 290}
        self.assertFalse(_overlaps(card, {"x": 159, "y": 264, "w": 101, "h": 40}))

    def test_a_hopeless_layout_still_yields_a_box_inside_the_stage(self):
        p = self.out["cramped"]
        self.assertGreaterEqual(p["left"], 14)
        self.assertGreaterEqual(p["top"], 14)
        self.assertLessEqual(p["left"] + 300, 1400 - 14)


@unittest.skipIf(NODE is None, "node is not installed")
class MenuFootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, "-e", _menu_fns() + MENU_HARNESS],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_the_menu_hangs_under_the_control_at_least_as_wide_as_the_faux_one(self):
        m = self.out["top"]
        self.assertEqual((m["x"], m["y"]), (1233, 50))
        self.assertEqual(m["width"], 160)          # .br-options min-width
        # five rows of 14px * 1.5 + 10px padding, plus the list's own padding
        self.assertEqual(m["height"], 5 * 31 + 10)

    def test_near_the_bottom_it_opens_upward_like_the_faux_menu(self):
        m = self.out["bottom"]
        self.assertEqual(m["y"] + m["height"], 900)

    def test_a_long_list_is_capped_at_the_faux_menu_max_height(self):
        self.assertEqual(self.out["long"]["height"], 264)

    def test_at_the_right_edge_it_keeps_the_faux_menu_floor(self):
        # the faux menu never goes under 160px either, viewport or not
        self.assertEqual(self.out["edge"]["width"], 160)


class WiringTests(unittest.TestCase):
    def test_a_tracked_select_reports_its_menu_as_a_box_to_avoid(self):
        bridge = _static("bridge.js")
        self.assertIn("if (isMenuSelect(el)) avoid.push(selectMenuRect(el));", bridge)
        # listboxes already show their options; nothing opens under them
        self.assertIn("!isListbox(el)", bridge[bridge.find("function isMenuSelect("):])
        # the faux menu and the footprint decide below/above the same way
        open_options = bridge[bridge.find("function openOptions("):bridge.find("function showme(")]
        self.assertIn("menuTop(r, list.offsetHeight)", open_options)
        self.assertIn("menuTop(r, h)", bridge[bridge.find("function menuFootprint("):])

    def test_a_drag_step_tracks_its_drop_zone_as_a_box_to_avoid(self):
        js = _static("player2.js")
        track = js[js.find("function trackStepTarget("):js.find("function bestCardPlacement(")]
        self.assertIn('step.action.type === "drag"', track)
        self.assertIn("[step.action.drop_target]", track)
        self.assertIn('cmd: "track", target, avoid', track)
        self.assertIn("lastAvoidRects.map(toStage)", js)

    def test_the_bridge_reports_avoid_rects_with_the_target_rect(self):
        bridge = _static("bridge.js")
        self.assertIn("trackedAvoid = Array.isArray(m.avoid) ? m.avoid : []", bridge)
        self.assertIn('send({ type: "target-rect", rect: el ? viewportRect(el) : null, avoid: avoid, seq: trackedSeq })',
                      bridge)

    # One move per step. The card used to be placed from the previous step's
    # rect on render, from a report for the previous target that was already
    # in flight, and against the target before Show me scrolled it to centre
    # -- each a second move after the first.
    def test_every_track_carries_a_sequence_the_bridge_echoes(self):
        js = _static("player2.js")
        track = js[js.find("function trackStepTarget("):js.find("function bestCardPlacement(")]
        self.assertIn("trackSeq += 1", track)
        self.assertIn("seq: trackSeq", track)
        bridge = _static("bridge.js")
        self.assertIn("trackedSeq = m.seq", bridge)

    def test_a_report_for_another_track_is_dropped(self):
        js = _static("player2.js")
        handler = js[js.find('m.type === "target-rect"'):js.find('m.type === "success"')]
        self.assertIn("if (m.seq !== trackSeq) return;", handler)
        self.assertLess(handler.find("m.seq !== trackSeq"), handler.find("positionStepCard("))

    def test_the_card_holds_still_until_its_own_rect_arrives(self):
        js = _static("player2.js")
        goto = js[js.find("function gotoStep("):js.find("function effectivePage(")]
        self.assertLess(goto.find("awaitingRect = true"), goto.find("renderStepCard()"))
        place = js[js.find("function positionStepCard("):js.find("function initCardDrag(")]
        self.assertIn("if (awaitingRect && !pinnedCard) return;", place)

    def test_show_me_scrolls_the_target_before_the_first_report(self):
        js = _static("player2.js")
        track = js[js.find("function trackStepTarget("):js.find("function bestCardPlacement(")]
        self.assertIn('reveal: !!target && mode === "show"', track)
        bridge = _static("bridge.js")
        branch = bridge[bridge.find('m.cmd === "track"'):bridge.find('m.cmd === "pick"')]
        self.assertIn('scrollLocal(shown, "reveal")', branch)
        # the same rule moveCursorTo uses, so its later scroll is a no-op
        self.assertIn('scrollLocal(el, "reveal");', bridge[bridge.find("function moveCursorTo("):])
        self.assertLess(branch.find("scrollLocal("), branch.find("sendTrackedRect()"))

    def test_the_v1_player_is_indifferent_to_the_protocol(self):
        # v1 never sends track; nothing here may make it start
        js = _static("player.js")
        self.assertNotIn("m.avoid", js)
        self.assertNotIn("avoid:", js)

    def test_the_card_is_dragged_by_its_header_and_pinned(self):
        js = _static("player2.js")
        self.assertIn('const CARD_GRIP = ".step-eyebrow, h2";', js)
        self.assertIn("pinnedCard = { left: pop.offsetLeft, top: pop.offsetTop }", js)
        # a pinned card wins over automatic placement…
        self.assertIn("if (pinnedCard) {", js[js.find("function positionStepCard("):])
        # …until the header is double-clicked, or a new run starts
        self.assertIn("now - lastTap < DOUBLE_TAP_MS", js)
        start = js[js.find("function startWalkthrough("):js.find("function switchMode(")]
        self.assertIn("pinnedCard = null", start)

    def test_the_grip_is_styled_as_one(self):
        css = _static("player2.css")
        self.assertIn(".step-pop .step-eyebrow, .step-pop h2 { cursor: grab; user-select: none; }", css)
        self.assertIn(".step-pop.dragging { transition: none;", css)

    def test_the_markup_is_untouched(self):
        """The header carries no new attributes: the grip is wired in JS so the
        rendered page (under tests/golden/player.html) does not change."""
        html = _static("player2.html")
        self.assertNotIn("draggable", html)
        self.assertNotIn("grip", html)


if __name__ == "__main__":
    unittest.main()
