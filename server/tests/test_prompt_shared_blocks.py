"""The schema text both system prompts share.

Nothing parses a prompt, so a schema change that lands in `validate()` and in
only one of the two prompts fails silently: the authoring pass writes the new
shape and the edit pass keeps describing the old one, or the reverse. The step
and action schema is therefore one string used by both, and these tests pin
that it stays that way — and that the vocabulary in it still matches what the
validator actually accepts.

Only the genuinely identical text is shared. The prose rules (frozen pages,
target picking, the three voices, merged runs) are stated separately in each
prompt on purpose — one pass builds a walkthrough, the other preserves one
while changing part of it — so there is deliberately no test here demanding
they match.
"""
import unittest

from captcher import author

PROMPTS = {"SYSTEM_PROMPT": author.SYSTEM_PROMPT,
           "EDIT_SYSTEM_PROMPT": author.EDIT_SYSTEM_PROMPT}


class SharedSchemaBlock(unittest.TestCase):
    def test_both_prompts_carry_the_shared_step_schema(self):
        for name, prompt in PROMPTS.items():
            with self.subTest(prompt=name):
                # the authoring prompt capitalises it (it follows a sentence);
                # compare on the part after the first letter
                self.assertIn(author.STEP_SCHEMA[1:], prompt)
                self.assertIn(author.SELECT_SEMANTICS, prompt)
                self.assertIn(author.DRAG_SEMANTICS, prompt)

    def test_the_schema_reference_appears_once_per_prompt(self):
        for name, prompt in PROMPTS.items():
            with self.subTest(prompt=name):
                self.assertEqual(prompt.count("Schema reference (walkthrough-def@1)"), 1)

    def test_every_valid_action_is_described_to_the_model(self):
        """The real drift guard: adding an action to VALID_ACTIONS without
        telling the passes it exists means the model never emits it."""
        for action in author.VALID_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f"`{action}`", author.STEP_SCHEMA)

    def test_only_described_actions_are_accepted(self):
        """And the reverse: an action described but not accepted would have
        the model emit something `validate()` rejects every time."""
        described = {a for a in ("click", "select", "input", "drag", "observe")
                     if f"`{a}` {{" in author.STEP_SCHEMA}
        self.assertEqual(described, author.VALID_ACTIONS)

    def test_the_top_level_schema_reaches_only_the_authoring_pass(self):
        """The edit pass is handed a complete walkthrough, so restating the
        top-level fields there is noise; the authoring pass writes them."""
        self.assertIn("completion", author.SYSTEM_PROMPT)
        self.assertIn(author.TOP_LEVEL_SCHEMA.strip(), author.SYSTEM_PROMPT)
        self.assertNotIn(author.TOP_LEVEL_SCHEMA.strip(), author.EDIT_SYSTEM_PROMPT)


class ClosersAreToolClosers(unittest.TestCase):
    """There is one path now. A prompt still asking for bare JSON back would
    be asking the model to bypass the validation gate."""

    def test_neither_prompt_asks_for_bare_json(self):
        for name, prompt in PROMPTS.items():
            with self.subTest(prompt=name):
                self.assertNotIn("Return ONLY", prompt)

    def test_each_prompt_names_its_submit_tool(self):
        self.assertIn("submit_walkthrough", author.SYSTEM_PROMPT)
        self.assertIn("submit_edit", author.EDIT_SYSTEM_PROMPT)

    def test_the_submit_tools_named_in_the_prompts_are_the_tools_supplied(self):
        self.assertEqual(author.SUBMIT_WALKTHROUGH_TOOL["name"], "submit_walkthrough")
        self.assertEqual(author.SUBMIT_EDIT_TOOL["name"], "submit_edit")
        for tool in (author.VALIDATE_TOOL, author.READ_PAGE_TOOL):
            with self.subTest(tool=tool["name"]):
                self.assertIn(tool["name"], author.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
