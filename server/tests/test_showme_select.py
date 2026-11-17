"""Show-me draws a select's option list open before it picks the value.

A native <select> cannot be opened from script, so a demoed select used to
change its value in place with nothing expanding — learners read it as the
dropdown never opening, and one author "fixed" it by editing `size="5"` into
the captured page. bridge.js now draws the menu itself (openOptions). The
planning half, optionRows(), is pure and runs here under node against fake
selects; the prompt half is pinned so the edit pass stops bending the page.

Skipped when node is unavailable.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from captcher import author

NODE = shutil.which("node")
BRIDGE = Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js"


def _extract_block():
    src = BRIDGE.read_text(encoding="utf-8")
    start = src.find("/* ---------- faux dropdown ----------")
    if start < 0:
        raise AssertionError("could not find the faux-dropdown block in bridge.js")
    end = src.find("  function showme(action) {", start)
    return src[start:end]


HARNESS = """
function norm(s) { return (s || "").replace(/\\s+/g, " ").trim(); }
function opt(text, extra) {
  var o = { textContent: text, value: text, selected: false, disabled: false, hidden: false };
  for (var k in (extra || {})) o[k] = extra[k];
  return o;
}
var out = {};

var plain = { options: [opt("All subscribers", {selected: true}), opt("Repeat customers"),
                        opt("Lapsed 90 days"), opt("New this month"), opt("Wholesale accounts")] };
var p = optionRows(plain, "New this month");
out.labels = p.rows.map(function (r) { return r.label; });
out.pick = p.pick;
out.current = p.rows.map(function (r) { return r.current; });
out.listbox_plain = isListbox(plain);

// options whose value differs from their label: matched by value first
var coded = { options: [opt("Pending", {value: "P"}), opt("Approved", {value: "A"})] };
out.pick_by_value = optionRows(coded, "A").pick;
out.pick_by_label = optionRows(coded, "Approved").pick;
out.pick_missing = optionRows(coded, "Rejected").pick;

// whitespace in the captured markup must not defeat the match
var spaced = { options: [opt("  New  this\\n month ")] };
out.pick_spaced = optionRows(spaced, "New this month").pick;

// hidden options are not drawn; disabled ones are drawn greyed
var mixed = { options: [opt("placeholder", {hidden: true}), opt("Off", {disabled: true}), opt("On")] };
var m = optionRows(mixed, "On");
out.mixed_labels = m.rows.map(function (r) { return r.label; });
out.mixed_disabled = m.rows.map(function (r) { return r.disabled; });
out.mixed_pick = m.pick;

out.listbox_size = isListbox({ options: [], size: 5 });
out.listbox_multiple = isListbox({ options: [], multiple: true });
out.listbox_size1 = isListbox({ options: [], size: 1 });

console.log(JSON.stringify(out));
"""


@unittest.skipIf(NODE is None, "node is not installed")
class OptionRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, "-e", _extract_block() + HARNESS],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_draws_every_option_and_rings_the_pick(self):
        self.assertEqual(self.out["labels"], ["All subscribers", "Repeat customers",
                                              "Lapsed 90 days", "New this month",
                                              "Wholesale accounts"])
        self.assertEqual(self.out["pick"], 3)
        self.assertEqual(self.out["current"], [True, False, False, False, False])
        self.assertFalse(self.out["listbox_plain"])

    def test_matches_the_way_setvalue_will(self):
        self.assertEqual(self.out["pick_by_value"], 1)
        self.assertEqual(self.out["pick_by_label"], 1)
        self.assertEqual(self.out["pick_missing"], -1)
        self.assertEqual(self.out["pick_spaced"], 0)

    def test_hidden_skipped_disabled_greyed(self):
        self.assertEqual(self.out["mixed_labels"], ["Off", "On"])
        self.assertEqual(self.out["mixed_disabled"], [True, False])
        self.assertEqual(self.out["mixed_pick"], 1)

    def test_listboxes_keep_the_direct_pick(self):
        self.assertTrue(self.out["listbox_size"])
        self.assertTrue(self.out["listbox_multiple"])
        self.assertFalse(self.out["listbox_size1"])


class ShowmeWiringTest(unittest.TestCase):
    def test_select_demo_opens_the_menu_before_setting_the_value(self):
        src = BRIDGE.read_text(encoding="utf-8")
        sel = src.find('action.type === "select"', src.find("function showme("))
        self.assertGreater(sel, 0)
        opened = src.find("openOptions(el, action.value", sel)
        # the value is the option the step means (optionValueFor: by value,
        # then by label), falling back to the raw value when none matches
        set_at = src.find('setValue(el, picked === null ? action.value : picked, ["input", "change"])', sel)
        self.assertGreater(opened, 0)
        self.assertGreater(set_at, opened, "the value must change after the menu is drawn")
        self.assertIn(".br-options{", src)

    def test_edit_prompt_forbids_page_hacks_for_dropdowns(self):
        self.assertIn("do not add `size` to a select", author.EDIT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
