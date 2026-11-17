"""Clicking a checkbox or radio in the player has to leave it ticked.

Every other click on a frozen snapshot is suppressed on purpose — onClick
preventDefaults so a captured page cannot navigate or run its own handlers.
For a checkable control that suppression removes the click's only visible
result, so bridge.js reflects the state itself. This runs the real
reflectCheck() out of bridge.js under node against fake elements; nothing in
it touches a browser API beyond the handful stubbed below.

Skipped when node is unavailable.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"


def _extract_block():
    """The checkable-controls section of bridge.js, verbatim."""
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.find("/* ---------- checkable controls ---------- */")
    if start < 0:
        raise AssertionError(
            "could not find the checkable-controls block in bridge.js — if it "
            "was renamed or moved, update this test")
    end = src.find("/* ---------- click handling ---------- */", start)
    return src[start:end]


# A checkbox is a plain object here; reflectCheck only reads tagName/type/
# checked and calls getAttribute/querySelector, all stubbed in the harness.
# ownerDocument mirrors the real bridge, where a label's `for` is resolved in
# the element's OWN document — a captured iframe's, not necessarily the top's.
HARNESS = """
var REGISTRY = {};
var DOC = { getElementById: function (id) { return REGISTRY[id] || null; } };
function input(type, checked) {
  return { nodeType: 1, tagName: "INPUT", type: type, checked: !!checked,
           ownerDocument: DOC,
           getAttribute: function () { return null; },
           querySelector: function () { return null; } };
}
function labelFor(id, inner) {
  return { nodeType: 1, tagName: "LABEL",
           ownerDocument: DOC,
           getAttribute: function (a) { return a === "for" ? id : null; },
           querySelector: function () { return inner || null; } };
}

var out = {};

var box = input("checkbox", false);
out.returns_true = reflectCheck(box);
out.now_checked = box.checked;

out.idempotent_returns_false = reflectCheck(box);
out.still_checked = box.checked;

var radio = input("RADIO", false);          // type casing must not matter
out.radio_checked = (reflectCheck(radio), radio.checked);

var wrapped = input("checkbox", false);
var lbl = labelFor(null, wrapped);
out.wrapping_label = (reflectCheck(lbl), wrapped.checked);

var pointed = input("checkbox", false);
REGISTRY["agree"] = pointed;
out.label_for = (reflectCheck(labelFor("agree", null)), pointed.checked);

var text = input("text", false);
out.text_input_ignored = reflectCheck(text);

out.button_ignored = reflectCheck({ nodeType: 1, tagName: "BUTTON",
                                    getAttribute: function () { return null; },
                                    querySelector: function () { return null; } });
out.null_ignored = reflectCheck(null);

console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node is not installed")
class ReflectCheckTests(unittest.TestCase):
    def setUp(self):
        proc = subprocess.run([NODE, "-e", _extract_block() + HARNESS],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"node failed: {proc.stderr}")
        self.out = json.loads(proc.stdout)

    def test_a_click_ticks_the_box(self):
        self.assertTrue(self.out["returns_true"])
        self.assertTrue(self.out["now_checked"])

    def test_it_drives_to_checked_rather_than_toggling(self):
        """Show-me replays, and stepping back and forward does not reload the
        page — a toggle would come back UNCHECKED on the second pass."""
        self.assertFalse(self.out["idempotent_returns_false"])
        self.assertTrue(self.out["still_checked"])

    def test_radios_are_covered_and_type_casing_is_ignored(self):
        self.assertTrue(self.out["radio_checked"])

    def test_it_finds_the_input_behind_a_label(self):
        """pickable() snaps to `label`, so an authored target frequently names
        the label rather than the control it points at or wraps."""
        self.assertTrue(self.out["wrapping_label"])
        self.assertTrue(self.out["label_for"])

    def test_everything_else_is_left_alone(self):
        for key in ("text_input_ignored", "button_ignored", "null_ignored"):
            self.assertFalse(self.out[key], key)


class WiredIntoBothPathsTests(unittest.TestCase):
    """Show-me and You-try fail differently — the demo never touched the
    control, the learner's click was preventDefaulted — so both call sites
    have to be present or the box ticks in only one mode."""

    @staticmethod
    def _body(src, decl):
        """One top-level function out of bridge.js's IIFE. Nested callbacks are
        indented deeper, so the next two-space `function ` ends the body."""
        start = src.index(decl)
        rest = src[start + len(decl):]
        end = rest.find("\n  function ")
        return rest if end < 0 else rest[:end]

    def test_both_call_sites_are_present(self):
        src = BRIDGE.read_text(encoding="utf-8")
        showme = self._body(src, "function showme")
        self.assertIn("reflectCheck(el)", showme)

        on_click = self._body(src, "function onClick")
        self.assertIn("reflectCheck(target)", on_click)
        # the wrong-click case must stay suppressed
        self.assertIn("e.preventDefault()", on_click)


if __name__ == "__main__":
    unittest.main()
