"""The digest-based authoring prompt and the ref-target contract.

The authoring pass used to send every captured page's prepared HTML — a
typical Salesforce capture reached ~1M input tokens and tripped the preflight
budget. It now sends per-page digests (digest.py) and the model targets
elements by ref; resolve_refs() substitutes the server-verified selectors
before validation. These tests pin that plumbing:

- byte-identical duplicate snapshots collapse to one digest with aliases,
- the prompt carries digests, not markup, and stays inside the budget,
- ref substitution, unknown refs, and cross-page refs behave as contracted,
- a page the digest builder cannot handle degrades to capped prepared HTML,
- validate() resolves in-grammar selectors against the trees and warns.
"""
import unittest
from unittest import mock

from captcher import author
from captcher import digest
from captcher import store
from conftest import new_member

from tests import agentic, fixtures


def modal_page(step_label, extra=""):
    """A page with enough handles for the digest ladder to work with."""
    return f"""<!DOCTYPE html><html><head><title>{step_label}</title></head><body>
<h1>{fixtures.PAGE_MARKER}: {step_label}</h1>
<button name="open-modal">Add a Contact</button>
<div role="dialog" aria-modal="true" class="modal">
  <label for="fn">First name</label><input id="fn" type="text">
  <select aria-label="Reason"><option value="">--None--</option>
    <option value="fraud">Fraud</option></select>
  <button class="btn-primary">Save</button>
</div>{extra}
</body></html>"""


def make_capture(owner_id, pages, title="Digest Walkthrough", draft=True):
    """A walkthrough row plus one `step-NNN/page.html` object per html.

    Returns (walkthrough_id, [capture-relative paths]). Was a directory of
    files under a temp root; the pages are objects under the row's own uuid
    prefix now, and the id comes back from the insert rather than being chosen
    here — see tests/fixtures.py.
    """
    definition = fixtures.walkthrough_def(steps=len(pages))
    if draft:
        # What run_pass looks at is the `authored_at` column, but the prompt
        # still shows the model the draft it is replacing.
        definition["title"] = "TODO"
    row = store.create(owner_id, title, definition)
    rels = []
    for i, html in enumerate(pages):
        rel = f"step-{i:03d}/page.html"
        store.put_capture(row["id"], rel, html)
        rels.append(rel)
    return row["id"], rels


class UniquePages(unittest.TestCase):
    def test_duplicates_collapse_with_alias_map(self):
        a, b = modal_page("A"), modal_page("B")
        pages = [("s0/page.html", a), ("s1/page.html", b),
                 ("s2/page.html", b), ("s3/page.html", a)]
        uniques, alias_map = author.unique_pages(pages)
        self.assertEqual([r for r, _ in uniques], ["s0/page.html", "s1/page.html"])
        self.assertEqual(alias_map, {"s2/page.html": "s1/page.html",
                                     "s3/page.html": "s0/page.html"})

    def test_distinct_pages_pass_through(self):
        pages = [("s0/page.html", modal_page("A")), ("s1/page.html", modal_page("B"))]
        uniques, alias_map = author.unique_pages(pages)
        self.assertEqual(len(uniques), 2)
        self.assertEqual(alias_map, {})


