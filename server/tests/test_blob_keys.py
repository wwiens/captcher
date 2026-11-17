"""Storage key containment — the object-store half of path traversal.

`blobs.key_for` is what `paths.safe_join` was for the capture folder: the one
place that decides whether an untrusted relative path may address an object,
and the reason the five scattered copies of that decision could be collapsed
into one. `rel` arrives off the URL on the `/capture/` route, so this is a
security boundary and not a formatting helper.

The old suite tested this against a resolved directory (`captures/add-points`
versus `captures/add-points-v2`, the sibling-prefix bug that made four
`str.startswith()` call sites wrong). There is no directory any more, so the
sibling-prefix case is ported here as what it became: a prefix is a uuid
followed by `/`, and the question is whether a key can be composed that lands
outside it or that reaches one object by two different names.

No database and no bucket: these are pure functions.
"""
import unittest

from captcher import blobs

WID = "0f3a5a5c-9e3a-4f21-8c2f-1b7d5f9c2a10"
OTHER = "0f3a5a5c-9e3a-4f21-8c2f-1b7d5f9c2a11"


class PrefixTests(unittest.TestCase):
    def test_the_prefix_is_the_id_and_a_slash(self):
        """The single place that knows the layout. If this changes, every
        stored object's address changes with it."""
        self.assertEqual(blobs.prefix_for(WID), WID + "/")

    def test_two_walkthroughs_never_share_a_prefix(self):
        """The property `str.startswith()` used to get wrong. It holds now
        because the trailing slash is part of the prefix — not because uuids
        happen not to prefix one another."""
        self.assertFalse(blobs.prefix_for(OTHER).startswith(blobs.prefix_for(WID)))
        key = blobs.key_for(WID, "step-000/page.html")
        self.assertTrue(key.startswith(blobs.prefix_for(WID)))
        self.assertFalse(key.startswith(blobs.prefix_for(OTHER)))


class KeyForTests(unittest.TestCase):
    def test_an_ordinary_capture_path_becomes_a_key_under_the_prefix(self):
        for rel in ("step-000/page.html", "step-000/screenshot.png",
                    "walkthrough.json", "auto/summary.html",
                    ".history/20260907-101010/step-000/page.html"):
            with self.subTest(rel=rel):
                self.assertEqual(blobs.key_for(WID, rel), f"{WID}/{rel}")

    def test_a_traversing_segment_is_refused(self):
        """An object store has no `..` resolution of its own, so such a key
        would not literally escape the prefix. It would do two worse things:
        let two different URLs address one object, and sail past any later
        prefix check written the naive way. Rejecting it outright is cheaper
        than reasoning about which callers normalise."""
        for rel in ("../other/page.html", "..", "../", "a/../../b.html",
                    "step-000/../../../etc/passwd", "./page.html",
                    "a/./b.html", f"../{OTHER}/page.html"):
            with self.subTest(rel=rel):
                self.assertIsNone(blobs.key_for(WID, rel))

    def test_an_empty_segment_is_refused(self):
        """`a//b` and `a/b` would address the same object in some stores and
        different ones in others. Neither answer is worth depending on."""
        for rel in ("", "   ", "/", "//", "a//b.html", "a/", "a/b/"):
            with self.subTest(rel=rel):
                self.assertIsNone(blobs.key_for(WID, rel))

    def test_a_backslash_or_a_nul_is_refused(self):
        """A backslash is a separator on the machine that recorded the capture
        and a literal character in a key, which is exactly the mismatch that
        produces an alias. A NUL truncates in anything written in C."""
        for rel in ("step-000\\page.html", "..\\..\\etc\\passwd",
                    "page.html\x00.png"):
            with self.subTest(rel=rel):
                self.assertIsNone(blobs.key_for(WID, rel))

    def test_a_leading_slash_is_stripped_rather_than_refused(self):
        """Documented because it is the one input that is normalised instead of
        rejected, so `/page.html` and `page.html` are the same object. That is
        safe — the result is still under the prefix — but it is an alias, and a
        future caller that hashes the `rel` it was given rather than the key
        would be wrong about it."""
        self.assertEqual(blobs.key_for(WID, "/step-000/page.html"),
                         f"{WID}/step-000/page.html")

    def test_a_missing_rel_is_refused_rather_than_addressing_the_prefix(self):
        """Otherwise `key_for(id, None)` would compose the prefix itself, and a
        GET of a prefix is a listing in some stores."""
        self.assertIsNone(blobs.key_for(WID, None))

    def test_every_produced_key_is_inside_the_prefix(self):
        """The invariant the individual cases exist to protect, stated once."""
        prefix = blobs.prefix_for(WID)
        for rel in ("a.html", "a/b.html", "a/b/c/d.png", "sp ace/page.html",
                    "ünïcōde/page.html", "step-000/page.html?x=1"):
            with self.subTest(rel=rel):
                key = blobs.key_for(WID, rel)
                self.assertIsNotNone(key)
                self.assertTrue(key.startswith(prefix))
                self.assertNotIn("/../", "/" + key + "/")


class ContentTypeTests(unittest.TestCase):
    def test_captured_pages_are_served_as_utf8_html(self):
        """The /capture/ route hands this straight to the browser, and a page
        served without a charset renders mojibake for every non-ASCII capture."""
        self.assertEqual(blobs.content_type_for(f"{WID}/step-000/page.html"),
                         "text/html; charset=utf-8")

    def test_an_unknown_or_absent_extension_is_octet_stream(self):
        """Never guessed, and never text/html — a capture object with a name
        this module does not recognise must not be sniffed into a script."""
        for key in (f"{WID}/nameless", f"{WID}/a.exe", f"{WID}/a.HTML.exe"):
            with self.subTest(key=key):
                self.assertEqual(blobs.content_type_for(key),
                                 "application/octet-stream")

    def test_the_extension_match_is_case_insensitive(self):
        """Recordings on case-insensitive filesystems produce .PNG."""
        self.assertEqual(blobs.content_type_for(f"{WID}/a.PNG"), "image/png")


class FileContainmentTests(unittest.TestCase):
    """Keys become paths, so the file layer refuses anything key_for would
    have refused, even when a caller skips key_for."""

    def test_captures_live_in_this_sessions_data_folder(self):
        from conftest import test_home
        self.assertEqual(blobs.root(), test_home() / "captures")

    def test_an_escaping_key_never_reaches_the_filesystem(self):
        for key in ("../outside.html", f"{WID}/../../outside.html", ""):
            with self.subTest(key=key):
                with self.assertRaises(blobs.StorageError):
                    blobs.put(key, b"x")
                self.assertFalse(blobs.exists(key))

    def test_a_round_trip_and_a_prefix_delete(self):
        blobs.put(f"{WID}/step-000/page.html", "<p>hi</p>")
        blobs.put(f"{WID}/step-000/shot.png", b"\x89PNG")
        self.assertEqual(blobs.get_text(f"{WID}/step-000/page.html"), "<p>hi</p>")
        self.assertEqual(blobs.list_keys(blobs.prefix_for(WID)),
                         [f"{WID}/step-000/page.html", f"{WID}/step-000/shot.png"])
        self.assertEqual(blobs.delete_prefix(blobs.prefix_for(WID)), 2)
        self.assertEqual(blobs.list_keys(""), [])
        with self.assertRaises(blobs.NotFound):
            blobs.get(f"{WID}/step-000/page.html")


if __name__ == "__main__":
    unittest.main()
