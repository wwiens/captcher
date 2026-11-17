"""Merged step runs — `continues: true`.

A step carrying the flag plays straight on from the one before it with no pause
between them, so a form's fields read as one thing to do rather than three.
Steps stay steps: each field keeps its own hint, demo_value and min_length, and
only the pacing and the counting merge.

Three surfaces have to agree, and none of them would fail loudly on their own:
author.validate (what may be saved), player.js (pacing, the beat counter and
the rail) and bridge.js (the arm-time re-check that keeps try mode from
deadlocking on a field the learner filled early).
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


def _fn(js, name):
    """The source of a top-level `function name(...) { … }`, brace-matched."""
    start = js.index(f"function {name}(")
    depth = 0
    for i in range(js.index("{", start), len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError(f"unbalanced braces reading {name}()")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = fixtures.make_walkthrough(new_member(), steps=3)
        self.walkthrough_id = self.row["id"]
        self.pages = {rel for rel, _ in author.snapshot_pages(self.walkthrough_id)}

    def _wt(self):
        return fixtures.walkthrough_def(self.walkthrough_id, steps=3)

    def _validate(self, walkthrough):
        return author.validate(walkthrough, self.walkthrough_id, self.pages)

    def test_absent_flag_is_valid(self):
        """Every walkthrough authored before the field existed omits it."""
        errors, _ = self._validate(self._wt())
        self.assertEqual(errors, [])

    def test_a_run_within_one_snapshot_is_valid(self):
        wt = self._wt()
        wt["steps"][1]["page"] = None          # same snapshot as step 0
        wt["steps"][1]["continues"] = True
        errors, _ = self._validate(wt)
        self.assertEqual(errors, [])

    def test_a_run_of_three_is_valid(self):
        wt = self._wt()
        for step in wt["steps"][1:]:
            step["page"] = None
            step["continues"] = True
        errors, _ = self._validate(wt)
        self.assertEqual(errors, [])

    def test_flag_on_the_first_step_is_rejected(self):
        """Nothing precedes it, so the player would have no run to join."""
        wt = self._wt()
        wt["steps"][0]["continues"] = True
        errors, _ = self._validate(wt)
        self.assertTrue(any("continues" in e for e in errors), errors)

    def test_flag_with_a_page_is_rejected(self):
        """A run plays without pause, which it cannot do across a page load —
        and the player's runStartOf() would report a heading from a snapshot
        that is no longer on screen."""
        wt = self._wt()
        wt["steps"][1]["continues"] = True     # fixture step 1 sets its own page
        self.assertIsNotNone(wt["steps"][1]["page"])
        errors, _ = self._validate(wt)
        self.assertTrue(any("continues" in e for e in errors), errors)

    def test_these_are_errors_not_warnings(self):
        """Same reason as the cursor style: a warning still writes the file,
        and the result would be a walkthrough whose counter and pacing
        disagree with its own steps."""
        wt = self._wt()
        wt["steps"][0]["continues"] = True
        errors, warnings = self._validate(wt)
        self.assertTrue(errors)
        self.assertFalse([w for w in warnings if "continues" in w])


class PlayerParityTests(unittest.TestCase):
    def setUp(self):
        self.js = _static("player.js")

    def test_pacing_never_reads_the_raw_flag(self):
        """continuesRun() carries the "not on step 0" half of the rule. A
        pacing path reading step.continues directly would treat a flag on the
        first step as a run — which validate() rejects on save, but an older
        file or a hand-edited one could still arrive carrying it. (The editor
        reads and writes the raw field on purpose; it is the one place that
        owns the value rather than consuming it.)"""
        self.assertIn("function continuesRun(i)", self.js)
        for name in ("actOnStep", "onStepSuccess", "onShowmeDone", "renderStepPanel",
                     "renderStepList", "computeBeats", "runStartOf"):
            with self.subTest(fn=name):
                self.assertNotIn(".continues", _fn(self.js, name))

    def test_a_continuing_step_skips_the_reading_pause(self):
        """Otherwise every field re-runs the start countdown — the exact
        stutter merging removes."""
        self.assertIn("skipStartPause || continuesRun(stepIdx)", _fn(self.js, "actOnStep"))

    def test_both_completion_paths_chain_through_a_run(self):
        """Show-me ends at showme-done and You-try at success; a run that
        chained on only one of them would pause in the other mode."""
        for name in ("onStepSuccess", "onShowmeDone"):
            with self.subTest(path=name):
                self.assertIn("continuesRun(stepIdx + 1)", _fn(self.js, name))

    def test_mid_run_success_is_silent(self):
        """The congratulation belongs to the beat, not to every field of it."""
        body = _fn(self.js, "onStepSuccess")
        chain = body.index("continuesRun(stepIdx + 1)")
        self.assertGreater(body.index("feedback(successMessage()"), chain,
                           "the success message must come after the mid-run return")

    def test_the_learner_counter_runs_on_beats(self):
        """steps.length would count a three-field form as three steps, which
        is what the merge exists to stop."""
        body = _fn(self.js, "renderStepPanel")
        self.assertIn("beatCount", body)
        self.assertIn("beatOf[stepIdx]", body)

    def test_the_editor_counter_still_runs_on_steps(self):
        """An editor works on the field in front of them and has to know which
        one it is — beats would make two of three fields unaddressable."""
        body = _fn(self.js, "renderStepPanel")
        self.assertIn("editing", body)
        self.assertIn("walkthrough.steps.length", body)

    def test_the_rail_lists_every_step_only_in_edit_mode(self):
        """Collapsed for learners, complete for editors: a merged field is
        otherwise unreachable, and its hint uneditable."""
        body = _fn(self.js, "renderStepList")
        self.assertIn('mode === "edit"', body)
        self.assertIn("continuesRun(i)", body)

    def test_the_rail_navigates_by_real_index(self):
        """The rail's positions stop matching step indices as soon as a run is
        collapsed, so data-step is what both the click handler and the title
        sync must read."""
        self.assertIn("li.dataset.step", self.js)
        self.assertIn('li[data-step="${stepIdx}"]', self.js)

    def test_the_editor_deletes_rather_than_writes_false(self):
        """An untouched walkthrough must not grow a field on its next save."""
        m = re.search(r'\$\("ed-continues"\)\.addEventListener\("change".*?\n  \}\);',
                      self.js, re.S)
        self.assertIsNotNone(m, "the ed-continues change handler is gone")
        self.assertIn("delete s.continues", m.group(0))
        self.assertIn("computeBeats()", m.group(0))
        self.assertIn("markDirty()", m.group(0))

    def test_the_checkbox_refuses_what_validate_refuses(self):
        """Both invalid shapes are unreachable in the editor, so an author
        meets the rule as a disabled control rather than a 422 on save."""
        body = _fn(self.js, "fillEditor")
        self.assertIn("stepIdx === 0", body)
        self.assertIn("step.page", body)

    def test_the_checkbox_exists_in_the_editor_markup(self):
        self.assertIn('id="ed-continues"', _static("player.html"))


class BridgeParityTests(unittest.TestCase):
    def setUp(self):
        self.js = _static("bridge.js")

    def test_expect_arms_through_the_rechecking_path(self):
        """Assigning `expectation` directly on the expect command is the
        regression: it would restore the deadlock below and nothing else would
        look wrong."""
        self.assertIn("armExpectation(m.action)", self.js)
        self.assertEqual(self.js.count("expectation = { action:"), 1,
                         "only armExpectation() may arm an expectation")

    def test_an_early_filled_field_is_re_checked_on_arm(self):
        """The deadlock this prevents: in a run the learner fills Email before
        its step comes round, the input listener discards it because a
        different action was expected, and when Email is finally expected no
        further input event is ever coming."""
        body = _fn(self.js, "armExpectation")
        self.assertIn("touched.has(el)", body)
        self.assertIn("succeed(el)", body)

    def test_only_a_touched_field_can_satisfy_an_arm(self):
        """A snapshot that ships with a field already filled must not
        auto-complete a step nobody has acted on."""
        body = _fn(self.js, "armExpectation")
        self.assertIn("if (!el || !touched.has(el)) return;", body)

    def test_both_event_paths_record_a_touch(self):
        """`change` covers selects and `input` covers typing; recording only
        one would leave the other deadlocking exactly as before. The handlers
        are named (onChange/onInput) so initDoc() can install them on every
        captured frame document, not just the top page's."""
        for handler in ("onInput", "onChange"):
            with self.subTest(handler=handler):
                body = _fn(self.js, handler)
                self.assertIn("touched.add(e.target)", body)
                self.assertIn(f'doc.addEventListener("{handler[2:].lower()}", {handler}, true)',
                              _fn(self.js, "initDoc"),
                              f"initDoc no longer installs {handler}")

    def test_a_touched_but_wrong_value_stays_silent_on_arm(self):
        """The learner is not acting at that moment — feedback about something
        they did two fields ago reads as the player malfunctioning."""
        body = _fn(self.js, "armExpectation")
        self.assertNotIn('type: "wrong"', body)


if __name__ == "__main__":
    unittest.main()
