"""Filesystem path containment — what is left of it.

`paths.safe_join` used to resolve a `/capture/` URL against a capture folder.
That folder is gone: captures are objects in a bucket, addressed through
`blobs.key_for`, and the traversal tests for it were ported to
tests/test_blob_keys.py rather than deleted — the boundary moved, it did not
disappear.

This file stays because ONE production caller survived the move:
`server.py` still resolves `/static/<rel>` with `paths.safe_join`. That is a
real, unauthenticated, attacker-controlled path on every request for a
stylesheet, so the containment check still has to be right, and these are the
tests that say so.

The sibling-prefix case is kept verbatim even though its original example
(`captures/add-points` versus `captures/add-points-v2`, produced by the old
-2/-3 id de-duplication) can no longer occur. It is the property the four
`str.startswith()` call sites got wrong, it is the property `is_within` exists
to provide, and a static directory sitting beside another whose name begins the
same way is exactly as reachable as a capture folder ever was.
"""
import os
import tempfile
import unittest
from pathlib import Path

from captcher import paths


class IsWithinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sibling_with_a_shared_name_prefix_is_not_inside(self):
        victim = self.root / "captures" / "add-points"
        attacker = self.root / "captures" / "add-points-v2"
        victim.mkdir(parents=True)
        attacker.mkdir(parents=True)
        secret = attacker / "secret.html"
        secret.write_text("x", encoding="utf-8")

        # what the old check did, and got wrong
        self.assertTrue(str(secret).startswith(str(victim)))
        # what the new check does
        self.assertFalse(paths.is_within(secret, victim))

    def test_plain_child_is_inside(self):
        child = self.root / "a" / "b" / "c.html"
        child.parent.mkdir(parents=True)
        child.write_text("x", encoding="utf-8")
        self.assertTrue(paths.is_within(child, self.root))

    def test_root_itself_is_inside(self):
        self.assertTrue(paths.is_within(self.root, self.root))

    def test_dotdot_cannot_escape(self):
        inside = self.root / "inside"
        inside.mkdir()
        self.assertFalse(paths.is_within(inside / ".." / "..", inside))

    def test_symlink_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        inside = self.root / "inside"
        inside.mkdir()
        link = inside / "link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertFalse(paths.is_within(link / "secret.txt", inside))


class SafeJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "ok.html").write_text("ok", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_joins_a_normal_relative_path(self):
        self.assertEqual(paths.safe_join(self.root, "ok.html"), self.root / "ok.html")

    def test_rejects_traversal(self):
        for rel in ("../etc/passwd", "a/../../etc/passwd", "..", "../"):
            with self.subTest(rel=rel):
                self.assertIsNone(paths.safe_join(self.root, rel))

    def test_rejects_absolute_paths(self):
        """(root / "/etc/passwd") discards root entirely — Path semantics."""
        self.assertIsNone(paths.safe_join(self.root, "/etc/passwd"))

    def test_rejects_a_sibling_prefix_escape(self):
        sibling = self.root.parent / (self.root.name + "-evil")
        sibling.mkdir(exist_ok=True)
        try:
            self.assertIsNone(
                paths.safe_join(self.root, f"../{sibling.name}/secret.html"))
        finally:
            sibling.rmdir()


class SlugShapeTests(unittest.TestCase):
    """A slug must never contain a slash.

    This used to guard route dispatch: ids were slugs, and the router splits on
    "/" and matches suffixes like "/pages". Ids are uuids now and are taken
    verbatim, so that reason is gone — but the property is load-bearing for two
    other reasons, and both are worse to get wrong than a mangled route was.

    `walkthroughs.slug` carries a CHECK constraint
    (`^[a-z0-9]+(-[a-z0-9]+)*$`), so a slug with a slash in it does not produce
    a bad URL — it aborts the INSERT, and an upload fails with a constraint
    violation naming a column the uploader never saw. And `/examples/<slug>` is
    a public route that matches its remainder against the configured list, so a
    slug that can contain a separator is a slug that can be spelled two ways.
    """

    def test_slugify_never_produces_a_slash(self):
        from captcher import ingest
        for raw in ("a/pages", "foo/embed", "a//b", "../etc/passwd",
                    "My Walkthrough!", "  spaces  ", "Ünïcōde", "a/b/c/export"):
            with self.subTest(raw=raw):
                out = ingest.slugify(raw)
                self.assertNotIn("/", out)
                self.assertRegex(out, r"^[a-z0-9-]*$")

if __name__ == "__main__":
    unittest.main()
