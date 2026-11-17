"""The authoring prompt's one-shot quality exemplar.

prompts/example-walkthrough.json is fed to the model as a reference for tone
and selector style. It is not inert: the model copies what it sees.

The exemplar was originally built from a pre-rename capture whose folders were
named "00 - Initial state/page.html". The model duly copied that path — and the
step id next to it — into its output, and every authoring run failed validation
with `page "00 - Initial state/page.html" is not a captured snapshot`. Captures
have used step-NNN for a long time.

So the exemplar must stay consistent with what a real capture looks like today.
"""
import json
import re
import unittest
from pathlib import Path

from captcher import author

EXEMPLAR = Path(author.ROOT) / author.EXAMPLE_WALKTHROUGH_FILE

# What ingest.convert() produces for every step, from every current capture.
STEP_PAGE_RE = re.compile(r"^step-\d{3}/page\.html$")


class ExemplarTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(EXEMPLAR.read_text(encoding="utf-8"))

    def test_exemplar_exists_and_loads(self):
        """It silently returned "" for a long time because the path was wrong;
        an exemplar that is not there is worse than useless, it is invisible."""
        block = author.example_walkthrough_block()
        self.assertTrue(block, "example_walkthrough_block() returned nothing")
        self.assertIn("quality, tone", block)

    def test_page_paths_use_the_current_capture_convention(self):
        pages = [s.get("page") for s in self.data["steps"]]
        self.assertTrue(any(pages), "exemplar has no page paths at all")
        for page in pages:
            if page is None:
                continue          # null == "same page as the previous step"
            with self.subTest(page=page):
                self.assertRegex(
                    page, STEP_PAGE_RE,
                    "exemplar page paths must look like a real capture "
                    "(step-NNN/page.html) — the model copies them verbatim")

    def test_exemplar_declares_the_current_schema(self):
        """It shipped as "lesson-def@1" — the pre-rename id — long after the
        rename, and was injected verbatim into every authoring run. Nothing
        caught it because run_pass() overwrites `schema` unconditionally, so the
        only symptom was the model being shown a dead value on every request.
        Same failure mode as the page paths above: the exemplar teaches."""
        self.assertEqual(self.data["schema"], "walkthrough-def@1")

    def test_exemplar_carries_no_todo_placeholders(self):
        """example_walkthrough_block() drops the exemplar entirely if it finds
        TODO, so a placeholder left in here silently disables it."""
        self.assertNotIn("TODO", json.dumps(self.data))

    def test_the_block_warns_against_copying(self):
        block = author.example_walkthrough_block()
        lowered = block.lower()
        self.assertIn("never copy", lowered)
        self.assertIn("page", lowered)

    def test_system_prompt_names_no_specific_capture_folder(self):
        """The prompt used to say the first step "often belongs on
        '00 - Initial state'", naming a folder that no capture produces."""
        self.assertNotIn("00 - Initial state", author.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
