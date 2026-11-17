"""Render-accuracy checking (render_check.py + its endpoints).

The two expensive seams — the Playwright renderer (render_check.render_pages)
and the vision verdict (author.call_claude_vision) — are stubbed throughout:
no browser, no network, no API key. What is under test is the orchestration:
which pages get rendered, how verdicts and failures land in the sidecar, and
how the endpoints guard and expose it.
"""
import json
import threading
import unittest
import urllib.request
import uuid
from unittest import mock

from captcher import author
from captcher import render_check
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)


def _verdict(matches=True, severity="ok", differences=()):
    return json.dumps({"matches": matches, "severity": severity,
                       "differences": list(differences)})


class RunCheckTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner_id, steps=2)["id"]

    def _screenshot(self, folder):
        store.put_capture(self.wt_id, f"{folder}/screenshot.png", PNG)

    def _fake_renderer(self, calls, skip=()):
        """Stands in for Playwright. Writes its shots the way the real renderer
        does now — `store.put_capture` at a capture-relative path, not a file
        at a filesystem Path."""
        def fake(base_url, cookie_header, walkthrough_id, jobs):
            calls.append({"base_url": base_url, "cookie": cookie_header,
                          "walkthrough_id": walkthrough_id, "jobs": list(jobs)})
            done = []
            for rel, _viewport, out_rel in jobs:
                if rel in skip:
                    continue
                store.put_capture(walkthrough_id, out_rel, PNG)
                done.append(rel)
            return done
        return fake

    def test_verdicts_land_in_the_sidecar(self):
        self._screenshot("step-000")
        self._screenshot("step-001")
        calls = []
        with mock.patch.object(render_check, "render_pages", self._fake_renderer(calls)), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict(False, "broken",
                                                        ["left panel is blank"])) \
                as vision:
            results = render_check.run_check(self.wt_id, "http://127.0.0.1:1",
                                             cookie_header="s=1", model="claude-test")

        # the verdict call is schema-constrained and cheap: low effort, small
        # budget, structured output
        kwargs = vision.call_args.kwargs
        self.assertIs(kwargs["output_schema"], render_check.VERDICT_SCHEMA)
        self.assertEqual(kwargs["effort"], render_check.VISION_EFFORT)
        self.assertEqual(kwargs["max_tokens"], render_check.VISION_MAX_TOKENS)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cookie"], "s=1")
        self.assertEqual([j[0] for j in calls[0]["jobs"]],
                         ["step-000/page.html", "step-001/page.html"])
        for rel in ("step-000/page.html", "step-001/page.html"):
            entry = results["pages"][rel]
            self.assertEqual(entry["status"], "broken")
            self.assertEqual(entry["differences"], ["left panel is blank"])
            folder = rel.split("/", 1)[0]
            self.assertEqual(entry["screenshot"], f"{folder}/screenshot.png")
            self.assertEqual(entry["render"], f"{folder}/render.png")
            self.assertIsNotNone(
                store.capture_bytes(self.wt_id, f"{folder}/render.png"))
        # …and the stored column is the same object
        stored = render_check.load_results(self.wt_id)
        self.assertEqual(stored["pages"], results["pages"])
        self.assertEqual(stored["schema"], render_check.SCHEMA)
        self.assertEqual(stored["model"], "claude-test")
        self.assertIn("checked_at", stored)

    def test_pages_without_a_screenshot_report_no_baseline(self):
        # Only step-001 has ground truth; step-000 predates screenshots.
        self._screenshot("step-001")
        calls = []
        with mock.patch.object(render_check, "render_pages", self._fake_renderer(calls)), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict()):
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        self.assertEqual(results["pages"]["step-000/page.html"],
                         {"status": "no-baseline"})
        self.assertEqual(results["pages"]["step-001/page.html"]["status"], "ok")
        # the baseline-less page is never rendered
        self.assertEqual([j[0] for j in calls[0]["jobs"]], ["step-001/page.html"])

    def test_byte_identical_pages_share_one_render_and_verdict(self):
        # step-002 is a byte-for-byte duplicate of step-001.
        store.put_capture(self.wt_id, "step-002/page.html",
                          store.capture_bytes(self.wt_id, "step-001/page.html"))
        self._screenshot("step-000")
        self._screenshot("step-001")
        self._screenshot("step-002")
        calls = []
        with mock.patch.object(render_check, "render_pages", self._fake_renderer(calls)), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict(False, "minor", ["softer images"])) \
                as vision:
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        # two unique pages → two renders, two vision calls, not three
        self.assertEqual(len(calls[0]["jobs"]), 2)
        self.assertEqual(vision.call_count, 2)
        alias = results["pages"]["step-002/page.html"]
        self.assertEqual(alias["alias_of"], "step-001/page.html")
        self.assertEqual(alias["status"], "minor")

    def test_a_page_that_fails_to_render_is_an_error_not_a_crash(self):
        self._screenshot("step-000")
        self._screenshot("step-001")
        calls = []
        fake = self._fake_renderer(calls, skip=("step-001/page.html",))
        with mock.patch.object(render_check, "render_pages", fake), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict()):
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        self.assertEqual(results["pages"]["step-000/page.html"]["status"], "ok")
        self.assertEqual(results["pages"]["step-001/page.html"]["status"], "error")

    def test_parallel_judging_keys_verdicts_by_page(self):
        """Verdicts run on a thread pool; each must land on its own page
        regardless of completion order."""
        self._screenshot("step-000")
        self._screenshot("step-001")

        def vision(system, content, model, **kwargs):
            page_line = content[0]["text"]
            if "step-000" in page_line:
                return _verdict(False, "broken", ["left panel is blank"])
            return _verdict()

        with mock.patch.object(render_check, "render_pages", self._fake_renderer([])), \
                mock.patch.object(author, "call_claude_vision", vision):
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        self.assertEqual(results["pages"]["step-000/page.html"]["status"], "broken")
        self.assertEqual(results["pages"]["step-001/page.html"]["status"], "ok")

    def test_a_vision_refusal_is_an_error_for_that_page_only(self):
        self._screenshot("step-000")
        self._screenshot("step-001")
        replies = iter([_verdict(), author.AuthorError("The model declined this request")])

        def vision(system, content, model, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(render_check, "render_pages", self._fake_renderer([])), \
                mock.patch.object(author, "call_claude_vision", vision):
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        statuses = sorted(v["status"] for v in results["pages"].values())
        self.assertEqual(statuses, ["error", "ok"])

    def test_viewports_from_the_def_reach_the_renderer(self):
        self._screenshot("step-000")
        vp = {"width": 1440, "height": 812, "dpr": 2}
        definition = store.get(self.wt_id)["definition"]
        definition["_source"] = {"viewports": {"step-000": vp}}
        store.save_with_history(self.wt_id, definition)
        calls = []
        with mock.patch.object(render_check, "render_pages", self._fake_renderer(calls)), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict()):
            results = render_check.run_check(self.wt_id, "http://x", model="m")
        job = calls[0]["jobs"][0]
        self.assertEqual(job[1], vp)
        self.assertEqual(results["pages"]["step-000/page.html"]["viewport"], vp)

    def test_after_authoring_the_viewports_come_from_the_draft(self):
        # Authoring strips `_source` from the definition; the draft keeps it.
        from captcher import db
        self._screenshot("step-000")
        vp = {"width": 1440, "height": 900, "dpr": 1}
        definition = store.get(self.wt_id)["definition"]
        definition.pop("_source", None)
        store.save_with_history(self.wt_id, definition)
        draft = dict(definition, _source={"viewports": {"step-000": vp}})
        db.execute("update walkthroughs set draft = ? where id = ?",
                   (json.dumps(draft), self.wt_id))
        calls = []
        with mock.patch.object(render_check, "render_pages", self._fake_renderer(calls)), \
                mock.patch.object(author, "call_claude_vision",
                                  return_value=_verdict()):
            render_check.run_check(self.wt_id, "http://x", model="m")
        self.assertEqual(calls[0]["jobs"][0][1], vp)

    def test_an_unknown_walkthrough_is_a_clean_error(self):
        for missing in ("no-such-thing", str(uuid.uuid4())):
            with self.subTest(walkthrough_id=missing), \
                    self.assertRaises(render_check.RenderCheckError):
                render_check.run_check(missing, "http://x", model="m")

    def test_a_walkthrough_with_no_captured_pages_is_a_clean_error(self):
        """This absorbs the old "the capture directory is missing" case: there
        is no directory to be missing, and an empty prefix and a prefix holding
        no page.html are the same answer to whoever asked for a check."""
        bare = fixtures.make_walkthrough(self.owner_id, title="Bare", steps=0)["id"]
        with self.assertRaises(render_check.RenderCheckError):
            render_check.run_check(bare, "http://x", model="m")


