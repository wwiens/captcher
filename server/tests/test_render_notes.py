"""Render-accuracy verdicts feeding back into authoring.

render_check.py judges whether the player renders a captured snapshot the way
the user saw it. Those verdicts are read back in two places, both advisory:
validate() warns when steps play on a page the check called broken, and the AI
edit pass is told not to describe what does not render. The vision call and the
headless renderer are never involved here — the verdicts are written directly.

They are a `render_check` column on the walkthrough row now rather than a
`render_check.json` sidecar beside it, so `store.load_render_check` takes the
row it is handed instead of an id it has to go and resolve. The helper below is
still called `verdicts()` after the shape it writes; the tests that used to
write the file now call `store.save_render_check`.
"""
import unittest

from captcher import author
from captcher import store
from conftest import new_member

from . import agentic, fixtures

PAGE0 = "step-000/page.html"
PAGE1 = "step-001/page.html"


def sidecar(pages, checked_at="2026-07-30T09:00:00Z"):
    return {"schema": "render-check@1", "model": "claude-opus-5",
            "checked_at": checked_at, "pages": pages}


def verdict(severity, differences=(), **extra):
    return {"status": severity, "severity": severity, "matches": severity == "ok",
            "differences": list(differences), **extra}


class RenderNotesTests(unittest.TestCase):
    """render_notes() keeps what an author can act on and drops the rest."""

    def test_only_pages_that_did_not_render_faithfully(self):
        notes = author.render_notes(sidecar({
            PAGE0: verdict("ok"),
            PAGE1: verdict("broken", ["the left navigation is blank"]),
        }))
        self.assertEqual(list(notes), [PAGE1])
        self.assertEqual(notes[PAGE1]["differences"], ["the left navigation is blank"])

    def test_minor_is_carried(self):
        notes = author.render_notes(sidecar({PAGE0: verdict("minor", ["logo missing"])}))
        self.assertEqual(notes[PAGE0]["severity"], "minor")

    def test_states_of_the_check_are_not_states_of_the_page(self):
        # no-baseline / pending / error say something about the checker, not
        # about what a learner would see — there is nothing to write around.
        notes = author.render_notes(sidecar({
            PAGE0: {"status": "no-baseline"},
            PAGE1: {"status": "error", "error": "the page did not produce a render"},
        }))
        self.assertEqual(notes, {})

    def test_no_check_and_junk_sidecars_are_simply_silence(self):
        for results in (None, {}, {"pages": None}, {"pages": {PAGE0: "not a dict"}}):
            self.assertEqual(author.render_notes(results), {}, results)

    def test_aliases_carry_their_canonical_verdict(self):
        # run_check copies a verdict onto byte-identical duplicates, and a step's
        # `page` may be any of those paths, so both must be lookup-able.
        notes = author.render_notes(sidecar({
            PAGE0: verdict("broken", ["blank panel"]),
            PAGE1: verdict("broken", ["blank panel"], alias_of=PAGE0),
        }))
        self.assertEqual(sorted(notes), [PAGE0, PAGE1])

    def test_block_is_none_when_there_is_nothing_to_say(self):
        self.assertIsNone(author.render_notes_block({}))
        self.assertIsNone(author.render_notes_block(None))

    def test_block_names_the_page_its_severity_and_the_differences(self):
        block = author.render_notes_block(
            author.render_notes(sidecar({PAGE1: verdict("broken", ["the chart is blank"])})),
            checked_at="2026-07-30T09:00:00Z")
        self.assertIn("RENDER ACCURACY", block)
        self.assertIn("2026-07-30T09:00:00Z", block)
        self.assertIn(f"{PAGE1} — broken: the chart is blank", block)
        # the model must not treat a rendering fault as a reason to restructure
        self.assertIn("never add, drop or reorder steps", block)


