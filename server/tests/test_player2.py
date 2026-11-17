"""The popup-instruction player (static/player2.*), the default at /player/.

It is also what every export, embed, and SCORM package ships — see
export.EXPORT_PLAYER. It began as a deliberate fork of the classic player
(static/player.*), which now stays on only as the edit surface (?edit=1) and
the backup at /player1/; no learner-facing artefact is built from it any more.
These tests pin the properties that must not drift while the two coexist:

- v2 is playback-only: nothing in it may write the walkthrough, and its cursor
  choice is always a session override (the saved shape belongs to the editor,
  which stays in v1).
- The step card is positioned from bridge.js {cmd:"track"} rect reports and
  lives in the player document — the card itself never enters the captured
  page (brand governs chrome only; bridge.js draws cursor + spotlight, and
  reporting a rect draws nothing).
- The track protocol is additive: the v1 player never sends it, so v1 must be
  byte-for-byte indifferent to it.
- The cursor <select> keeps parity with author.CURSOR_STYLES, same as v1's
  (see test_cursor_style.py for the four-way v1 pin).
"""
import re
import unittest
from pathlib import Path

from captcher import author


def _static(name):
    return (Path(__file__).resolve().parents[1] / "captcher" / "static" / name).read_text(
        encoding="utf-8")


