"""Diagnostic logs: the recorder's capture log arriving with an upload, and
the record every AI edit attempt leaves — exact request, exact diff.

author.run_edit is stubbed; the stubs save through store.save_with_history the
way the real pass does, so the history rows and page backups the diff is built
from are the real ones.
"""
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile

from captcher import author
from captcher import diagnostics
from captcher import jobs
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures


def capture_zip():
    manifest = {"schema": "page-capture-walkthrough@1", "title": "Logged capture",
                "createdAt": "2026-10-06T10:00:00.000Z",
                "initialCapture": {"folder": "step-000"}, "steps": []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("walkthrough.json", json.dumps(manifest))
        z.writestr("step-000/page.html", "<html><body><h1>Start</h1></body></html>")
    return buf.getvalue()


def multipart(fields):
    boundary = uuid.uuid4().hex
    body = b""
    for name, (filename, data) in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                 f"filename=\"{filename}\"\r\n\r\n").encode() + data + b"\r\n"
    return body + f"--{boundary}--\r\n".encode(), boundary


CAPTURE_LOG = {
    "schema": "captcher-capture-log@1", "kind": "walkthrough",
    "startedAt": "2026-10-06T10:00:00.000Z", "omitted": 0,
    "entries": [
        {"at": "2026-10-06T10:00:00.100Z", "elapsed": None, "level": "mark",
         "text": "recording started — \"Logged capture\" on example.com"},
        {"at": "2026-10-06T10:00:01.200Z", "elapsed": 1.1, "level": "mark",
         "text": "viewport screenshot — 212 KB"},
        {"at": "2026-10-06T10:00:02.000Z", "elapsed": None, "level": "",
         "text": "upload: POST https://app.example/api/upload — 2 KB, 60s deadline"},
    ],
}


