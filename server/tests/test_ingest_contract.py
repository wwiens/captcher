"""The capture-extension -> server wire contract.

The extension advertises schema "page-capture-walkthrough@1" on every upload,
and until this suite existed nothing checked it: a future @2 with different
semantics would have been ingested silently and wrongly. PRD.md CAP-017
asserted this requirement; these tests are it.

Also covers the manifest-name compatibility window. The extension writes
walkthrough.json now, but ZIPs recorded before that carry lesson.json, so both
must keep working.

WHAT MOVED

`ingest.convert()` takes the parsed manifest and returns a definition. It
writes nothing and it has no `ingest.ROOT` to be redirected at — which is why
these tests no longer redirect one. `extract_capture_zip` writes objects into
one walkthrough's Storage prefix rather than unpacking a directory, so it takes
a walkthrough id and the row has to exist first.

There is no `capture_dir` in the converted definition to assert on any more.
What replaced it is asserted instead, in ExtensionRoundTripTests: the objects
land under the walkthrough's own uuid, and the step `page` values are the
capture-relative paths that address them.
"""
import io
import json
import unittest
import zipfile

from captcher import blobs
from captcher import ingest
from captcher import store
from conftest import new_member

from . import capturefiles, fixtures


def _manifest(schema=ingest.CAPTURE_SCHEMA, steps=None, drop_schema=False):
    m = {
        "title": "Contract Test",
        "createdAt": "2026-07-25T00:00:00Z",
        "initialCapture": {"folder": "step-000"},
        "steps": steps if steps is not None else [
            {"folder": "step-001", "files": {"html": "page.html"},
             "actions": [{"type": "click", "element": "button", "label": "Save"}]},
        ],
    }
    if not drop_schema:
        m["schema"] = schema
    return m


