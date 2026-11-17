"""The published capture limits: steps and distinct pages per recording.

Checked on upload before a row exists (`ingest.check_capture_limits`), so an
over-limit recording costs nothing and leaves nothing behind. Distinct means
byte-distinct, as `author.unique_pages` counts them: revisiting a page is free.
The numbers are stated on the plans page, which reads them from `ingest`.
"""
import io
import json
import unittest
import urllib.error
import urllib.request
import zipfile

from captcher import ingest
from captcher import store
from conftest import new_member, signed_in_client

from .test_diagnostics import Server, multipart


def capture(n_steps, distinct_pages=None):
    """A capture of `n_steps` clicks, one snapshot each, cycling through
    `distinct_pages` different page bodies (default: every page different)."""
    distinct_pages = distinct_pages or n_steps + 1
    steps = [{"folder": f"step-{i:03d}", "files": {"html": "page.html"},
              "actions": [{"type": "click", "element": "button", "label": f"Go {i}"}]}
             for i in range(1, n_steps + 1)]
    manifest = {"schema": ingest.CAPTURE_SCHEMA, "title": "Limits",
                "initialCapture": {"folder": "step-000"}, "steps": steps}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("walkthrough.json", json.dumps(manifest))
        for i in range(n_steps + 1):
            z.writestr(f"step-{i:03d}/page.html",
                       f"<html><body>page {i % distinct_pages}</body></html>")
    return buf.getvalue()


def check(zip_bytes):
    manifest = ingest.zip_manifest(zip_bytes)
    ingest.check_capture_limits(zip_bytes, manifest, ingest.convert(manifest))


class CaptureLimitTests(unittest.TestCase):
    def test_a_capture_at_the_limits_is_accepted(self):
        check(capture(ingest.MAX_CAPTURE_STEPS,
                      distinct_pages=ingest.MAX_CAPTURE_PAGES))

    def test_too_many_steps_is_refused_with_a_reason(self):
        with self.assertRaises(ingest.CaptureTooLarge) as cm:
            check(capture(ingest.MAX_CAPTURE_STEPS + 1, distinct_pages=2))
        self.assertIn(f"limit is {ingest.MAX_CAPTURE_STEPS}", str(cm.exception))

    def test_too_many_distinct_pages_is_refused(self):
        with self.assertRaises(ingest.CaptureTooLarge) as cm:
            check(capture(ingest.MAX_CAPTURE_PAGES + 1))
        self.assertIn("different pages", str(cm.exception))

    def test_repeated_pages_count_once(self):
        check(capture(ingest.MAX_CAPTURE_PAGES + 5, distinct_pages=3))

    def test_it_is_a_value_error_for_callers_that_do_not_tell_it_apart(self):
        self.assertTrue(issubclass(ingest.CaptureTooLarge, ValueError))


class UploadRouteTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.srv = Server()
        self.client = signed_in_client(self.srv.base, self.owner_id)

    def tearDown(self):
        self.srv.close()

    def test_an_over_limit_upload_is_a_413_and_creates_nothing(self):
        body, boundary = multipart(
            {"capture_zip": ("capture.zip", capture(ingest.MAX_CAPTURE_STEPS + 1, 2))})
        req = urllib.request.Request(
            self.srv.base + "/api/upload", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.client.open(req)
        self.assertEqual(cm.exception.code, 413)
        answer = json.loads(cm.exception.read())
        self.assertEqual(answer["code"], "capture_limit")
        self.assertIn("shorter parts", answer["error"])
        self.assertFalse(store.load_walkthroughs(self.owner_id))