class Server:
    def __init__(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class CaptureLogUploadTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.srv = Server()
        self.client = signed_in_client(self.srv.base, self.owner_id)

    def tearDown(self):
        self.srv.close()

    def upload(self, log_bytes=None):
        fields = {"capture_zip": ("capture.zip", capture_zip())}
        if log_bytes is not None:
            fields["capture_log"] = ("capture-log.json", log_bytes)
        body, boundary = multipart(fields)
        req = urllib.request.Request(
            self.srv.base + "/api/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "X-Captcher-Recorder": "1.2.3"})
        r = self.client.open(req)
        self.assertEqual(r.status, 201)
        return json.loads(r.read())["walkthrough_id"]

    def test_the_log_is_stored_exactly_with_the_walkthrough(self):
        wid = self.upload(json.dumps(CAPTURE_LOG).encode())
        [row] = diagnostics.entries(wid)
        self.assertEqual(row["kind"], "capture")
        self.assertEqual(row["member_id"], self.owner_id)
        self.assertEqual(row["entry"]["entries"], CAPTURE_LOG["entries"])
        self.assertEqual(row["entry"]["recorder"], "1.2.3")
        self.assertIn("received_at", row["entry"])

    def test_the_log_is_not_a_capture_file(self):
        """It must never ride into exports or SCORM packages."""
        wid = self.upload(json.dumps(CAPTURE_LOG).encode())
        self.assertFalse([f for f in store.capture_files(wid) if "log" in f.lower()])

    def test_an_upload_without_a_log_still_works(self):
        wid = self.upload()
        self.assertEqual(diagnostics.entries(wid), [])

    def test_a_malformed_log_never_fails_the_upload(self):
        for bad in (b"not json", b"[1,2,3]", b'{"entries": "nope"}',
                    b"x" * (diagnostics.MAX_CAPTURE_LOG_BYTES + 1)):
            wid = self.upload(bad)
            self.assertEqual(diagnostics.entries(wid), [], bad[:20])

    def test_logs_go_with_the_walkthrough(self):
        wid = self.upload(json.dumps(CAPTURE_LOG).encode())
        store.delete(wid)
        self.assertEqual(diagnostics.entries(wid), [])


class AiEditLogTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.row = fixtures.make_walkthrough(self.owner_id, steps=2)
        self.wt_id = self.row["id"]
        store.mark_authored(self.wt_id)
        self.srv = Server()
        self.client = signed_in_client(self.srv.base, self.owner_id)
        self._orig_run_edit = author.run_edit

    def tearDown(self):
        author.run_edit = self._orig_run_edit
        held = jobs.active_for(self.wt_id)
        if held is not None:
            held.finish()
            jobs.release(held)
        self.srv.close()

    def edit(self, body):
        req = urllib.request.Request(
            f"{self.srv.base}/api/walkthroughs/{self.wt_id}/ai-edit",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            return self.client.open(req).status
        except urllib.error.HTTPError as e:
            return e.code

    def stub(self, rewrite_page=True, then_raise=None, before_save=None):
        def fake(walkthrough_id, instruction, model=None, current_step=None,
                 selection=None, progress=None, replay_ctx=None):
            if before_save:
                raise before_save
            edited = dict(store.get(walkthrough_id)["definition"])
            edited["title"] = "Edited by AI"
            changed = {}
            if rewrite_page:
                page = store.capture_text(walkthrough_id, "step-001/page.html")
                changed["step-001/page.html"] = page.replace("<h1>", '<h1 data-hint="new">', 1)
            stamp = store.save_with_history(walkthrough_id, edited,
                                            changed_pages=changed or None)
            if then_raise:
                raise then_raise
            return {"walkthrough_id": walkthrough_id, "title": edited["title"],
                    "steps_before": 2, "steps_after": 2,
                    "pages_changed": len(changed), "pages_changed_paths": sorted(changed),
                    "warnings": ["minor"], "stamp": stamp, "summary": "Renamed it."}
        author.run_edit = fake

    def only_edit_entry(self):
        rows = diagnostics.entries(self.wt_id, "ai_edit")
        self.assertEqual(len(rows), 1)
        return rows[0]["entry"]

    def test_a_saved_edit_records_the_request_as_sent_and_the_exact_diff(self):
        self.stub()
        request = {"instruction": "  Rename the title, and hint the heading  ",
                   "current_step": 1,
                   "selection": {"page": "step-001/page.html", "selector": "h1"}}
        self.assertEqual(self.edit(request), 200)
        e = self.only_edit_entry()
        # As sent: untrimmed instruction, raw selection.
        self.assertEqual(e["request"], request)
        self.assertEqual(e["outcome"], "saved")
        self.assertEqual(e["result"]["summary"], "Renamed it.")
        ch = e["changes"]
        self.assertEqual(ch["history_stamp"], e["result"]["stamp"])
        self.assertRegex(ch["definition_diff"], r'(?m)^-  "title": ')
        self.assertIn('+  "title": "Edited by AI",', ch["definition_diff"])
        [page] = ch["pages"]
        self.assertEqual(page["page"], "step-001/page.html")
        self.assertRegex(page["diff"], r'(?m)^\+\s*<h1 data-hint="new">$')
        self.assertRegex(page["diff"], r"(?m)^-\s*<h1>$")
        self.assertFalse(page["truncated"])
        self.assertNotEqual(page["before_sha256"], page["after_sha256"])

    def test_a_refused_edit_records_the_request_and_no_changes(self):
        self.stub(before_save=author.AuthorError("This walkthrough is still a draft"))
        self.edit({"instruction": "do a thing"})
        e = self.only_edit_entry()
        self.assertEqual(e["request"]["instruction"], "do a thing")
        self.assertEqual(e["outcome"], "failed")
        self.assertEqual(e["error"]["type"], "AuthorError")
        self.assertNotIn("traceback", e["error"])
        self.assertNotIn("changes", e)

    def test_a_crash_records_its_traceback(self):
        self.stub(before_save=RuntimeError("boom"))
        self.edit({"instruction": "do a thing"})
        e = self.only_edit_entry()
        self.assertEqual(e["error"]["type"], "RuntimeError")
        self.assertIn("boom", e["error"]["traceback"])

    def test_a_failure_after_the_save_still_records_the_diff(self):
        self.stub(then_raise=jobs.Cancelled("cancelled"))
        self.edit({"instruction": "rename"})
        e = self.only_edit_entry()
        self.assertEqual(e["outcome"], "cancelled_after_save")
        self.assertIn('+  "title": "Edited by AI",', e["changes"]["definition_diff"])
        self.assertEqual([p["page"] for p in e["changes"]["pages"]], ["step-001/page.html"])

    def test_an_answered_question_records_the_answer_and_no_changes(self):
        def fake(walkthrough_id, instruction, **_kw):
            return {"walkthrough_id": walkthrough_id, "kind": "answer",
                    "answer": "No, the options are not in the capture.",
                    "suggested_change": "Add them", "steps_before": 2,
                    "steps_after": 2, "pages_changed": 0,
                    "pages_changed_paths": [], "warnings": [], "summary": "",
                    "stamp": None}
        author.run_edit = fake
        self.assertEqual(self.edit({"instruction": "do we have the options?"}), 200)
        e = self.only_edit_entry()
        self.assertEqual(e["outcome"], "answered")
        self.assertEqual(e["result"], {"answer": "No, the options are not in the capture.",
                                       "suggested_change": "Add them"})
        self.assertNotIn("changes", e)

    def test_an_earlier_save_is_not_blamed_on_a_failed_edit(self):
        store.save_with_history(self.wt_id, dict(store.get(self.wt_id)["definition"]))
        self.stub(before_save=author.AuthorError("nope"))
        self.edit({"instruction": "x"})
        self.assertEqual(self.only_edit_entry()["outcome"], "failed")


class PageDiffTests(unittest.TestCase):
    def test_one_attribute_on_a_single_line_page_is_a_one_tag_diff(self):
        body = "".join(f"<div id=d{i}>text {i}</div>" for i in range(5000))
        before = f"<html><body>{body}</body></html>"
        after = before.replace("<div id=d2500>", "<div id=d2500 hidden>")
        diff, truncated = diagnostics.page_diff(before, after, "p.html")
        self.assertFalse(truncated)
        self.assertLess(len(diff), 1000)
        self.assertIn("-<div id=d2500>", diff)
        self.assertIn("+<div id=d2500 hidden>", diff)

    def test_an_oversized_diff_is_truncated_and_says_so(self):
        before = "<p>a</p>" * 300_000
        after = "<p>b</p>" * 300_000
        diff, truncated = diagnostics.page_diff(before, after, "p.html")
        self.assertTrue(truncated)
        self.assertEqual(len(diff), diagnostics.MAX_PAGE_DIFF_CHARS)

    def test_definition_diff_ignores_jsonb_key_order(self):
        self.assertEqual(diagnostics.definition_diff({"a": 1, "b": 2}, {"b": 2, "a": 1}), "")