def _zip(manifest, manifest_name="walkthrough.json",
         steps=("step-000", "step-001"), extra=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(manifest_name, json.dumps(manifest))
        for s in steps:
            z.writestr(f"{s}/page.html", "<html><body>x</body></html>")
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


class SchemaValidationTests(unittest.TestCase):
    """No database: convert() is a pure function of the manifest now."""

    def test_the_current_schema_is_accepted(self):
        ingest.check_capture_schema(ingest.CAPTURE_SCHEMA)

    def test_a_future_schema_is_rejected(self):
        for bad in ("page-capture-walkthrough@2", "page-capture-lesson@1",
                    "something-else", "walkthrough-def@1"):
            with self.subTest(schema=bad):
                with self.assertRaises(ValueError) as ctx:
                    ingest.check_capture_schema(bad)
                self.assertIn(bad, str(ctx.exception))

    def test_captures_predating_the_schema_field_are_still_accepted(self):
        for missing in (None, ""):
            with self.subTest(schema=missing):
                ingest.check_capture_schema(missing)

    def test_convert_rejects_an_unknown_schema(self):
        with self.assertRaises(ValueError):
            ingest.convert(_manifest(schema="page-capture-walkthrough@2"))

    def test_convert_accepts_the_current_schema(self):
        self.assertTrue(ingest.convert(_manifest()))

    def test_convert_does_not_invent_an_id(self):
        """At conversion time the row does not exist, so there is nothing to
        mirror. Inventing a value a later save would have to correct is exactly
        how `capture_dir` came to disagree with the thing it pointed at."""
        self.assertNotIn("id", ingest.convert(_manifest()))
        self.assertEqual(ingest.convert(_manifest(), "given-id")["id"], "given-id")

    def test_the_converted_definition_carries_no_capture_dir(self):
        """Stamping a dead key into every ingested walkthrough would be worse
        than the indirection it replaced."""
        self.assertNotIn("capture_dir", json.dumps(ingest.convert(_manifest())))


class ManifestNameTests(unittest.TestCase):
    """The extension writes walkthrough.json; older ZIPs carry lesson.json."""

    def setUp(self):
        self.wid = fixtures.make_walkthrough(new_member(), steps=0)["id"]

    def stored(self, rel):
        return capturefiles.current().text(blobs.key_for(self.wid, rel))

    def test_walkthrough_json_extracts(self):
        ingest.extract_capture_zip(_zip(_manifest(steps=[])), self.wid)
        self.assertIsNotNone(self.stored("walkthrough.json"))

    def test_legacy_lesson_json_is_normalised(self):
        ingest.extract_capture_zip(
            _zip(_manifest(steps=[]), manifest_name="lesson.json"), self.wid)
        self.assertIsNotNone(self.stored("walkthrough.json"))
        self.assertIsNone(self.stored("lesson.json"))

    def test_an_archive_carrying_both_names_keeps_the_modern_one(self):
        """Renaming over a real walkthrough.json would silently prefer the
        older manifest."""
        modern = _manifest(steps=[])
        modern["title"] = "Modern"
        legacy = json.dumps(dict(modern, title="Legacy"))
        ingest.extract_capture_zip(
            _zip(modern, extra=[("lesson.json", legacy)]), self.wid)
        self.assertEqual(json.loads(self.stored("walkthrough.json"))["title"],
                         "Modern")

    def test_an_archive_with_no_manifest_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("step-000/page.html", "<html></html>")
        with self.assertRaises(ValueError):
            ingest.extract_capture_zip(buf.getvalue(), self.wid)
        self.assertEqual(capturefiles.current().keys(), [])

    def test_an_entry_that_needs_normalising_is_refused_rather_than_normalised(self):
        """`key_for` strips a leading slash, because the /capture/ route hands
        it URL fragments — so "/escaped.html" would be accepted, contained, and
        stored at the same key as a sibling entry named "escaped.html". Two
        archive entries collapsing onto one object is the aliasing key_for
        exists to prevent, so an archive needing normalisation is refused."""
        for bad in ("/escaped.html", "../escaped.html", "step-000/../../x.html"):
            with self.subTest(entry=bad):
                with self.assertRaises(ValueError) as ctx:
                    ingest.extract_capture_zip(
                        _zip(_manifest(steps=[]), extra=[(bad, "x")]), self.wid)
                self.assertIn("unsafe path", str(ctx.exception))


class ExtensionRoundTripTests(unittest.TestCase):
    """A ZIP shaped exactly as the extension's side panel packages one, pushed
    through the server's real ingest path."""

    def test_a_capture_ingests_to_a_draft_walkthrough_under_its_own_prefix(self):
        manifest = {
            "schema": ingest.CAPTURE_SCHEMA,
            "title": "Extension Round Trip",
            "createdAt": "2026-07-25T08:00:00Z",
            "initialCapture": {"folder": "step-000"},
            "steps": [
                {"folder": "step-001", "files": {"html": "page.html"},
                 "actions": [{"type": "click", "element": "row", "label": "Marcus Osei"}]},
                {"folder": "step-002", "files": {"html": "page.html"},
                 "actions": [{"type": "input", "element": "input",
                              "label": "Points", "value": "2500"}]},
            ],
        }
        owner = new_member()
        definition = ingest.convert(manifest)
        row = store.create(owner, definition["title"], definition)
        written = ingest.extract_capture_zip(
            _zip(manifest, steps=("step-000", "step-001", "step-002")), row["id"])

        self.assertEqual(written, 4)   # the manifest plus three pages
        self.assertEqual(definition["schema"], "walkthrough-def@1")
        self.assertEqual(definition["_source"]["capture_schema"], ingest.CAPTURE_SCHEMA)
        self.assertEqual(len(definition["steps"]), 3)   # initial + two actions
        self.assertEqual([s["page"] for s in definition["steps"]],
                         ["step-000/page.html", "step-001/page.html",
                          "step-002/page.html"])

        # The prefix is the walkthrough's own uuid. Nothing stored anywhere
        # says so — it is derived, which is what stops it drifting.
        self.assertEqual(store.capture_files(row["id"]),
                         ["step-000/page.html", "step-001/page.html",
                          "step-002/page.html", "walkthrough.json"])
        for key in capturefiles.current().keys():
            self.assertTrue(key.startswith(blobs.prefix_for(row["id"])), key)

    def test_a_failed_extraction_leaves_the_row_free_of_half_a_capture(self):
        """There is no directory to rmtree any more: a caller whose extraction
        fails deletes the row it just created, and store.delete clears the
        prefix with it."""
        owner = new_member()
        row = store.create(owner, "Doomed", ingest.convert(_manifest()))
        with self.assertRaises(ValueError):
            ingest.extract_capture_zip(
                _zip(_manifest(), extra=[("../escape.html", "x")]), row["id"])
        store.delete(row["id"])
        self.assertEqual(capturefiles.current().keys(), [])


class ScreenshotMetadataTests(unittest.TestCase):
    """Screenshots and viewports are additive on @1: the extension writes a
    step-NNN/screenshot.png plus files.screenshot and a viewport field
    (a sibling `screenshot` on initialCapture, which has no files dict).
    convert() must carry the per-folder viewports into _source for the render
    check — while manifests without either keep converting exactly as before."""

    def test_viewports_reach_the_walkthrough_def(self):
        vp0 = {"width": 1440, "height": 812, "dpr": 2}
        vp1 = {"width": 1280, "height": 720, "dpr": 1}
        m = _manifest(steps=[
            {"folder": "step-001",
             "files": {"html": "page.html", "screenshot": "screenshot.png"},
             "viewport": vp1, "actions": []},
        ])
        m["initialCapture"] = {"folder": "step-000",
                               "screenshot": "screenshot.png", "viewport": vp0}
        self.assertEqual(ingest.convert(m)["_source"]["viewports"],
                         {"step-000": vp0, "step-001": vp1})

    def test_a_manifest_without_screenshots_converts_unchanged(self):
        self.assertNotIn("viewports", ingest.convert(_manifest())["_source"])


class ScreenshotExtractionTests(unittest.TestCase):
    def test_screenshot_png_extracts_verbatim(self):
        """Bytes, not text. A PNG that came back through a decode/encode round
        trip would still be a file and would no longer be an image."""
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(16))
        wid = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        ingest.extract_capture_zip(
            _zip(_manifest(steps=[]), steps=("step-000",),
                 extra=[("step-000/screenshot.png", png)]), wid)
        self.assertEqual(store.capture_bytes(wid, "step-000/screenshot.png"), png)

    def test_a_png_is_stored_under_its_own_content_type(self):
        """The /capture/ route serves these straight to a browser."""
        wid = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        ingest.extract_capture_zip(
            _zip(_manifest(steps=[]), steps=("step-000",),
                 extra=[("step-000/screenshot.png", b"\x89PNG")]), wid)
        storage = capturefiles.current()
        self.assertEqual(
            storage.content_type(blobs.key_for(wid, "step-000/screenshot.png")),
            "image/png")
        self.assertEqual(
            storage.content_type(blobs.key_for(wid, "step-000/page.html")),
            "text/html; charset=utf-8")


