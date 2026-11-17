"""The AI edit endpoint (/ai-edit), its undo (/restore), and the current-step
context passed into author.run_edit.

author.run_edit is stubbed for the endpoint tests; the tests that run the real
thing script the model at author._send_request via tests/agentic.py, so no
network and no API key.
"""
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from captcher import author
from captcher import jobs
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import agentic, fixtures


class AiEditEndpointTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.intruder_id = new_member()
        self.row = fixtures.make_walkthrough(self.owner_id, steps=2)
        self.wt_id = self.row["id"]
        store.mark_authored(self.wt_id)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

        self.owner = signed_in_client(self.base, self.owner_id)
        self.intruder = signed_in_client(self.base, self.intruder_id)
        self._orig_run_edit = author.run_edit

    def tearDown(self):
        author.run_edit = self._orig_run_edit
        held = jobs.active_for(self.wt_id)
        if held is not None:
            held.finish()
            jobs.release(held)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def post_json(self, client, path, obj):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        return client.open(req)

    # ---- /ai-edit -----------------------------------------------------------

    def stub_run_edit(self, calls, error=None):
        """Replace author.run_edit with a fake that records its arguments and,
        on success, rewrites the walkthrough the way the real one would."""
        def fake(walkthrough_id, instruction, model=None, current_step=None,
                 selection=None, progress=None, replay_ctx=None):
            calls.append({"walkthrough_id": walkthrough_id, "instruction": instruction,
                          "current_step": current_step, "selection": selection,
                          "replay_ctx": replay_ctx})
            if error:
                raise error
            edited = dict(store.get(walkthrough_id)["definition"])
            edited["title"] = "Edited by AI"
            stamp = store.save_with_history(walkthrough_id, edited)
            return {"walkthrough_id": walkthrough_id, "title": edited["title"],
                    "steps_before": 2, "steps_after": 2, "pages_changed": 0,
                    "warnings": ["something minor"], "stamp": stamp,
                    "summary": "Renamed the title."}
        author.run_edit = fake

    def test_ai_edit_applies_and_returns_the_fresh_walkthrough(self):
        calls = []
        self.stub_run_edit(calls)
        r = self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "Rename the title", "current_step": 1})
        d = json.loads(r.read())
        self.assertTrue(d["ok"])
        # the replay ctx is the server's own address plus the caller's
        # session cookie — what the probe tool and post-save sweep drive
        replay_ctx = calls[0].pop("replay_ctx")
        self.assertTrue(replay_ctx[0].startswith("http://127.0.0.1:"))
        self.assertIn("session=", replay_ctx[1])
        self.assertEqual(calls, [{"walkthrough_id": self.wt_id,
                                  "instruction": "Rename the title",
                                  "current_step": 1, "selection": None}])
        self.assertEqual(d["walkthrough"]["title"], "Edited by AI")
        self.assertNotIn("owner", d["walkthrough"])
        self.assertEqual(d["steps_before"], 2)
        self.assertEqual(d["steps_after"], 2)
        self.assertEqual(d["warnings"], ["something minor"])
        self.assertEqual(d["summary"], "Renamed the title.")
        self.assertRegex(d["stamp"], r"^\d{8}-\d{6}$")

    def test_an_answer_comes_back_as_an_answer_with_nothing_to_undo(self):
        def fake(walkthrough_id, instruction, **_kw):
            return {"walkthrough_id": walkthrough_id, "kind": "answer",
                    "answer": "No.", "suggested_change": "Add the options",
                    "steps_before": 2, "steps_after": 2, "pages_changed": 0,
                    "pages_changed_paths": [], "warnings": [], "summary": "",
                    "stamp": None}
        author.run_edit = fake
        before = store.get(self.wt_id)["definition"]
        r = self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "do we have the options?"})
        d = json.loads(r.read())
        self.assertEqual(d["kind"], "answer")
        self.assertEqual(d["answer"], "No.")
        self.assertEqual(d["suggested_change"], "Add the options")
        self.assertIsNone(d["stamp"])
        self.assertEqual(store.get(self.wt_id)["definition"], before)

    def test_an_edit_says_so_and_names_the_pages_it_rewrote(self):
        calls = []
        self.stub_run_edit(calls)
        r = self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "Rename the title"})
        d = json.loads(r.read())
        self.assertEqual(d["kind"], "edit")
        self.assertEqual(d["pages_changed_paths"], [])

    def test_the_editor_can_run_an_edit_in_the_background(self):
        """The editor's shape: 202 + a job id it follows over SSE, with the
        same result the synchronous path returns waiting at the end."""
        calls = []
        self.stub_run_edit(calls)
        r = self.post_json(self.owner,
                           f"/api/walkthroughs/{self.wt_id}/ai-edit?background=1",
                           {"instruction": "Rename the title"})
        self.assertEqual(r.status, 202)
        job_id = json.loads(r.read())["job_id"]
        for _ in range(100):
            snap = json.loads(self.owner.open(f"{self.base}/api/jobs/{job_id}").read())
            if snap["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["result"]["kind"], "edit")
        self.assertEqual(snap["result"]["walkthrough"]["title"], "Edited by AI")

    def test_ai_edit_ignores_a_non_int_current_step(self):
        calls = []
        self.stub_run_edit(calls)
        self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                       {"instruction": "x", "current_step": True})
        self.assertEqual(calls[0]["current_step"], None)

    # ---- the pointed-at element ---------------------------------------------

    def test_ai_edit_passes_the_selection_through(self):
        calls = []
        self.stub_run_edit(calls)
        self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                       {"instruction": "make this required",
                        "selection": {"page": "pages/1.html", "selector": "#email",
                                      "text": "Email", "tag": "input",
                                      "matches": 1, "html": "<input id=email>"}})
        self.assertEqual(calls[0]["selection"],
                         {"page": "pages/1.html", "selector": "#email",
                          "text": "Email", "tag": "input",
                          "matches": 1, "html": "<input id=email>"})

    def test_ai_edit_drops_a_selection_with_no_selector(self):
        """There is nothing to point at without one, and a page name alone
        would still read to the model as a confident pick."""
        calls = []
        self.stub_run_edit(calls)
        for bad in ({"page": "pages/1.html"}, {"selector": "   "},
                    {"selector": "#a", "text": 42}, "not-a-dict", None):
            self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "x", "selection": bad})
            self.assertIsNone(calls[-1]["selection"], bad)

    def test_ai_edit_caps_the_selection_strings(self):
        """These land in a model prompt, and the editor is not the only thing
        that can POST here."""
        calls = []
        self.stub_run_edit(calls)
        self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                       {"instruction": "x",
                        "selection": {"selector": "a" * 5000, "html": "b" * 90000,
                                      "matches": -1}})
        sel = calls[0]["selection"]
        self.assertEqual(len(sel["selector"]), server.SELECTION_FIELDS["selector"])
        self.assertEqual(len(sel["html"]), server.SELECTION_FIELDS["html"])
        self.assertNotIn("matches", sel)   # out of range, so simply not reported

    def test_ai_edit_rejects_a_missing_instruction(self):
        calls = []
        self.stub_run_edit(calls)
        for body in ({}, {"instruction": "   "}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit", body)
            self.assertEqual(raised.exception.code, 400)
        self.assertEqual(calls, [])

    def test_ai_edit_rejects_a_non_object_body(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit", ["not", "a", "dict"])
        self.assertEqual(raised.exception.code, 400)

    def test_ai_edit_hides_other_accounts_walkthroughs(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json(self.intruder, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "steal"})
        self.assertEqual(raised.exception.code, 404)

    def test_ai_edit_conflicts_while_a_pass_is_running(self):
        held = jobs.claim(self.wt_id, "author", owner=self.owner_id)
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                               {"instruction": "x"})
            self.assertEqual(raised.exception.code, 409)
            # the 409 names the holder, so the UI can say what to wait for
            body = json.loads(raised.exception.read())
            self.assertEqual(body["kind"], "author")
            self.assertEqual(body["job_id"], held.id)
        finally:
            held.finish()
            jobs.release(held)

    def test_ai_edit_maps_author_errors_to_422_and_releases_the_lock(self):
        calls = []
        self.stub_run_edit(calls, error=author.AuthorError("nope"))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/ai-edit",
                           {"instruction": "x"})
        self.assertEqual(raised.exception.code, 422)
        self.assertEqual(json.loads(raised.exception.read())["error"], "nope")
        # the claim is released even though the pass failed, so a retry is
        # not locked out by the attempt that just failed
        self.assertIsNone(jobs.active_for(self.wt_id))

    # ---- /restore -----------------------------------------------------------

    PAGE = "step-000/page.html"

    def seed_history(self):
        """Simulate an AI edit: change the definition and one capture page
        through store.save_with_history, the way run_edit persists. Returns the
        stamp plus the originals.

        `changed_pages` is keyed by capture-relative path now — the same string
        the definition names — where the folder version took a resolved Path
        and had to convert."""
        original = store.get(self.wt_id)["definition"]
        original_page_text = store.capture_text(self.wt_id, self.PAGE)

        edited = dict(original, title="Edited by AI")
        stamp = store.save_with_history(
            self.wt_id, edited,
            changed_pages={self.PAGE: "<html><body>EDITED</body></html>"})
        return stamp, original, original_page_text

    def test_restore_puts_walkthrough_and_pages_back(self):
        stamp, original, original_page_text = self.seed_history()
        self.assertEqual(store.capture_text(self.wt_id, self.PAGE),
                         "<html><body>EDITED</body></html>")
        # The history stamp is YYYYmmdd-HHMMSS and the unique constraint on
        # (walkthrough_id, stamp) is ON CONFLICT DO NOTHING, so a second save
        # inside the same wall-clock second keeps the first entry and silently
        # drops the second. Real edits are minutes apart; this test is not, and
        # without the wait it asserts the collision rather than the restore.
        time.sleep(max(0.0, 1.01 - (time.time() % 1)))

        r = self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/restore",
                           {"stamp": stamp})
        d = json.loads(r.read())
        self.assertTrue(d["ok"])
        self.assertEqual(d["restored_from"], stamp)
        self.assertEqual(d["pages_restored"], 1)
        self.assertNotIn("owner", d["walkthrough"])
        self.assertEqual(d["walkthrough"]["title"], fixtures.TITLE)

        self.assertEqual(store.get(self.wt_id)["definition"]["title"],
                         original["title"])
        self.assertEqual(store.capture_text(self.wt_id, self.PAGE),
                         original_page_text)
        # the restore is itself undoable: its history row holds the AI edit
        undone = store.history_entry(self.wt_id, d["stamp"])
        self.assertEqual(undone["definition"]["title"], "Edited by AI")

    def test_restore_rejects_traversal_shaped_stamps(self):
        self.seed_history()
        for stamp in ("../../../../etc/passwd", "20260101-000000/../x", "", "junk"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/restore",
                               {"stamp": stamp})
            self.assertEqual(raised.exception.code, 400, stamp)

    def test_restore_404s_an_unknown_stamp(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/restore",
                           {"stamp": "20990101-000000"})
        self.assertEqual(raised.exception.code, 404)

    def test_restore_hides_other_accounts_walkthroughs(self):
        stamp, *_ = self.seed_history()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post_json(self.intruder, f"/api/walkthroughs/{self.wt_id}/restore",
                           {"stamp": stamp})
        self.assertEqual(raised.exception.code, 404)

    def test_restore_conflicts_while_a_pass_is_running(self):
        stamp, *_ = self.seed_history()
        held = jobs.claim(self.wt_id, "author", owner=self.owner_id)
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.post_json(self.owner, f"/api/walkthroughs/{self.wt_id}/restore",
                               {"stamp": stamp})
            self.assertEqual(raised.exception.code, 409)
        finally:
            held.finish()
            jobs.release(held)


