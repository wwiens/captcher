"""The authoring/edit tool loop.

Both passes hand the model the validator as a tool and end only when a submit
tool validates clean, so the model repairs its own errors inside one run
rather than the author re-paying for a re-run it never learns from. These
tests script the model side at the `_send_request` seam: each scripted turn is
a function(params) -> wire-shaped response dict, so the tests assert both the
requests the loop builds (tools, cache markers, tool_results) and the files it
writes.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from captcher import author
from captcher import store
from conftest import new_member

from tests import fixtures
from tests.test_author_digest import make_capture, modal_page

AGENTIC_ENV = {"ANTHROPIC_API_KEY": "sk-test"}


def tool_use(name, inp, tu_id="tu_1"):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": inp}


def reply(*blocks, stop_reason="tool_use"):
    return {"content": list(blocks), "stop_reason": stop_reason,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def scripted_sender(turns, log):
    """Pop one turn per request; record every request's params."""
    remaining = list(turns)

    def fake_send(params):
        # snapshot: the loop mutates its messages list in place after the
        # request, so a live reference would show the final state everywhere
        log.append(copy.deepcopy(params))
        if not remaining:
            raise AssertionError("model called more times than scripted")
        return remaining.pop(0)(params)
    return fake_send


def prompt_text(params):
    """The initial user prompt text of a loop request."""
    return params["messages"][0]["content"][0]["text"]


def ref_for_save(params):
    """The Save button's ref, parsed from the digests in the prompt."""
    return next(ln.split()[0] for ln in prompt_text(params).splitlines()
                if ln.strip().startswith("e") and '"Save"' in ln)