class ValidateWarningTests(unittest.TestCase):
    """validate() turns a broken page into one warning, never an error."""

    def setUp(self):
        self.row = fixtures.make_walkthrough(new_member(), steps=2)
        self.wt_id = self.row["id"]
        self.wt = fixtures.walkthrough_def(self.wt_id, steps=2)
        self.pages = {PAGE0, PAGE1}

    def check(self, notes, walkthrough=None):
        return author.validate(walkthrough or self.wt, self.wt_id, self.pages,
                               render_notes=notes)

    def test_broken_page_warns_and_still_saves(self):
        errors, warnings = self.check(
            author.render_notes(sidecar({PAGE1: verdict("broken", ["the form is unstyled"])})))
        self.assertEqual(errors, [])          # advisory: the write is not blocked
        hits = [w for w in warnings if w.startswith(PAGE1)]
        self.assertEqual(len(hits), 1, warnings)
        self.assertIn("the form is unstyled", hits[0])
        self.assertIn("step-1", hits[0])      # the step that plays there

    def test_one_warning_per_page_not_per_step(self):
        # every step onto the same broken page: the warning must not be repeated
        for step in self.wt["steps"]:
            step["page"] = PAGE1
        _, warnings = self.check(
            author.render_notes(sidecar({PAGE1: verdict("broken", ["blank"])})))
        hits = [w for w in warnings if w.startswith(PAGE1)]
        self.assertEqual(len(hits), 1, warnings)
        self.assertIn("2 step(s)", hits[0])

    def test_minor_does_not_warn(self):
        # still usable for training; a line per imperfect page would bury the
        # target and selector warnings next to it
        _, warnings = self.check(
            author.render_notes(sidecar({PAGE1: verdict("minor", ["softer images"])})))
        self.assertEqual([w for w in warnings if w.startswith(PAGE1)], [])

    def test_a_broken_page_no_step_plays_on_does_not_warn(self):
        for step in self.wt["steps"]:
            step["page"] = PAGE0
        _, warnings = self.check(
            author.render_notes(sidecar({PAGE1: verdict("broken", ["blank"])})))
        self.assertEqual([w for w in warnings if w.startswith(PAGE1)], [])

    def test_a_page_inherited_through_a_null_page_still_warns(self):
        # `page: null` means "same snapshot as the previous step" — the step
        # plays on the broken page just as much as the one that loaded it
        self.wt["steps"][0]["page"] = PAGE1
        self.wt["steps"][1]["page"] = None
        _, warnings = self.check(
            author.render_notes(sidecar({PAGE1: verdict("broken", ["blank"])})))
        hits = [w for w in warnings if w.startswith(PAGE1)]
        self.assertEqual(len(hits), 1, warnings)
        self.assertIn("2 step(s)", hits[0])

    def test_no_render_check_changes_nothing(self):
        self.assertEqual(self.check(None), self.check({}))
        self.assertEqual(self.check(None)[1], [])