class DigestPrompt(unittest.TestCase):
    def test_prompt_carries_digests_not_markup_and_fits_the_budget(self):
        dup = modal_page("dup")
        pages = [(f"step-{i:03d}/page.html", dup) for i in range(13)]
        blocks, ref_table, alias_map, trees = author.build_page_digests(pages)
        user = author.build_user_prompt("any-id", "", {"steps": []}, blocks)
        self.assertEqual(len(blocks), 1)                       # 13 files, 1 digest
        self.assertEqual(len(alias_map), 12)
        self.assertIn("INTERACTIVE ELEMENTS", user)
        self.assertIn("step-012/page.html", user)              # aliases listed
        self.assertNotIn("<div role", user)                    # no raw markup
        self.assertLess(author.estimate_tokens(author.SYSTEM_PROMPT)
                        + author.estimate_tokens(user), author.INPUT_TOKEN_BUDGET)

    def test_every_ref_targets_verified_unique(self):
        blocks, ref_table, _, trees = author.build_page_digests(
            [("step-000/page.html", modal_page("A"))])
        self.assertGreater(len(ref_table), 3)
        tree = trees["step-000/page.html"]
        for entry in ref_table.values():
            hits = digest.match(tree, entry["target"])
            self.assertEqual(len(hits), 1, entry)

    def test_unparseable_page_falls_back_to_capped_prepared_html(self):
        ok = modal_page("A")
        with mock.patch.object(author.digest, "parse_page",
                               side_effect=digest.DigestError("boom")):
            blocks, ref_table, _, trees = author.build_page_digests(
                [("step-000/page.html", ok)])
        self.assertEqual(len(blocks), 1)
        self.assertIn("raw HTML fallback", blocks[0])
        self.assertIn("Add a Contact", blocks[0])              # prepared HTML, present
        self.assertEqual(ref_table, {})
        self.assertEqual(trees, {})

    def test_fallback_pages_are_capped(self):
        huge = "<html><body>" + "<p>x</p>" * 200_000 + "</body></html>"
        with mock.patch.object(author.digest, "parse_page",
                               side_effect=digest.DigestError("boom")):
            blocks, *_ = author.build_page_digests([("step-000/page.html", huge)])
        self.assertLess(len(blocks[0]), author.FALLBACK_PAGE_CHARS + 1_000)
        self.assertIn("truncated", blocks[0])


class ResolveRefs(unittest.TestCase):
    def setUp(self):
        self.ref_table = {
            "e1": {"page": "step-000/page.html",
                   "target": {"selector": 'button[name="open-modal"]'}},
            "e2": {"page": "step-001/page.html",
                   "target": {"selector": "div.modal button.btn-primary",
                              "text_exact": "Save"}},
        }
        self.alias_map = {"step-002/page.html": "step-001/page.html"}

    def wt(self, steps):
        return {"steps": steps}

    def test_known_ref_is_substituted_and_model_extras_discarded(self):
        wt = self.wt([{"id": "s1", "page": "step-000/page.html",
                       "action": {"type": "click",
                                  "target": {"ref": "e1", "selector": "junk",
                                             "text": "junk"}}}])
        errors = author.resolve_refs(wt, self.ref_table, self.alias_map)
        self.assertEqual(errors, [])
        self.assertEqual(wt["steps"][0]["action"]["target"],
                         {"selector": 'button[name="open-modal"]'})

    def test_unknown_ref_is_an_error(self):
        wt = self.wt([{"id": "s1", "page": "step-000/page.html",
                       "action": {"type": "click", "target": {"ref": "e99"}}}])
        errors = author.resolve_refs(wt, self.ref_table, self.alias_map)
        self.assertEqual(len(errors), 1)
        self.assertIn("e99", errors[0])

    def test_ref_on_the_wrong_page_is_an_error(self):
        wt = self.wt([{"id": "s1", "page": "step-000/page.html",
                       "action": {"type": "click", "target": {"ref": "e2"}}}])
        errors = author.resolve_refs(wt, self.ref_table, self.alias_map)
        self.assertEqual(len(errors), 1)
        self.assertIn("step-001/page.html", errors[0])

    def test_alias_pages_share_the_canonical_refs(self):
        # the step plays on a duplicate snapshot; its ref lives on the canonical
        wt = self.wt([{"id": "s1", "page": "step-002/page.html",
                       "action": {"type": "click", "target": {"ref": "e2"}}}])
        self.assertEqual(author.resolve_refs(wt, self.ref_table, self.alias_map), [])

    def test_page_null_inherits_the_previous_page(self):
        wt = self.wt([
            {"id": "s1", "page": "step-000/page.html",
             "action": {"type": "click", "target": {"ref": "e1"}}},
            {"id": "s2", "page": None,
             "action": {"type": "click", "target": {"ref": "e1"}}},
        ])
        self.assertEqual(author.resolve_refs(wt, self.ref_table, self.alias_map), [])

    def test_plain_selector_targets_pass_through_untouched(self):
        target = {"selector": ".legacy", "text": "Keep me"}
        wt = self.wt([{"id": "s1", "page": "step-000/page.html",
                       "action": {"type": "click", "target": dict(target)}}])
        self.assertEqual(author.resolve_refs(wt, self.ref_table, self.alias_map), [])
        self.assertEqual(wt["steps"][0]["action"]["target"], target)