class RenderCheckEndpointTests(unittest.TestCase):
    """POST runs the check (owner only, one at a time); GET serves the column."""

    def setUp(self):
        self.owner_id = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner_id, steps=1)["id"]
        store.put_capture(self.wt_id, "step-000/screenshot.png", PNG)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.owner = signed_in_client(self.base, self.owner_id)
        self.intruder = signed_in_client(self.base, new_member())

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def _post(self, client, path):
        return client.open(urllib.request.Request(self.base + path, data=b"",
                                                  method="POST"))

    def test_post_runs_the_check_against_the_servers_own_address(self):
        seen = {}

        def fake_run(walkthrough_id, base_url, cookie_header=None, model=None,
                     progress=None):
            seen.update(walkthrough_id=walkthrough_id, base_url=base_url,
                        cookie=cookie_header)
            return {"schema": render_check.SCHEMA, "model": "m",
                    "checked_at": "2026-07-30T00:00:00Z",
                    "pages": {"step-000/page.html": {"status": "ok"}}}

        with mock.patch.object(server.render_check, "run_check", fake_run):
            with self._post(self.owner, f"/api/walkthroughs/{self.wt_id}/render-check") as r:
                body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["pages"]["step-000/page.html"]["status"], "ok")
        self.assertEqual(seen["walkthrough_id"], self.wt_id)
        # self-addressed: the loopback + bound port, not the Host header
        self.assertEqual(seen["base_url"], self.base)
        self.assertIn("session=", seen["cookie"] or "")

    def test_a_render_check_error_is_a_422_with_the_message(self):
        with mock.patch.object(
                server.render_check, "run_check",
                side_effect=render_check.RenderCheckError("Playwright is not installed.")):
            try:
                self._post(self.owner, f"/api/walkthroughs/{self.wt_id}/render-check")
                self.fail("expected HTTP 422")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 422)
                self.assertIn("Playwright", json.loads(e.read())["error"])

    def test_only_the_owner_can_run_or_read_a_check(self):
        for method, path in (("POST", f"/api/walkthroughs/{self.wt_id}/render-check"),
                             ("GET", f"/api/walkthroughs/{self.wt_id}/render-check")):
            with self.subTest(method=method):
                try:
                    self.intruder.open(urllib.request.Request(
                        self.base + path, data=b"" if method == "POST" else None,
                        method=method))
                    self.fail("expected HTTP 404")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404)

    def test_get_serves_the_sidecar_and_null_before_any_check(self):
        with self.owner.open(self.base + f"/api/walkthroughs/{self.wt_id}/render-check") as r:
            self.assertEqual(json.loads(r.read()), {"pages": None})
        results = {"schema": render_check.SCHEMA, "model": "m",
                   "checked_at": "2026-07-30T00:00:00Z",
                   "pages": {"step-000/page.html": {"status": "minor"}}}
        store.save_render_check(self.wt_id, results)
        with self.owner.open(self.base + f"/api/walkthroughs/{self.wt_id}/render-check") as r:
            self.assertEqual(json.loads(r.read()), results)


if __name__ == "__main__":
    unittest.main()
