"""`accept_any` on select actions must exist in every place that has an
opinion about it, or a walkthrough saved with the flag plays strict anyway:

- static/bridge.js decides success/wrong — both the live change listener and
  the armExpectation re-check (merged runs) must honour the flag
- both authoring prompts describe the schema the model may emit

(author.validate() deliberately has no opinion: `value` stays required because
Show-me still demos it, and a stray extra field cannot break playback.)
"""
import unittest
from pathlib import Path

from captcher import author

BRIDGE = (Path(__file__).resolve().parents[1] / "captcher" / "static" / "bridge.js").read_text(
    encoding="utf-8")


class AcceptAnyParityTests(unittest.TestCase):
    def test_bridge_honours_the_flag_in_both_select_checks(self):
        self.assertIn("act.accept_any && target.value", BRIDGE)      # change listener
        self.assertIn("action.accept_any && el.value", BRIDGE)       # armExpectation

    def test_both_prompts_document_the_flag(self):
        for prompt in (author.SYSTEM_PROMPT, author.EDIT_SYSTEM_PROMPT):
            self.assertIn("accept_any?", prompt)          # in the schema reference
            self.assertIn("accept any choice", prompt)    # and explained, not just listed


if __name__ == "__main__":
    unittest.main()