class RunEditCurrentStepTests(unittest.TestCase):
    """The real run_edit against the fixture walkthrough, model scripted."""

    def setUp(self):
        self.wt_id = fixtures.make_walkthrough(new_member(), steps=2)["id"]
        store.mark_authored(self.wt_id)

    def edited(self, title=None, summary="Shortened the hint."):
        """The submit_edit payload a well-behaved model would send."""
        wt = fixtures.walkthrough_def(self.wt_id, steps=2)
        if title:
            wt["title"] = title
        return {"walkthrough": wt, "capture_edits": [], "summary": summary}

    def test_current_step_lands_in_the_prompt_and_stamp_in_the_result(self):
        payload = self.edited(title="Edited by AI")

        with agentic.submitting_edit(lambda _p: payload) as log:
            result = author.run_edit(self.wt_id, "Shorten this step's hint", current_step=1)

        prompt = agentic.prompt_text(log[0])
        self.assertIn("step 2 of 2", prompt)
        self.assertIn("Step 1 title", prompt)
        self.assertEqual(result["summary"], "Shortened the hint.")
        self.assertRegex(result["stamp"], r"^\d{8}-\d{6}$")
        self.assertEqual(store.get(self.wt_id)["definition"]["title"],
                         "Edited by AI")

    def test_an_out_of_range_step_adds_no_context(self):
        payload = self.edited(title="Edited by AI")

        with agentic.submitting_edit(lambda _p: payload) as log:
            author.run_edit(self.wt_id, "x", current_step=99)
        self.assertNotIn("currently viewing", agentic.prompt_text(log[0]))

    def _prompt_for(self, **kwargs):
        payload = self.edited(title="Edited by AI")

        with agentic.submitting_edit(lambda _p: payload) as log:
            author.run_edit(self.wt_id, "make this required", **kwargs)
        return agentic.prompt_text(log[0])

    def test_the_pointed_at_element_lands_in_the_prompt(self):
        prompt = self._prompt_for(selection={
            "page": "pages/2.html", "selector": "form.signup #email",
            "text": "Email address", "tag": "input", "matches": 1,
            "html": '<input id="email" name="email">'})
        self.assertIn("POINTED AT", prompt)
        self.assertIn("form.signup #email", prompt)
        self.assertIn("Email address", prompt)
        self.assertIn("pages/2.html", prompt)
        self.assertIn('<input id="email" name="email">', prompt)
        self.assertIn("verified to match exactly one element", prompt)

    def test_the_markup_excerpt_is_flagged_as_unusable_for_find_replace(self):
        """outerHTML is the DOM's serialisation, not the bytes on disk, so it
        must never be copied into a literal capture_edits `find`."""
        prompt = self._prompt_for(selection={"selector": "#email",
                                             "html": "<input id=email>"})
        self.assertIn("never from here", prompt)

    def test_an_ambiguous_selection_is_flagged_not_silently_trusted(self):
        prompt = self._prompt_for(selection={"selector": "input", "matches": 7})
        self.assertIn("matched 7 elements", prompt)
        self.assertNotIn("verified to match exactly one element", prompt)

    def test_no_selection_adds_no_block(self):
        self.assertNotIn("POINTED AT", self._prompt_for())

    def test_a_question_is_answered_and_nothing_is_saved(self):
        before = store.get(self.wt_id)["definition"]
        stamps = [h["stamp"] for h in store.history_entries(self.wt_id)]
        turns = [lambda p: agentic.reply(agentic.tool_use(
            "answer_author", {"answer": "No, the options are not in the capture.",
                              "suggested_change": "Add the options"}))]
        with agentic.scripted(turns) as log:
            result = author.run_edit(self.wt_id, "do we have the options?")
        self.assertIn("answer_author", agentic.tool_names(log[0]))
        self.assertEqual(result["kind"], "answer")
        self.assertEqual(result["answer"], "No, the options are not in the capture.")
        self.assertEqual(result["suggested_change"], "Add the options")
        self.assertIsNone(result["stamp"])
        self.assertEqual(store.get(self.wt_id)["definition"], before)
        self.assertEqual([h["stamp"] for h in store.history_entries(self.wt_id)], stamps)

    def test_an_edit_is_labelled_as_one(self):
        with agentic.submitting_edit(lambda _p: self.edited(title="Edited by AI")):
            result = author.run_edit(self.wt_id, "rename it")
        self.assertEqual(result["kind"], "edit")

    def test_the_answer_exit_can_be_withheld(self):
        with agentic.submitting_edit(lambda _p: self.edited(title="Edited by AI")) as log:
            author.run_edit(self.wt_id, "rename it", answerable=False)
        self.assertNotIn("answer_author", agentic.tool_names(log[0]))