class ValidateWithTrees(unittest.TestCase):
    def setUp(self):
        self.wt_id, self.rels = make_capture(new_member(), [modal_page("A")])
        pages = author.snapshot_pages(self.wt_id)
        _, _, self.alias_map, self.trees = author.build_page_digests(pages)
        self.valid_pages = set(self.rels)

    def wt(self, target):
        # No `capture_dir`: it is not in the schema any more, and validate()
        # no longer requires it.
        return {
            "schema": "walkthrough-def@1", "id": self.wt_id, "title": "T",
            "track": "t", "level": "beginner", "estimated_minutes": 2,
            "intro": {"scenario": "s", "objectives": ["o"]},
            "steps": [{"id": "s1", "title": "T", "page": self.rels[0],
                       "instruction": "i", "showme_caption": "c", "hint": "h",
                       "action": {"type": "click", "target": target}}],
            "completion": {"summary": "s", "success_criteria": ["c"]},
        }

    def check(self, target):
        return author.validate(self.wt(target), self.wt_id, self.valid_pages,
                               trees=self.trees, alias_map=self.alias_map)

    def test_unique_selector_no_warning(self):
        errors, warnings = self.check({"selector": 'button[name="open-modal"]'})
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_ambiguous_selector_warns(self):
        errors, warnings = self.check({"selector": "button"})
        self.assertEqual(errors, [])
        self.assertTrue(any("matches 2 elements" in w for w in warnings), warnings)

    def test_missing_selector_target_warns(self):
        errors, warnings = self.check({"selector": "button.does-not-exist"})
        self.assertEqual(errors, [])
        self.assertTrue(any("matches nothing" in w for w in warnings), warnings)

    def test_out_of_grammar_selector_gets_no_tree_verdict(self):
        errors, warnings = self.check({"selector": "button:first-of-type"})
        self.assertEqual(errors, [])
        self.assertFalse(any("matches" in w for w in warnings), warnings)


class RunPassEndToEnd(unittest.TestCase):
    """run_pass with the model stubbed: it returns ref targets and the stored
    walkthrough carries the substituted verified selectors."""

    def setUp(self):
        self.wt_id, self.rels = make_capture(new_member(), [modal_page("A")])

    def draft_reply(self, params=None, ref=None):
        wt = fixtures.walkthrough_def(self.wt_id, steps=1)
        wt["steps"][0]["page"] = self.rels[0]
        if ref is not None:
            wt["steps"][0]["action"] = {"type": "click", "target": {"ref": ref}}
        return wt

    def test_ref_targets_come_back_as_verified_selectors(self):
        def build(params):
            # pick the modal's Save button by ref, wherever it landed
            ref = agentic.ref_matching(agentic.prompt_text(params), "Save")
            return self.draft_reply(ref=ref)

        with agentic.submitting_walkthrough(build) as log:
            result = author.run_pass(self.wt_id)

        self.assertIn("INTERACTIVE ELEMENTS", agentic.prompt_text(log[0]))
        # the authoring pass is the quality-critical call and thinks harder
        # than the module default
        self.assertEqual(log[0]["output_config"]["effort"], author.AUTHOR_EFFORT)
        written = store.get(self.wt_id)["definition"]
        target = written["steps"][0]["action"]["target"]
        self.assertNotIn("ref", target)
        tree = digest.parse_page(modal_page("A"))
        self.assertEqual(len(digest.match(tree, target)), 1)
        self.assertEqual(result["steps_after"], 1)

    def test_unknown_ref_fails_validation_and_leaves_the_draft(self):
        """A bad ref is rejected at submit, and the model gets the error back.
        Here it never fixes it, so the loop runs out of turns with nothing
        salvageable and the draft survives untouched."""
        def build(params):
            return {"walkthrough": self.draft_reply(ref="e999")}

        with agentic.repeating(build):
            with self.assertRaises(author.AuthorError) as cm:
                author.run_pass(self.wt_id)
        self.assertIn("e999", str(cm.exception))
        self.assertEqual(store.get(self.wt_id)["definition"]["title"], "TODO")
        self.assertEqual(store.authoring_state(store.get(self.wt_id)), {})


