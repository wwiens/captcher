"""What a hostile capture ZIP must not be able to do.

`ingest.extract_capture_zip` is reached by anyone holding a capture token, and
it writes attacker-supplied entries to attacker-influenced keys. Two invariants
protect it, and neither had a test before this file:

  - **Key containment.** Entry names must compose a key inside the
    walkthrough's own prefix, and anything that does not is refused. This used
    to be a `relative_to` check on a resolved path under a destination
    directory; it is `blobs.key_for` returning None now, and the archive is
    rejected even when the name would merely have been *normalised* — see the
    aliasing note below.
  - **Expansion bounds.** The server caps the compressed upload (MAX_UPLOAD),
    but a ZIP's inflation ratio is unbounded — 200KB of zeros expanded to 200MB
    before `_read_bounded` existed, and nothing read a size first. That bound
    matters more, not less, now that the expansion is bytes uploaded to Storage
    rather than bytes written to a local disk.

The bombs here are built with real zeros rather than a forged `file_size`
header, because that header is written by whoever built the archive and the
extractor deliberately does not trust it. They stay small enough on disk to
build in-process: DEFLATE takes a run of zeros down about 1000:1.
"""
import io
import json
import unittest
import zipfile

from captcher import blobs
from captcher import ingest
from conftest import new_member

from . import capturefiles, fixtures


def _bomb_zip(entries):
    """A well-formed capture archive whose entries are runs of zero bytes.

    `entries` is a list of (name, size). The manifest is real so extraction
    fails on the size guard rather than on a missing anchor.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("walkthrough.json",
                   json.dumps({"title": "Bomb", "schema": ingest.CAPTURE_SCHEMA,
                               "initialCapture": {"folder": "step-000"}, "steps": []}))
        for name, size in entries:
            z.writestr(name, b"\0" * size)
    return buf.getvalue()


class ExpansionBoundTests(unittest.TestCase):
    def setUp(self):
        self.wid = fixtures.make_walkthrough(new_member(), steps=0)["id"]

    def stored_bytes(self):
        return sum(len(data) for data, _ in capturefiles.current().objects.values())

    def test_one_oversized_entry_is_refused(self):
        size = ingest.MAX_ENTRY_BYTES + (8 * 1024 * 1024)
        payload = _bomb_zip([("step-000/page.html", size)])
        # the point of the test: tiny on the wire, huge once inflated
        self.assertLess(len(payload), 4 * 1024 * 1024)

        with self.assertRaises(ValueError) as ctx:
            ingest.extract_capture_zip(payload, self.wid)
        self.assertIn("page.html", str(ctx.exception))

    def test_nothing_beyond_the_bound_reaches_storage(self):
        """The guard reads bounded and raises before the PUT, so a rejected
        bomb uploads nothing at all. On disk this used to leave one entry's
        partial write behind for the caller to clean up; over the network there
        is no partial object, because the read fails first."""
        payload = _bomb_zip([("step-000/page.html", ingest.MAX_ENTRY_BYTES * 2)])
        with self.assertRaises(ValueError):
            ingest.extract_capture_zip(payload, self.wid)
        self.assertLessEqual(self.stored_bytes(), ingest.MAX_ENTRY_BYTES)

    def test_many_legal_sized_entries_still_hit_the_total_bound(self):
        """Each entry under MAX_ENTRY_BYTES, the archive as a whole over
        MAX_TOTAL_BYTES — the case a per-file limit alone would miss."""
        each = ingest.MAX_ENTRY_BYTES
        count = (ingest.MAX_TOTAL_BYTES // each) + 2
        payload = _bomb_zip([(f"step-{i:03d}/page.html", each) for i in range(count)])
        with self.assertRaises(ValueError) as ctx:
            ingest.extract_capture_zip(payload, self.wid)
        self.assertIn("archive expands past", str(ctx.exception))

    def test_an_ordinary_capture_still_extracts(self):
        """The bounds must not be reachable by a real capture."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("walkthrough.json",
                       json.dumps({"title": "Real", "schema": ingest.CAPTURE_SCHEMA,
                                   "initialCapture": {"folder": "step-000"},
                                   "steps": []}))
            z.writestr("step-000/page.html", "<html><body>hello</body></html>")
        ingest.extract_capture_zip(buf.getvalue(), self.wid)
        self.assertIsNotNone(
            capturefiles.current().text(blobs.key_for(self.wid, "step-000/page.html")))

    def test_an_oversized_manifest_is_refused_before_extraction(self):
        """zip_manifest() runs first, on the upload path that names the
        walkthrough from the capture's own title, and used to inflate the
        manifest whole with zf.read()."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("walkthrough.json", b"\0" * (ingest.MAX_ENTRY_BYTES + 1024))
        with self.assertRaises(ValueError):
            ingest.zip_walkthrough_title(buf.getvalue())


class KeyContainmentTests(unittest.TestCase):
    """Zip-slip, as it survives into an object store.

    An object key is a literal string with no `..` resolution, so a traversing
    entry could not escape the prefix the way it escaped a directory. It would
    do something subtler and just as bad: give two archive entries one key, and
    walk past any prefix check written with startswith(). Refused outright.
    """

    def setUp(self):
        self.wid = fixtures.make_walkthrough(new_member(), steps=0)["id"]

    def _zip_with(self, name):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("walkthrough.json",
                       json.dumps({"title": "T", "schema": ingest.CAPTURE_SCHEMA,
                                   "initialCapture": {"folder": "step-000"},
                                   "steps": []}))
            z.writestr(name, "pwned")
        return buf.getvalue()

    def test_a_traversing_entry_is_refused(self):
        for name in ("../escaped.html",
                     "step-000/../../escaped.html",
                     "step-000/../../../etc/escaped.html"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    ingest.extract_capture_zip(self._zip_with(name), self.wid)
                self.assertIn("unsafe path", str(ctx.exception))
                self.assertEqual(capturefiles.current().keys(), [],
                                 "nothing was written before the refusal")

    def test_an_absolute_entry_is_refused_rather_than_normalised(self):
        """zipfile strips the leading slash on write, but an archive built by
        hand need not. `key_for` would strip it and produce a key inside the
        prefix — contained, but the same key a sibling entry named
        `escaped.html` produces. Two entries collapsing onto one object is the
        aliasing the check exists to prevent, so the archive is refused."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("walkthrough.json",
                       json.dumps({"title": "T", "schema": ingest.CAPTURE_SCHEMA,
                                   "initialCapture": {"folder": "step-000"},
                                   "steps": []}))
            info = zipfile.ZipInfo("/escaped.html")
            z.writestr(info, "pwned")
        with self.assertRaises(ValueError) as ctx:
            ingest.extract_capture_zip(buf.getvalue(), self.wid)
        self.assertIn("unsafe path", str(ctx.exception))

    def test_nothing_lands_outside_the_walkthroughs_own_prefix(self):
        """The invariant the individual cases exist to protect."""
        neighbour = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        ingest.extract_capture_zip(self._zip_with("step-000/page.html"), self.wid)
        for key in capturefiles.current().keys():
            self.assertTrue(key.startswith(blobs.prefix_for(self.wid)), key)
            self.assertFalse(key.startswith(blobs.prefix_for(neighbour)), key)


if __name__ == "__main__":
    unittest.main()
