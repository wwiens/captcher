"""An input step a learner can finish by typing the model answer.

You-try completes an input step once `min_length` characters are typed
(bridge.js). A rejected "0" or "31" authored with the usual min_length of 3,
or a blank demo_value standing for "clear the field", left the learner who
did exactly what Show-me did stuck on the step for good.
"""
import unittest

from captcher import author


def _wt(*actions):
    return {"steps": [{"action": a} for a in actions]}


class ForceInvariantsTests(unittest.TestCase):
    def test_min_length_never_exceeds_the_model_answer(self):
        wt = _wt({"type": "input", "demo_value": "0", "min_length": 3},
                 {"type": "input", "demo_value": "31", "min_length": 5})
        author._force_invariants(wt, {}, "x")
        self.assertEqual([s["action"]["min_length"] for s in wt["steps"]], [1, 2])

    def test_a_missing_or_zero_min_length_becomes_typable(self):
        wt = _wt({"type": "input", "demo_value": "Hello world", "min_length": 0},
                 {"type": "input", "demo_value": "ok"})
        author._force_invariants(wt, {}, "x")
        self.assertEqual([s["action"]["min_length"] for s in wt["steps"]], [3, 2])

    def test_a_sensible_min_length_is_left_alone(self):
        wt = _wt({"type": "input", "demo_value": "Picking this up", "min_length": 10})
        author._force_invariants(wt, {}, "x")
        self.assertEqual(wt["steps"][0]["action"]["min_length"], 10)


class ValidateTests(unittest.TestCase):
    def _errors(self, demo):
        wt = {"schema": "walkthrough-def@1", "id": "x", "title": "T", "steps": [{
            "id": "s1", "title": "Type", "page": "step-000/page.html",
            "instruction": "Type it.", "showme_caption": "We type it.", "hint": "The box.",
            "action": {"type": "input", "target": {"selector": "#q"}, "demo_value": demo,
                       "min_length": 1}}]}
        errors, _ = author.validate(wt, "x", {"step-000/page.html"})
        return [e for e in errors if "demo_value" in e]

    def test_a_whitespace_demo_value_is_an_error(self):
        self.assertTrue(self._errors(" "))

    def test_a_real_demo_value_is_fine(self):
        self.assertFalse(self._errors("Denver"))


if __name__ == "__main__":
    unittest.main()
