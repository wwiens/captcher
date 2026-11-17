"""The per-walkthrough Show-me cursor.

`cursor` is an optional top-level field on a walkthrough, chosen in the
in-player editor and pushed to bridge.js in the {cmd:"config"} message. Three
things have to agree about it: author.validate (what may be saved), bridge.js
(what can be drawn) and the editor's <select> (what can be chosen). These tests
pin them together — a shape added to one but not the others is the failure
mode, and it is silent otherwise.
"""
import re
import unittest
from pathlib import Path

from captcher import author
from conftest import new_member

from tests import fixtures


def _static(name):
    return (Path(__file__).resolve().parents[1] / "captcher" / "static" / name).read_text(
        encoding="utf-8")


class CursorValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = fixtures.make_walkthrough(new_member())
        self.walkthrough_id = self.row["id"]
        self.pages = {rel for rel, _ in author.snapshot_pages(self.walkthrough_id)}

    def _walkthrough(self):
        return fixtures.walkthrough_def(self.walkthrough_id)

    def _validate(self, walkthrough):
        return author.validate(walkthrough, self.walkthrough_id, self.pages)

    def test_absent_cursor_is_valid(self):
        """Every walkthrough authored before this field existed has no cursor."""
        wt = self._walkthrough()
        errors, _ = self._validate(wt)
        self.assertEqual(errors, [])

    def test_each_supported_style_is_valid(self):
        for style in sorted(author.CURSOR_STYLES):
            with self.subTest(cursor=style):
                wt = self._walkthrough()
                wt["cursor"] = style
                errors, _ = self._validate(wt)
                self.assertEqual(errors, [])

    def test_unknown_style_is_rejected(self):
        """An error, not a warning: warnings still write the file, and a shape
        bridge.js cannot draw would leave Show-me with no cursor at all."""
        wt = self._walkthrough()
        wt["cursor"] = "crosshair"
        errors, _ = self._validate(wt)
        self.assertTrue(any("cursor" in e for e in errors), errors)


class CursorParityTests(unittest.TestCase):
    def test_dot_is_the_documented_default(self):
        self.assertIn("dot", author.CURSOR_STYLES)

    def test_bridge_handles_every_validated_style(self):
        """bridge.js maps the style onto a class; "dot" is the bare element."""
        js = _static("bridge.js")
        for style in author.CURSOR_STYLES - {"dot"}:
            with self.subTest(cursor=style):
                self.assertIn(f'"{style}"', js)
                self.assertIn(f"#br-cursor.br-{style}", js)

    def test_settings_modal_offers_every_validated_style(self):
        html = _static("player.html")
        m = re.search(r'<select id="opt-cursor">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m, "the cursor <select> is gone or was renamed")
        offered = set(re.findall(r'value="([^"]+)"', m.group(1)))
        self.assertEqual(offered, author.CURSOR_STYLES)

    def test_cursor_row_is_grouped_as_walkthrough_owned(self):
        """It lives among viewer preferences that persist to localStorage, so
        the grouping is the only thing telling an author it gets saved."""
        html = _static("player.html")
        m = re.search(r'<div class="ps-group" id="ps-walkthrough-group">(.*?)</div>\s*</div>',
                      html, re.S)
        self.assertIsNotNone(m, "the walkthrough-owned settings group is gone")
        self.assertIn('id="opt-cursor"', m.group(1))

    def test_the_label_says_where_the_choice_lands(self):
        """The row is offered in both modes and the <select> looks identical in
        each, so the scope label is the only thing distinguishing a saved
        change from a session-only one."""
        js = _static("player.js")
        m = re.search(r'\$\("ps-cursor-scope"\)\.textContent\s*=\s*(.*?);', js, re.S)
        self.assertIsNotNone(m, "the cursor scope label is no longer kept in sync")
        self.assertIn('mode === "edit"', m.group(1))

    def test_editing_saves_and_playback_only_overrides(self):
        """A viewer cannot PUT the walkthrough, so their choice must stay in
        memory: no markDirty (nothing to save) and no storeSet (the shape
        belongs to the walkthrough, not to the browser)."""
        js = _static("player.js")
        m = re.search(r'\$\("opt-cursor"\)\.addEventListener\("change".*?\n  \}\);', js, re.S)
        self.assertIsNotNone(m, "the opt-cursor change handler is gone")
        handler = m.group(0)
        self.assertIn("walkthrough.cursor = value", handler)
        self.assertIn("markDirty()", handler)
        self.assertIn("cursorOverride = value", handler)
        self.assertNotIn("storeSet", handler)

    def test_entering_edit_mode_drops_a_playback_override(self):
        """Otherwise the editor would show the experiment while Save wrote the
        stored value — the select and the file silently disagreeing."""
        js = _static("player.js")
        m = re.search(r"function switchMode\(m\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(m, "switchMode() is gone or was renamed")
        self.assertIn("cursorOverride = null", m.group(0))

    def test_the_bridge_is_sent_the_effective_cursor(self):
        """An override has to reach the captured page, or the control would
        change the label and nothing else."""
        js = _static("player.js")
        m = re.search(r"function bridgeConfig\(\) \{.*?\n\}", js, re.S)
        self.assertIsNotNone(m, "bridgeConfig() is gone")
        self.assertIn("effectiveCursor()", m.group(0))

    def test_only_bridge_config_builds_a_config_payload(self):
        """Three sites send one (fresh page, speed change, cursor change) and
        all go through bridgeConfig(). A second hand-rolled payload is the
        drift this pins: it would keep working while silently omitting the
        cursor. Counting call sites would just break on every new one, so this
        checks the literal instead."""
        js = _static("player.js")
        self.assertIn("function bridgeConfig()", js)
        self.assertEqual(js.count('cmd: "config"'), 1,
                         'only bridgeConfig() may build a {cmd:"config"} payload')


if __name__ == "__main__":
    unittest.main()