class P1ContextUpgrades(unittest.TestCase):
    """Display refs are never step targets; the read tool keeps scripts; a
    pointed-at element outside every inventory still yields a find anchor;
    verify_capture_edit shows matches in context."""

    def test_resolve_refs_rejects_a_display_ref_as_step_target(self):
        wt = {"steps": [{"id": "s1", "page": "p.html",
                         "action": {"type": "click", "target": {"ref": "d7"}}}]}
        table = {"d7": {"page": "p.html", "target": None, "node": object(),
                        "display": True}}
        errors = author.resolve_refs(wt, table, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("DISPLAY element", errors[0])
        # the bogus target was not substituted
        self.assertEqual(wt["steps"][0]["action"]["target"], {"ref": "d7"})

    def test_read_tool_keeps_scripts_and_caps_long_ones(self):
        long_body = "var filler=1;" * 800     # ~10K chars
        html = (f"<html><head><script>{long_body}</script></head>"
                f"<body><script>recompute()</script><p>Total</p></body></html>")
        handler = author._read_page_tool_handler([("p.html", html)], {})
        out, _ = handler({"page": "p.html", "query": "recompute"})
        self.assertIn("recompute()", out)
        out2, _ = handler({"page": "p.html", "query": "script truncated"})
        self.assertIn("script truncated", out2)
        # the prompt-inlined variant still strips scripts entirely
        self.assertNotIn("recompute", author.prepare_page(html))

    def test_uninventoried_selection_gets_a_copyable_start_tag(self):
        class FakeNode:
            src = '<span class="a b" data-testid=grand-total>'
        text = author.describe_selection(
            {"selector": "#x", "html": "<span>$10</span>"}, node=FakeNode())
        self.assertIn("data-testid=grand-total", text)
        self.assertIn("safe capture_edits find anchors", text)
        # the DOM serialisation warning stays
        self.assertIn("never from here", text)

    def test_display_selection_names_the_ref_and_forbids_targeting(self):
        text = author.describe_selection(
            {"selector": "#x"}, ref="d3", display=True)
        self.assertIn("d3", text)
        self.assertIn("DISPLAY", text)
        self.assertNotIn('write {"ref"', text)
    def test_verify_capture_edit_shows_match_context(self):
        wt_id = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        store.put_capture(wt_id, "step-000/page.html",
                          "<p>Amount due: <b>$3,712.15</b> as of today</p>")
        out, _ = author._verify_capture_edit_handler(wt_id)({"find": "$3,712.15"})
        self.assertIn("1x in step-000/page.html", out)
        self.assertIn("Amount due:", out)      # context, not just a count
        self.assertIn("as of today", out)


class ObservedEffectsBlock(unittest.TestCase):
    """The recorder's live cause→effect observations, resolved to digest refs
    and placed on the page the action happened on (the PREVIOUS capture).

    The manifest is an object in the capture prefix now — `walkthrough.json`
    beside the pages it describes — rather than a file in a capture folder."""

    def setUp(self):
        self.wt_id, _rels = make_capture(
            new_member(),
            [modal_page("A", extra='<span data-testid="grand-total">$10</span>'),
             modal_page("B")])
        store.put_capture(self.wt_id, store.CAPTURE_MANIFEST, json.dumps({
            "schema": "page-capture-walkthrough@1",
            "initialCapture": {"folder": "step-000"},
            "steps": [{
                "folder": "step-001", "files": {"html": "page.html"},
                "actions": [{
                    "type": "input", "selector": "#fn", "value": "2000",
                    "effects": [{"selector": 'span[data-testid="grand-total"]',
                                 "before": "$10", "after": "$2,000.00"}],
                }],
            }],
        }))
        pages = author.snapshot_pages(self.wt_id)
        _blocks, self.ref_table, self.alias_map, self.trees = \
            author.build_page_digests(pages)

    def test_effects_resolve_to_refs_on_the_pre_action_page(self):
        block = author.observed_effects_block(self.wt_id, self.ref_table,
                                              self.alias_map, self.trees)
        self.assertIn("OBSERVED BEHAVIOUR", block)
        # the action rode on capture step-001, so it HAPPENED on step-000
        self.assertIn("step-000/page.html:", block)
        self.assertIn('"$10" → "$2,000.00"', block)
        # both sides named by ref: the cause is an interactive e-ref, the
        # changed total a display d-ref
        line = next(ln for ln in block.splitlines() if "input on" in ln)
        self.assertRegex(line, r"input on e\d+ → d\d+ changed")

    def test_no_manifest_or_no_effects_yields_no_block(self):
        from captcher import blobs
        blobs.delete(blobs.key_for(self.wt_id, store.CAPTURE_MANIFEST))
        self.assertEqual(author.observed_effects_block(
            self.wt_id, self.ref_table, self.alias_map, self.trees), "")


class FakeNode:
    def __init__(self, src, children=()):
        self.src = src
        self.children = list(children)


class StructuredOps(unittest.TestCase):
    """set_attribute / set_text compile to page-scoped literal replaces,
    verified against the file's exact bytes — refused, never guessed."""

    SPAN = '<span data-testid=total>'
    PAGE = f'<html><body><p>x</p>{SPAN}$10</span></body></html>'

    def setUp(self):
        self.wt_id = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        store.put_capture(self.wt_id, "step-000/page.html", self.PAGE)
        self.table = {"d1": {"page": "step-000/page.html", "target": None,
                             "node": FakeNode(self.SPAN, ["$10"]),
                             "display": True}}

    def test_set_text_compiles_to_a_scoped_literal_op(self):
        ops, errors = author.compile_structured_ops(
            [{"op": "set_text", "ref": "d1", "value": "$25"}],
            self.table, self.wt_id)
        self.assertEqual(errors, [])
        self.assertEqual(ops, [{"find": self.SPAN + "$10",
                                "replace": self.SPAN + "$25",
                                "page": "step-000/page.html"}])
        # and the compiled op actually lands
        changed, warns = author.apply_capture_edits(self.wt_id, ops)
        self.assertEqual(warns, [])
        self.assertIn(self.SPAN + "$25",
                      next(iter(changed.values())))

    def test_set_attribute_add_replace_bare_and_remove(self):
        tag = '<input type=text value="10" required>'
        self.assertEqual(author._rewrite_start_tag(tag, "value", "25", False),
                         '<input type=text value="25" required>')
        self.assertEqual(author._rewrite_start_tag(tag, "maxlength", "6", False),
                         '<input type=text value="10" required maxlength="6">')
        self.assertEqual(author._rewrite_start_tag(tag, "required", None, True),
                         '<input type=text value="10">')
        self.assertEqual(author._rewrite_start_tag(tag, "disabled", "", False),
                         '<input type=text value="10" required disabled>')

    def test_ambiguous_and_absent_anchors_are_errors_not_guesses(self):
        store.put_capture(self.wt_id, "step-000/page.html",
                          f"{self.SPAN}$10</span>{self.SPAN}$10</span>")
        _, errors = author.compile_structured_ops(
            [{"op": "set_text", "ref": "d1", "value": "$25"}],
            self.table, self.wt_id)
        self.assertEqual(len(errors), 1)
        self.assertIn("occurs 2 times", errors[0])
        # a node whose parsed bytes are not in the file (entities)
        table = {"d2": {"page": "step-000/page.html", "target": None,
                        "node": FakeNode("<span data-x=nope>", ["gone"]),
                        "display": True}}
        _, errors = author.compile_structured_ops(
            [{"op": "set_text", "ref": "d2", "value": "x"}], table, self.wt_id)
        self.assertIn("were not found", errors[0])

    def test_set_text_refuses_elements_with_child_markup(self):
        table = {"e1": {"page": "step-000/page.html", "target": {},
                        "node": FakeNode("<div>", ["a", FakeNode("<b>")]),
                        "display": False}}
        _, errors = author.compile_structured_ops(
            [{"op": "set_text", "ref": "e1", "value": "x"}], table, self.wt_id)
        self.assertIn("plain text", errors[0])

    def test_plain_replace_ops_pass_through_untouched(self):
        ops, errors = author.compile_structured_ops(
            [{"find": "$10", "replace": "$25"}], {}, self.wt_id)
        self.assertEqual((ops, errors),
                         ([{"find": "$10", "replace": "$25"}], []))


class PostEditProbeSweep(unittest.TestCase):
    """The mechanical replay sweep after an accepted edit: probes only the
    steps the edit touched, warns on 0 / no-answer / >1."""

    BEFORE = {"steps": [
        {"id": "a", "page": "p1.html",
         "action": {"type": "click", "target": {"selector": "#old"}}},
        {"id": "b", "page": "p2.html",
         "action": {"type": "click", "target": {"selector": "#same"}}},
    ]}
    AFTER = {"steps": [
        {"id": "a", "page": "p1.html",
         "action": {"type": "click", "target": {"selector": "#new"}}},
        {"id": "b", "page": "p2.html",
         "action": {"type": "click", "target": {"selector": "#same"}}},
    ]}

    def test_only_touched_steps_are_probed_and_failures_warn(self):
        from captcher import verify_pass
        seen = {}

        def fake_drive(base_url, cookie, wt_id, jobs, out_dir):
            seen["jobs"] = jobs
            return {"a": {"count": 0, "shot": None}}

        with mock.patch.object(verify_pass, "drive_pages", fake_drive):
            warnings = author._post_edit_probe_warnings(
                ("http://x", None), "wt", self.BEFORE, self.AFTER, set())
        probed = [sid for _page, steps in seen["jobs"] for sid, _s in steps]
        self.assertEqual(probed, ["a"])          # b's target is unchanged
        self.assertEqual(len(warnings), 1)
        self.assertIn("matched NOTHING", warnings[0])

    def test_steps_on_rewritten_pages_are_probed_even_if_untouched(self):
        from captcher import verify_pass
        with mock.patch.object(verify_pass, "drive_pages",
                               lambda *a, **k: {"a": {"count": 1},
                                                "b": {"count": 3}}):
            warnings = author._post_edit_probe_warnings(
                ("http://x", None), "wt", self.BEFORE, self.AFTER,
                {"p2.html"})
        self.assertEqual(len(warnings), 1)       # a matched 1 → silent
        self.assertIn("'b'", warnings[0])
        self.assertIn("matches 3 elements", warnings[0])


class CaptureEditScoping(unittest.TestCase):
    """apply_capture_edits: per-op page scoping and over-edit reporting.

    `changed` is keyed by capture-relative path now, not by filesystem Path —
    the result is what `store.save_with_history(..., changed_pages=…)` takes,
    and nothing is written here. That property is load-bearing and survived the
    move: an edit that fails validation must leave the capture untouched."""

    def setUp(self):
        self.wt_id = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        for rel, body in [("step-000/page.html", "<p>Total $10</p>"),
                          ("step-001/page.html", "<p>Total $10</p>"),
                          ("step-002/page.html", "<p>Done</p>")]:
            store.put_capture(self.wt_id, rel, body)

    def test_scoped_op_touches_only_its_page(self):
        changed, warnings = author.apply_capture_edits(
            self.wt_id, [{"find": "$10", "replace": "$25",
                          "page": "step-000/page.html"}])
        self.assertEqual(sorted(changed), ["step-000/page.html"])
        self.assertEqual(warnings, [])

    def test_nothing_is_written_until_the_save(self):
        author.apply_capture_edits(
            self.wt_id, [{"find": "$10", "replace": "$25",
                          "page": "step-000/page.html"}])
        self.assertEqual(store.capture_text(self.wt_id, "step-000/page.html"),
                         "<p>Total $10</p>")

    def test_scope_follows_byte_identical_duplicates_both_directions(self):
        # digests present duplicates as interchangeable page values, so a
        # scoped op must edit the whole identity group, whichever name it uses
        amap = {"step-001/page.html": "step-000/page.html"}
        changed, _ = author.apply_capture_edits(
            self.wt_id, [{"find": "$10", "replace": "$25",
                          "page": "step-001/page.html"}], alias_map=amap)
        self.assertEqual(sorted(changed),
                         ["step-000/page.html", "step-001/page.html"])

    def test_unscoped_multi_file_hit_is_reported_with_counts(self):
        changed, warnings = author.apply_capture_edits(
            self.wt_id, [{"find": "$10", "replace": "$25"}])
        self.assertEqual(len(changed), 2)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 files", warnings[0])
        self.assertIn("1x in step-000/page.html", warnings[0])
        self.assertIn('scope it with "page"', warnings[0])

    def test_single_file_hit_stays_silent(self):
        _, warnings = author.apply_capture_edits(
            self.wt_id, [{"find": "Done", "replace": "Complete"}])
        self.assertEqual(warnings, [])

    def test_scoped_zero_hit_and_bad_page_each_warn(self):
        _, warnings = author.apply_capture_edits(
            self.wt_id, [{"find": "Missing", "replace": "x",
                          "page": "step-000/page.html"},
                         {"find": "$10", "replace": "x",
                          "page": "step-999/page.html"}])
        self.assertEqual(len(warnings), 2)
        self.assertIn("scoped to step-000/page.html", warnings[0])
        self.assertIn("not a captured snapshot", warnings[1])


def good_walkthrough(walkthrough_id, ref):
    wt = fixtures.walkthrough_def(walkthrough_id, steps=1)
    wt["steps"][0]["page"] = "step-000/page.html"
    wt["steps"][0]["action"] = {"type": "click", "target": {"ref": ref}}
    return wt


class AgenticRunPass(unittest.TestCase):
    def setUp(self):
        self.wt_id, self.rels = make_capture(new_member(), [modal_page("A")])

    def good(self, ref):
        return good_walkthrough(self.wt_id, ref)

    def _run(self, turns):
        log = []
        with mock.patch.dict(os.environ, AGENTIC_ENV), \
                mock.patch.object(author, "_send_request",
                                  scripted_sender(turns, log)):
            result = author.run_pass(self.wt_id)
        return result, log

    def test_submit_error_is_repaired_in_loop(self):
        """A bad ref no longer aborts the pass — the submit tool reports it
        and the corrected resubmission lands."""
        turns = [
            lambda p: reply(tool_use("submit_walkthrough",
                                     {"walkthrough": self.good("e999")})),
            lambda p: reply(tool_use("submit_walkthrough",
                                     {"walkthrough": self.good(ref_for_save(p))})),
        ]
        result, log = self._run(turns)

        # the second request carried the validator's error back to the model
        second = log[1]["messages"]
        tool_result = second[-1]["content"][-1]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertIn("e999", tool_result["content"])
        self.assertIn('"accepted": false', tool_result["content"].lower())

        # the stored walkthrough carries the substituted verified selector
        written = store.get(self.wt_id)["definition"]
        target = written["steps"][0]["action"]["target"]
        self.assertNotIn("ref", target)
        self.assertTrue(target.get("selector"))
        self.assertEqual(result["steps_after"], 1)

    def test_clean_submit_with_warnings_is_reviewed_once(self):
        """A submission with no errors but with warnings used to be accepted
        silently. It is now handed back once with the warnings, and the next
        submission is accepted even if it still carries them."""
        def warned(p):
            wt = self.good(ref_for_save(p))
            wt["steps"][0]["hint"] = ""
            return wt

        seen = {}

        def first(p):
            return reply(tool_use("submit_walkthrough", {"walkthrough": warned(p)}))

        def second(p):
            seen["review"] = json.loads(p["messages"][-1]["content"][-1]["content"])
            return reply(tool_use("submit_walkthrough", {"walkthrough": warned(p)},
                                  "tu_2"))

        result, log = self._run([first, second])
        self.assertEqual(len(log), 2)
        self.assertFalse(seen["review"]["accepted"])
        self.assertEqual(seen["review"]["errors"], [])
        self.assertTrue(any("hint is empty" in w for w in seen["review"]["warnings"]))
        self.assertEqual(seen["review"]["note"], author.WARNINGS_REVIEW_NOTE)
        # accepted on the second submit, warning and all
        self.assertTrue(any("hint is empty" in w for w in result["warnings"]))

    def test_truncated_turn_salvages_an_earlier_clean_validation(self):
        """A max_tokens turn used to abort the pass and discard everything,
        including a walkthrough an earlier turn had already validated clean."""
        def first(p):
            return reply(tool_use("validate_walkthrough",
                                  {"walkthrough": self.good(ref_for_save(p))}))

        def second(p):
            return reply(stop_reason="max_tokens")

        result, _ = self._run([first, second])
        self.assertEqual(result["steps_after"], 1)
        self.assertTrue(store.get(self.wt_id)["definition"]["steps"])

    def test_truncated_turn_with_nothing_to_salvage_keeps_its_message(self):
        with self.assertRaises(author.AuthorError) as cm:
            self._run([lambda p: reply(stop_reason="max_tokens")])
        self.assertIn("max_tokens", str(cm.exception))
        self.assertIn("EFFORT", str(cm.exception))

    def test_validate_tool_reports_without_finishing(self):
        seen = {}

        def first(p):
            return reply(tool_use("validate_walkthrough",
                                  {"walkthrough": self.good("e999")}))

        def second(p):
            seen["validate_result"] = p["messages"][-1]["content"][-1]["content"]
            return reply(tool_use("submit_walkthrough",
                                  {"walkthrough": self.good(ref_for_save(p))}))

        result, _ = self._run([first, second])
        report = json.loads(seen["validate_result"])
        self.assertTrue(any("e999" in e for e in report["errors"]))
        self.assertEqual(result["steps_after"], 1)

    def test_read_page_html_returns_match_windows(self):
        seen = {}

        def first(p):
            return reply(tool_use("read_page_html",
                                  {"page": "step-000/page.html", "query": "Save"}))

        def second(p):
            seen["read_result"] = p["messages"][-1]["content"][-1]["content"]
            return reply(tool_use("submit_walkthrough",
                                  {"walkthrough": self.good(ref_for_save(p))}))

        self._run([first, second])
        self.assertIn("match window", seen["read_result"])
        self.assertIn("Save", seen["read_result"])

    def test_end_turn_without_submit_gets_one_nudge_then_fails(self):
        turns = [
            lambda p: reply({"type": "text", "text": "Here is the JSON: {}"},
                            stop_reason="end_turn"),
            lambda p: reply(tool_use("submit_walkthrough",
                                     {"walkthrough": self.good(ref_for_save(p))})),
        ]
        result, log = self._run(turns)
        nudge = log[1]["messages"][-1]["content"][0]["text"]
        self.assertIn("call the submit", nudge)
        self.assertEqual(result["steps_after"], 1)

        # …but a second text-only turn is the end of the road
        turns = [
            lambda p: reply({"type": "text", "text": "chat"}, stop_reason="end_turn"),
            lambda p: reply({"type": "text", "text": "more chat"}, stop_reason="end_turn"),
        ]
        # a fresh draft: the successful run above authored the first one, and
        # run_pass refuses a walkthrough that already has an `authored_at`
        self.setUp()
        with self.assertRaises(author.AuthorError) as cm:
            self._run(turns)
        self.assertIn("without submitting", str(cm.exception))

    def test_loop_exhaustion_with_nothing_valid_raises_and_says_why(self):
        """`e999` never resolves, so no candidate ever validates and there is
        nothing to salvage. The message carries the actual errors rather than
        just "the loop did not finish"."""
        def validate_forever(p):
            return reply(tool_use("validate_walkthrough",
                                  {"walkthrough": self.good("e999")}))

        with self.assertRaises(author.LoopExhausted) as cm:
            self._run([validate_forever] * author.MAX_LOOP_ITERATIONS)
        self.assertIn("nothing was written", str(cm.exception))
        self.assertIn("e999", str(cm.exception))
        self.assertTrue(any("e999" in e for e in cm.exception.errors))

    def test_a_candidate_that_validated_clean_is_salvaged_when_turns_run_out(self):
        """Eight turns of Opus is the most expensive thing this server does.
        A model that validates clean and then spends its remaining turns
        polishing instead of submitting used to lose the whole run."""
        def validate_good(p):
            return reply(tool_use("validate_walkthrough",
                                  {"walkthrough": self.good(ref_for_save(p))}))

        result, _ = self._run([validate_good] * author.MAX_LOOP_ITERATIONS)
        self.assertEqual(result["steps_after"], 1)
        written = store.get(self.wt_id)["definition"]
        self.assertNotIn("TODO", json.dumps(written))
        # and the salvaged copy went through the same resolution a submit runs
        self.assertNotIn("ref", written["steps"][0]["action"]["target"])

    def test_refusal_mid_loop_raises(self):
        def refusal(p):
            return {"content": [], "stop_reason": "refusal",
                    "stop_details": {"category": "cyber"},
                    "usage": {"input_tokens": 1, "output_tokens": 0}}

        with self.assertRaises(author.AuthorError) as cm:
            self._run([refusal])
        self.assertIn("declined", str(cm.exception))
        self.assertIn("cyber", str(cm.exception))

    def test_loop_requests_carry_tools_and_cache_markers(self):
        def first(p):
            return reply(tool_use("submit_walkthrough",
                                  {"walkthrough": self.good(ref_for_save(p))}))

        _, log = self._run([first])
        body = log[0]
        names = [t["name"] for t in body["tools"]]
        self.assertEqual(names, ["validate_walkthrough", "read_page_html",
                                 "view_screenshot", "submit_walkthrough"])
        # stable prefix cached: system block and the initial user prompt
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][0]["content"][0]["cache_control"],
                         {"type": "ephemeral"})
        # the closer tells the model to submit through a tool — there is no
        # longer a second, bare-JSON closer it could have been swapped for
        self.assertIn(author.PASS_CLOSER, body["system"][0]["text"])
        self.assertIn("submit_walkthrough", body["system"][0]["text"])