class EditPromptTests(unittest.TestCase):
    """run_edit puts the verdicts in the prompt — the pass that has them."""

    def setUp(self):
        self.owner = new_member()
        self.row = fixtures.make_walkthrough(self.owner, steps=2)
        self.wt_id = self.row["id"]
        # run_edit refuses a draft. "Draft or authored" is the `authored_at`
        # column's question now, not a substring scan for "TODO" — which could
        # not tell one of ingest's placeholders from an author legitimately
        # writing the word.
        store.mark_authored(self.wt_id)

    def write_sidecar(self, pages):
        store.save_render_check(self.wt_id, sidecar(pages))

    def run_edit(self):
        def build(_params):
            wt = fixtures.walkthrough_def(self.wt_id, steps=2)
            return {"walkthrough": wt, "capture_edits": [],
                    "summary": "Reworded step 1."}

        # run_edit sends content blocks (cached digest prefix + volatile
        # suffix); the assertions below care about the text in order
        with agentic.submitting_edit(build) as log:
            result = author.run_edit(self.wt_id, "reword step 1")
        return {"system": agentic.system_text(log[0]),
                "user": agentic.prompt_text(log[0])}, result

    def test_the_block_also_reaches_the_authoring_pass_prompt(self):
        """run_pass used to get only the validation warning; verdicts already
        on the row (the opt-in pre-check, or a re-author after review) now feed
        the prompt too, exactly like the edit pass."""
        wt_id = fixtures.make_walkthrough(self.owner, title="Second", steps=2)["id"]
        store.save_render_check(
            wt_id, sidecar({PAGE1: verdict("broken", ["the right sidebar is blank"])}))

        def build(_params):
            return fixtures.walkthrough_def(wt_id, steps=2)

        with agentic.submitting_walkthrough(build) as log:
            author.run_pass(wt_id)

        user = agentic.prompt_text(log[0])
        self.assertIn("RENDER ACCURACY", user)
        self.assertIn("the right sidebar is blank", user)
        # after the digests, before the ask — same placement as the edit pass
        self.assertGreater(user.index("RENDER ACCURACY"), user.index("=== PAGE "))

    def test_the_block_reaches_the_prompt_after_the_digests(self):
        self.write_sidecar({PAGE1: verdict("broken", ["the right sidebar is blank"])})
        captured, _ = self.run_edit()
        user = captured["user"]
        self.assertIn("RENDER ACCURACY", user)
        self.assertIn("the right sidebar is blank", user)
        # it annotates the digests, so it follows them and precedes the ask
        self.assertGreater(user.index("RENDER ACCURACY"), user.index("=== PAGE "))
        self.assertLess(user.index("RENDER ACCURACY"),
                        user.index("Now apply the instruction and submit "
                                   "the result with submit_edit."))
        self.assertIn("RENDER ACCURACY", captured["system"])

    def test_nothing_is_added_when_every_page_rendered_faithfully(self):
        self.write_sidecar({PAGE0: verdict("ok"), PAGE1: verdict("ok")})
        captured, _ = self.run_edit()
        self.assertNotIn("RENDER ACCURACY", captured["user"])

    def test_nothing_is_added_when_no_check_has_been_run(self):
        captured, result = self.run_edit()
        self.assertNotIn("RENDER ACCURACY", captured["user"])
        self.assertEqual(result["summary"], "Reworded step 1.")

    def test_a_broken_page_warns_through_the_edit_result(self):
        self.write_sidecar({PAGE1: verdict("broken", ["the form is unstyled"])})
        _, result = self.run_edit()
        self.assertTrue(any(w.startswith(PAGE1) for w in result["warnings"]),
                        result["warnings"])


class StoreReaderTests(unittest.TestCase):
    """The reader sits in store.py because render_check imports author.

    It takes the ROW, not an id: the verdicts are a column on a row every
    caller is already holding, so resolving one again would be a second query
    for something already in hand. The parse-failure cases the JSON sidecar
    needed are gone with it — jsonb either stores a document or refuses the
    write — so what is left to be total about is the shape."""

    def test_no_check_and_a_junk_column_both_read_as_no_check(self):
        row = fixtures.make_walkthrough(new_member())
        self.assertIsNone(store.load_render_check(store.get(row["id"])))
        for bad in (None, [], "a string", 3):
            with self.subTest(stored=bad):
                store.save_render_check(row["id"], bad)
                self.assertIsNone(store.load_render_check(store.get(row["id"])))

    def test_a_stored_verdict_set_reads_back_whole(self):
        row = fixtures.make_walkthrough(new_member())
        results = sidecar({PAGE1: verdict("broken", ["blank"])})
        store.save_render_check(row["id"], results)
        self.assertEqual(store.load_render_check(store.get(row["id"])), results)

    def test_a_missing_row_reads_as_no_check_rather_than_raising(self):
        """The catalog build hands this whatever `store.get` returned."""
        self.assertIsNone(store.load_render_check(None))


if __name__ == "__main__":
    unittest.main()