class Player2SurfaceTests(unittest.TestCase):
    def test_page_loads_its_own_assets(self):
        html = _static("player2.html")
        self.assertIn("/static/player2.css", html)
        self.assertIn("/static/player2.js", html)
        # a v2 page pulling v1 assets is the classic copy-fork slip
        self.assertNotIn("/static/player.css", html)
        self.assertNotIn('src="/static/player.js"', html)

    def test_the_step_card_replaces_the_side_panel(self):
        html = _static("player2.html")
        self.assertIn('id="step-pop"', html)
        self.assertNotIn('<aside class="panel">', html)
        self.assertNotIn('id="editor"', html)
        self.assertNotIn('id="step-list"', html)

    def test_step_buttons_toggle_exists_and_defaults_to_hidden(self):
        """The card is pure text by default — pacing buttons only appear when
        the viewer opts in. `=== "1"` is the pin: an absent stored value must
        read as hidden, so flipping the comparison (e.g. to !== "0") silently
        flips the default for every fresh viewer."""
        html = _static("player2.html")
        self.assertIn('id="opt-buttons"', html)
        js = _static("player2.js")
        self.assertIn('storeGet("sc-buttons") === "1"', js)

    def test_speed_slider_and_select_agree(self):
        """The topbar slider steps SPEED_STEPS by index; the settings select
        offers the same speeds by value. One list in player2.js feeds both —
        this pins the markup to it, so a speed added to one surface alone
        (or a slider max that no longer matches) fails loudly."""
        js = _static("player2.js")
        m = re.search(r"const SPEED_STEPS = \[([^\]]+)\]", js)
        self.assertIsNotNone(m, "SPEED_STEPS is gone or was renamed")
        steps = [float(s) for s in m.group(1).split(",")]
        html = _static("player2.html")
        sel = re.search(r'<select id="opt-speed">(.*?)</select>', html, re.S)
        self.assertIsNotNone(sel, "the speed <select> is gone or was renamed")
        offered = [float(v) for v in re.findall(r'value="([^"]+)"', sel.group(1))]
        self.assertEqual(offered, steps)
        slider = re.search(r'id="tb-speed-slider"[^>]*\bmax="(\d+)"', html)
        self.assertIsNotNone(slider, "the topbar speed slider is gone")
        self.assertEqual(int(slider.group(1)), len(steps) - 1)

    def test_the_card_titles_each_step_by_its_own_name(self):
        """Even inside a merged run: the card sits next to the field being
        worked on, so the heading follows it. The classic player holds the
        run-start title instead — that behaviour must not be "restored" here,
        so the run-start helper itself is absent."""
        js = _static("player2.js")
        self.assertNotIn("runStartOf", js)
        self.assertIn('$("panel-title").textContent = step.title', js)

    def test_mode_switch_on_the_final_step_restarts_the_walkthrough(self):
        """Mid-walkthrough a mode switch redoes the current step, but the
        final step is a recap whose only control is the Finish button —
        switching there means "run the whole thing that way", like the
        completion screen's buttons."""
        js = _static("player2.js")
        m = re.search(r"function switchMode\(m\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(m, "switchMode() is gone or was renamed")
        self.assertIn("return startWalkthrough(m)", m.group(0))

    def test_a_demonstration_ends_with_the_two_next_moves(self):
        """In show mode the final step offers "Return to catalog" and
        "You try" directly instead of a Finish that opened one more screen of
        the same choices. Try mode keeps Finish — its completion screen
        carries the run's stats. The catalog link must also vanish in
        standalone exports, which have no catalog to return to."""
        html = _static("player2.html")
        self.assertIn('id="btn-catalog"', html)
        self.assertIn('id="btn-try"', html)
        js = _static("player2.js")
        m = re.search(r"function showContinueButton\(\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(m, "showContinueButton() is gone or was renamed")
        self.assertIn('mode === "show"', m.group(0))
        self.assertIn("btn-catalog", m.group(0))
        self.assertIn("btn-try", m.group(0))
        css = _static("player2.css")
        self.assertIn("body.sc-embedded #btn-catalog", css)

    def test_the_last_step_never_advances_on_its_own(self):
        """Pacing carries the learner between steps, but leaving the last one
        is their decision — it always stops on the Finish button. Two paths
        can auto-advance (the post-step advance and the observe-step silent
        wait) and both must consult lastStepWaits()."""
        js = _static("player2.js")
        self.assertIn("function lastStepWaits()", js)
        sched = re.search(r"function scheduleAdvance\(\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(sched, "scheduleAdvance() is gone or was renamed")
        self.assertIn("lastStepWaits()", sched.group(0))
        # definition + the two auto-advance call sites
        self.assertGreaterEqual(js.count("lastStepWaits()"), 3)

    def test_settings_modal_offers_every_validated_style(self):
        html = _static("player2.html")
        m = re.search(r'<select id="opt-cursor">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m, "the cursor <select> is gone or was renamed")
        offered = set(re.findall(r'value="([^"]+)"', m.group(1)))
        self.assertEqual(offered, author.CURSOR_STYLES)

    def test_example_mode_hides_only_the_settings_entry_point(self):
        js = _static("player2.js")
        css = _static("player2.css")
        self.assertIn('if (!EMBED.example) document.body.classList.add("sc-embedded")', js)
        self.assertIn('if (EMBED.example) document.body.classList.add("sc-example")', js)
        self.assertIn("body.sc-example #btn-settings", css)
        self.assertIn('id="btn-settings"', _static("player2.html"))


class Player2PlaybackOnlyTests(unittest.TestCase):
    def test_nothing_writes_the_walkthrough(self):
        """No editor means no save path at all: a PUT appearing here means
        editing leaked into the fork and the two players now race on writes."""
        js = _static("player2.js")
        self.assertNotIn('method: "PUT"', js)
        self.assertNotIn("markDirty", js)
        self.assertNotIn("saveWalkthrough", js)

    def test_cursor_choice_is_session_only(self):
        """The shape belongs to the walkthrough; v2 cannot save it, so the
        change handler may only set the in-memory override — never the
        walkthrough object, never localStorage."""
        js = _static("player2.js")
        m = re.search(r'\$\("opt-cursor"\)\.addEventListener\("change".*?\n  \}\);', js, re.S)
        self.assertIsNotNone(m, "the opt-cursor change handler is gone")
        handler = m.group(0)
        self.assertIn("cursorOverride = ", handler)
        self.assertNotIn("walkthrough.cursor", handler)
        self.assertNotIn("storeSet", handler)

    def test_only_bridge_config_builds_a_config_payload(self):
        js = _static("player2.js")
        self.assertIn("function bridgeConfig()", js)
        self.assertIn("effectiveCursor()", js)
        self.assertEqual(js.count('cmd: "config"'), 1,
                         'only bridgeConfig() may build a {cmd:"config"} payload')


class TrackProtocolTests(unittest.TestCase):
    def test_bridge_implements_track_and_target_rect(self):
        js = _static("bridge.js")
        self.assertIn('m.cmd === "track"', js)
        self.assertIn('type: "target-rect"', js)

    def test_v2_positions_the_card_from_rect_reports(self):
        js = _static("player2.js")
        self.assertIn('cmd: "track"', js)
        self.assertIn('"target-rect"', js)

    def test_v1_never_sends_track(self):
        """The protocol addition is inert under the v1 player — that is what
        makes it safe to share bridge.js between the two while they coexist."""
        js = _static("player.js")
        self.assertNotIn('"track"', js)


if __name__ == "__main__":
    unittest.main()