class AgenticRunEdit(unittest.TestCase):
    def setUp(self):
        self.wt_id = fixtures.make_walkthrough(new_member(), steps=2)["id"]
        # run_edit refuses a draft, and that is the `authored_at` column's
        # question now rather than a substring scan for "TODO".
        store.mark_authored(self.wt_id)

    def edited(self):
        return fixtures.walkthrough_def(self.wt_id, steps=2)

    def _run(self, turns, instruction="rename the member", **kwargs):
        log = []
        with mock.patch.dict(os.environ, AGENTIC_ENV), \
                mock.patch.object(author, "_send_request",
                                  scripted_sender(turns, log)):
            result = author.run_edit(self.wt_id, instruction, **kwargs)
        return result, log

    def test_probe_tool_exists_only_with_a_replay_ctx(self):
        def submit(p):
            wt = self.edited()
            return reply(tool_use("submit_edit",
                                  {"walkthrough": wt, "summary": "ok"}))

        _result, log = self._run(turns=[submit])
        self.assertNotIn("probe_target", [t["name"] for t in log[0]["tools"]])

    def test_probe_tool_probes_and_the_post_save_sweep_runs(self):
        from captcher import verify_pass
        shot_dir = tempfile.TemporaryDirectory()
        self.addCleanup(shot_dir.cleanup)
        shot = Path(shot_dir.name) / "probe.png"
        # 1x1 transparent png
        shot.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049"
            "454e44ae426082"))
        probed, seen = {}, {}

        def fake_probe(base_url, cookie, wt_id, page_rel, target, out_dir):
            probed.update(page=page_rel, target=target)
            return {"count": 1, "shot": str(shot)}

        def first(p):
            return reply(tool_use("probe_target",
                                  {"page": "step-000/page.html",
                                   "ref": ref_for_save(p)}))

        def second(p):
            seen["probe_result"] = p["messages"][-1]["content"][-1]["content"]
            wt = self.edited()
            return reply(tool_use("submit_edit",
                                  {"walkthrough": wt, "summary": "ok"}))

        with mock.patch.object(verify_pass, "probe_one", fake_probe), \
                mock.patch.object(verify_pass, "drive_pages",
                                  lambda *a, **k: {}) as _sweep:
            result, log = self._run(turns=[first, second],
                                    replay_ctx=("http://127.0.0.1:1", None))
        self.assertIn("probe_target", [t["name"] for t in log[0]["tools"]])
        # the handler resolved the ref to its verified selector and probed it
        self.assertEqual(probed["page"], "step-000/page.html")
        self.assertTrue(probed["target"].get("selector"))
        # the model saw count + ringed screenshot
        blocks = seen["probe_result"]
        self.assertIn("matches 1 element(s)", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(result["summary"], "ok")

    def test_a_display_ref_probe_is_refused_with_guidance(self):
        seen = {}

        def first(p):
            return reply(tool_use("probe_target",
                                  {"page": "step-000/page.html", "ref": "d999"}))

        def second(p):
            seen["result"] = p["messages"][-1]["content"][-1]["content"]
            wt = self.edited()
            return reply(tool_use("submit_edit",
                                  {"walkthrough": wt, "summary": "ok"}))

        self._run(turns=[first, second],
                  replay_ctx=("http://127.0.0.1:1", None))
        self.assertIn("not in the page digests", seen["result"])

    def test_verify_then_submit_edit_applies_capture_edits(self):
        seen = {}

        def first(p):
            return reply(tool_use("verify_capture_edit",
                                  {"find": fixtures.PAGE_MARKER}))

        def second(p):
            seen["verify_result"] = p["messages"][-1]["content"][-1]["content"]
            wt = self.edited()
            return reply(tool_use("submit_edit", {
                "walkthrough": wt,
                "capture_edits": [{"find": fixtures.PAGE_MARKER,
                                   "replace": "Renamed capture page"}],
                "summary": "Renamed the marker.",
            }))

        def third(p):
            # the unscoped find hit two pages, which warns; the review round
            # hands that back once and an unchanged resubmit is accepted
            seen["review"] = p["messages"][-1]["content"][-1]["content"]
            submitted = next(b for b in p["messages"][-2]["content"]
                             if b.get("type") == "tool_use")
            return reply(tool_use("submit_edit", submitted["input"], "tu_3"))

        result, log = self._run(turns=[first, second, third])
        self.assertIn("applied across 2 files", seen["review"])
        # the verify tool reported real per-file counts before the submit
        self.assertIn("occurs", seen["verify_result"])
        self.assertIn("step-000/page.html", seen["verify_result"])
        # the edit landed: pages rewritten, summary passed through
        page = store.capture_text(self.wt_id, "step-000/page.html")
        self.assertIn("Renamed capture page", page)
        self.assertNotIn(fixtures.PAGE_MARKER, page)
        self.assertEqual(result["summary"], "Renamed the marker.")
        self.assertEqual(result["pages_changed"], 2)
        # the edit toolset includes the capture-edit verifier
        names = [t["name"] for t in log[0]["tools"]]
        self.assertIn("verify_capture_edit", names)
        self.assertIn("submit_edit", names)

    def test_submit_edit_error_reports_instead_of_aborting(self):
        def first(p):
            wt = self.edited()
            wt["steps"][0]["action"] = {"type": "click", "target": {"ref": "e999"}}
            return reply(tool_use("submit_edit", {"walkthrough": wt}))

        def second(p):
            wt = self.edited()
            return reply(tool_use("submit_edit",
                                  {"walkthrough": wt, "summary": "Fixed."}))

        result, log = self._run([first, second])
        errors_back = log[1]["messages"][-1]["content"][-1]["content"]
        self.assertIn("e999", errors_back)
        self.assertEqual(result["summary"], "Fixed.")


if __name__ == "__main__":
    unittest.main()