class RunEditEndToEnd(unittest.TestCase):
    """run_edit sends digests and resolves ref targets the same way."""

    def setUp(self):
        self.wt_id, _ = make_capture(
            new_member(), [modal_page("A"), modal_page("B")], draft=False)
        store.mark_authored(self.wt_id)

    def test_edit_prompt_carries_digests_and_refs_resolve(self):
        def build(params):
            text = agentic.prompt_text(params)
            wt = fixtures.walkthrough_def(self.wt_id, steps=2)
            # step 2 plays on step-001 — the ref must come from THAT page's
            # inventory (picking page-000's Save is the cross-page error)
            page_001 = text[text.index("=== PAGE step-001/page.html ==="):]
            wt["steps"][1]["action"] = {
                "type": "click",
                "target": {"ref": agentic.ref_matching(page_001, "Save")}}
            return {"walkthrough": wt, "capture_edits": [],
                    "summary": "Retargeted step 2."}

        with agentic.submitting_edit(build) as log:
            result = author.run_edit(self.wt_id, "retarget step 2 to the save button")

        text = agentic.prompt_text(log[0])
        self.assertIn("INTERACTIVE ELEMENTS", text)
        # no raw page markup — interactive elements' start tags appear only as
        # src: excerpts, and non-interactive markup not at all
        self.assertNotIn("<h1>", text)
        written = store.get(self.wt_id)["definition"]
        target = written["steps"][1]["action"]["target"]
        self.assertNotIn("ref", target)
        self.assertTrue(target.get("selector"))
        self.assertEqual(result["summary"], "Retargeted step 2.")


class PromptContentBlocks(unittest.TestCase):
    """The vision-grounding and caching shape of the prompts.

    run_pass interleaves each page's capture-time screenshot with its digest
    (so hints are written from what is on screen, not DOM order), and
    run_edit leads with a cache-marked digest prefix (stable across repeated
    edits) with the volatile context and instruction last.
    """

    def test_run_pass_inlines_page_screenshots_after_their_digests(self):
        from tests.test_render_check import PNG
        wt_id, rels = make_capture(new_member(), [modal_page("A")])
        store.put_capture(wt_id, "step-000/screenshot.png", PNG)

        def build(params):
            ref = agentic.ref_matching(agentic.prompt_text(params), "Save")
            wt = fixtures.walkthrough_def(wt_id, steps=1)
            wt["steps"][0]["page"] = rels[0]
            wt["steps"][0]["action"] = {"type": "click", "target": {"ref": ref}}
            return wt

        with agentic.submitting_walkthrough(build) as log:
            author.run_pass(wt_id)

        user = agentic.content_blocks(log[0])
        images = [i for i, b in enumerate(user) if b.get("type") == "image"]
        self.assertEqual(len(images), 1)
        i = images[0]
        # the caption names the page, and the page's digest precedes it
        self.assertIn("Capture-time screenshot of step-000/page.html",
                      user[i - 1]["text"])
        joined_before = "".join(b.get("text", "") for b in user[:i])
        self.assertIn("=== PAGE step-000/page.html", joined_before)

    def test_run_edit_leads_with_a_cache_marked_digest_prefix(self):
        wt_id = fixtures.make_walkthrough(new_member(), steps=2)["id"]
        store.mark_authored(wt_id)

        def build(params):
            return {"walkthrough": fixtures.walkthrough_def(wt_id, steps=2),
                    "capture_edits": [], "summary": "ok"}

        with agentic.submitting_edit(build) as log:
            author.run_edit(wt_id, "reword step 1")

        user = agentic.content_blocks(log[0])
        prefix, suffix = user[0], user[1]
        self.assertEqual(prefix["cache_control"], {"type": "ephemeral"})
        self.assertIn("=== PAGE ", prefix["text"])
        # The volatile half carries the walkthrough, context and instruction —
        # instruction after the digests, as EDIT_SYSTEM_PROMPT promises.
        # It also carries a cache marker of its own, which the single-shot
        # path did not set: inside a loop the WHOLE initial prompt is a stable
        # prefix re-sent every turn, so the second breakpoint pays for itself
        # from turn two onward (call_claude_agentic marks content[-1]).
        self.assertEqual(suffix["cache_control"], {"type": "ephemeral"})
        self.assertIn("CURRENT walkthrough.json", suffix["text"])
        self.assertIn("EDIT INSTRUCTION from the author:\nreword step 1",
                      suffix["text"])


if __name__ == "__main__":
    unittest.main()