class RecordedMetadataTests(unittest.TestCase):
    """Newer captures carry per-action metadata the recorder verified against
    the live DOM: `selector` (querySelectorAll-unique at event time),
    `optionValue` (the <option>'s value attribute, not its display text), and
    `docTitle`/`heading` context. The manifest schema stays @1 — the fields
    are additive and old captures keep working through the guess path."""

    def convert(self, actions):
        return ingest.convert(_manifest(steps=[
            {"folder": "step-001", "files": {"html": "page.html"},
             "actions": actions}]))

    def test_recorded_selector_is_used_alone(self):
        wt = self.convert([{"type": "click", "element": "button", "label": "Save",
                            "selector": 'button[name="save"]'}])
        self.assertEqual(wt["steps"][0]["action"]["target"],
                         {"selector": 'button[name="save"]'})

    def test_without_a_recorded_selector_the_guess_path_is_unchanged(self):
        wt = self.convert([{"type": "click", "element": "button", "label": "Save"}])
        self.assertEqual(wt["steps"][0]["action"]["target"],
                         {"selector": "button", "text": "Save"})

    def test_select_prefers_optionValue_and_recorded_selector(self):
        wt = self.convert([{"type": "select", "element": "select",
                            "label": "Reason", "value": "Fraud — suspicious activity",
                            "optionValue": "fraud", "selector": "#reason"}])
        action = wt["steps"][0]["action"]
        self.assertEqual(action["value"], "fraud")
        self.assertEqual(action["target"], {"selector": "#reason"})

    def test_select_without_optionValue_keeps_the_legacy_split(self):
        wt = self.convert([{"type": "select", "element": "select",
                            "value": "Fraud — suspicious activity"}])
        action = wt["steps"][0]["action"]
        self.assertEqual(action["value"], "Fraud")
        self.assertEqual(action["target"], {"selector": "select"})

    def test_input_uses_the_recorded_selector(self):
        wt = self.convert([{"type": "input", "element": "input", "value": "2500",
                            "selector": 'input[name="points"]'}])
        self.assertEqual(wt["steps"][0]["action"]["target"],
                         {"selector": 'input[name="points"]'})

    def test_doc_title_and_heading_reach_the_draft_instruction(self):
        wt = self.convert([{"type": "click", "element": "button", "label": "Save",
                            "docTitle": "Contacts | Salesforce",
                            "heading": "New Contact"}])
        self.assertIn("Contacts | Salesforce / New Contact",
                      wt["steps"][0]["instruction"])


class MalformedStepTests(unittest.TestCase):
    def test_missing_files_html_is_a_clean_error_not_a_keyerror(self):
        """This used to raise KeyError and surface to the client as a 500."""
        with self.assertRaises(ValueError) as ctx:
            ingest.convert(_manifest(steps=[{"folder": "step-001"}]))
        self.assertIn("files.html", str(ctx.exception))

    def test_missing_folder_is_a_clean_error(self):
        with self.assertRaises(ValueError):
            ingest.convert(_manifest(steps=[{"files": {"html": "page.html"}}]))

    def test_an_untitled_capture_gets_a_placeholder_rather_than_a_folder_name(self):
        """There is no folder name left to fall back on — a capture is a uuid
        prefix now."""
        m = _manifest()
        m["title"] = "   "
        self.assertEqual(ingest.convert(m)["title"], "Untitled capture")


if __name__ == "__main__":
    unittest.main()