class PlayerSplitTests(unittest.TestCase):
    """The popup player is playback-only: none of the AI-edit surface may leak
    into it (tests/test_player2.py pins the rest of the fork)."""

    def test_player2_has_no_ai_edit_surface(self):
        static = Path(__file__).resolve().parents[1] / "captcher" / "static"
        for name in ("player2.html", "player2.js"):
            text = (static / name).read_text(encoding="utf-8")
            self.assertNotIn("ai-edit", text, name)
            self.assertNotIn("/restore", text, name)
            self.assertNotIn("ai-pick", text, name)


class ElementPickerTests(unittest.TestCase):
    """The picker spans two files: bridge.js draws it inside the captured page
    and answers with `picked`; player.js is its only consumer. Neither half
    does anything alone."""

    static = Path(__file__).resolve().parents[1] / "captcher" / "static"

    def _read(self, name):
        return (self.static / name).read_text(encoding="utf-8")

    def test_the_editor_offers_the_control_and_reads_the_answer(self):
        self.assertIn('id="ai-pick"', self._read("player.html"))
        player = self._read("player.js")
        self.assertIn('cmd: "pick"', player)
        self.assertIn('m.type === "picked"', player)

    def test_the_bridge_reports_enough_to_identify_the_element(self):
        """A bare selector is not enough for the model to be sure which node
        it is looking at in a page it only sees as text."""
        bridge = self._read("bridge.js")
        for field in ("tag:", "html: markupExcerpt(s.el)"):
            self.assertIn(field, bridge)

    def test_every_id_the_player_reaches_for_exists_in_its_markup(self):
        """$("ai-sel-warn") against an id that is not there returns null and
        throws on the next property access — a whole panel silently dead, and
        nothing above this line would notice. Cheap to check, so checked."""
        html = self._read("player.html")
        js = self._read("player.js")
        ids = set(re.findall(r'id="([^"]+)"', html))
        refs = set(re.findall(r'\$\("([^"]+)"\)', js))
        self.assertEqual(sorted(refs - ids), [])

    def test_every_inline_handler_in_the_markup_is_defined(self):
        html = self._read("player.html")
        js = self._read("player.js")
        handlers = set(re.findall(r'on\w+="(\w+)\(', html))
        missing = [h for h in handlers
                   if not re.search(rf"function {re.escape(h)}\b", js)]
        self.assertEqual(sorted(missing), [])

    def test_the_excerpt_carries_no_class_the_bridge_invented(self):
        """br-pick-hover sits on the element at the instant it is clicked, and
        no br-* class exists in the snapshot on disk — describing one to the
        model would describe an element that is not there."""
        bridge = self._read("bridge.js")
        excerpt = bridge[bridge.index("function markupExcerpt"):]
        excerpt = excerpt[:excerpt.index("function cssFor")]
        self.assertIn('indexOf("br-") !== 0', excerpt)


if __name__ == "__main__":
    unittest.main()
