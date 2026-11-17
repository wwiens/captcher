"""Guards for the catalog card image and the three caches in front of it.

A shelf of twelve cards used to cost twenty-four storage round trips before
it painted a pixel — a LIST to see the cached thumbnail was there and a GET to
fetch it, per card, on every cold browser cache. Everything here pins the parts
of thumbs.py that removed those, plus the format change that made what is left
a fifth of the bytes.

The bucket is tests/capturefiles.py, so "a round trip" is a method call that can
be counted rather than something to take on faith.
"""
import io
import unittest

from captcher import blobs
from captcher import store
from captcher import thumbs

try:
    from PIL import Image, features
    HAS_PIL = True
    HAS_WEBP = features.check("webp")
except ImportError:                                    # pragma: no cover
    HAS_PIL = HAS_WEBP = False

WID = "11111111-1111-1111-1111-111111111111"
DEF = {"steps": [{"page": "step-000/page.html"}, {"page": "step-001/page.html"}]}


def frame(width=1280, height=800, colour=(30, 40, 60)):
    """A PNG the size of a captured viewport, so the downscale has work to do."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "PNG")
    return out.getvalue()


class Counter:
    """Wraps the capture store's verbs and counts them; restore() unwraps."""

    VERBS = ("get", "put", "list_keys", "delete")

    def __init__(self):
        self.calls = {}
        self._originals = {name: getattr(blobs, name) for name in self.VERBS}
        for name, fn in self._originals.items():
            setattr(blobs, name, self._wrap(name, fn))

    def restore(self):
        for name, fn in self._originals.items():
            setattr(blobs, name, fn)

    def _wrap(self, name, fn):
        def counted(*a, **kw):
            self.calls[name] = self.calls.get(name, 0) + 1
            return fn(*a, **kw)
        return counted

    def reset(self):
        self.calls = {}


@unittest.skipUnless(HAS_PIL, "Pillow is unavailable")
class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.counter = Counter()
        thumbs._memo.clear()
        thumbs._memo_bytes = 0
        self.addCleanup(self.counter.restore)
        self.addCleanup(thumbs._memo.clear)

    def put(self, rel, data):
        store.put_capture(WID, rel, data, blobs.content_type_for(rel))

    def rels(self):
        return set(store.capture_files(WID))

    # ---- the image itself -------------------------------------------------

    def test_a_capture_frame_becomes_a_card_image(self):
        self.put("step-000/screenshot.png", frame())
        thumb = thumbs.thumbnail(WID, DEF)
        self.assertIsNotNone(thumb)
        with Image.open(io.BytesIO(thumb.data)) as im:
            self.assertEqual(im.width, thumbs.THUMB_WIDTH,
                             "the frame is width-capped for a 300px cell at 2x")

    @unittest.skipUnless(HAS_WEBP, "this Pillow has no WebP")
    def test_the_card_image_is_webp_and_much_smaller_than_the_frame(self):
        src = frame()
        self.put("step-000/screenshot.png", src)
        thumb = thumbs.thumbnail(WID, DEF)
        self.assertEqual(thumb.content_type, "image/webp")
        self.assertIn(f"step-000/{thumbs.THUMB_WEBP}", self.rels())
        self.assertLess(len(thumb.data), len(src))

    def test_a_capture_with_no_frame_has_no_card_image(self):
        """The card falls back to a plate; the route 404s."""
        self.put("step-000/page.html", "<html></html>")
        self.assertIsNone(thumbs.thumbnail(WID, DEF))

    def test_a_later_step_is_searched_when_the_first_has_no_frame(self):
        self.put("step-001/screenshot.png", frame())
        self.assertIsNotNone(thumbs.thumbnail(WID, DEF))

    # ---- the caches -------------------------------------------------------

    def test_a_repeat_request_touches_no_storage_at_all(self):
        """The point of the memo. The bucket cache removed the downscale from a
        repeat request; it did not remove the network."""
        self.put("step-000/screenshot.png", frame())
        first = thumbs.thumbnail(WID, DEF)
        self.counter.reset()
        again = thumbs.thumbnail(WID, DEF)
        self.assertEqual(self.counter.calls, {})
        self.assertEqual(first.data, again.data)

    def test_a_cached_image_is_read_without_a_listing(self):
        """A cold process against a warm bucket: one GET, no LIST. The listing
        was a round trip spent to learn what the read itself reports."""
        self.put("step-000/screenshot.png", frame())
        thumbs.thumbnail(WID, DEF)          # writes the cached image
        thumbs._memo.clear()
        thumbs._memo_bytes = 0
        self.counter.reset()
        self.assertIsNotNone(thumbs.thumbnail(WID, DEF))
        self.assertEqual(self.counter.calls.get("list_keys", 0), 0)
        self.assertEqual(self.counter.calls.get("get", 0), 1)

    def test_the_downscale_runs_once_across_processes(self):
        """The bucket copy is what a restarted server finds."""
        self.put("step-000/screenshot.png", frame())
        made = thumbs.thumbnail(WID, DEF)
        thumbs._memo.clear()
        thumbs._memo_bytes = 0
        self.counter.reset()
        again = thumbs.thumbnail(WID, DEF)
        self.assertEqual(made.data, again.data)
        self.assertEqual(self.counter.calls.get("put", 0), 0,
                         "a cache hit must not rewrite the object")

    def test_the_memo_is_bounded_by_bytes(self):
        """Unbounded, this is a memory leak wearing a cache's clothes."""
        saved = thumbs.MEMO_MAX_BYTES
        thumbs.MEMO_MAX_BYTES = 4096
        try:
            for i in range(12):
                thumbs._memo_put(f"id-{i}", thumbs._as_thumb(b"x" * 1024,
                                                             thumbs.THUMB_WEBP))
            self.assertLessEqual(thumbs._memo_bytes, 4096)
            self.assertIn("id-11", thumbs._memo, "newest survives")
            self.assertNotIn("id-0", thumbs._memo, "oldest evicted first")
        finally:
            thumbs.MEMO_MAX_BYTES = saved

    def test_a_hit_is_moved_to_the_end_so_it_is_not_evicted_next(self):
        for i in range(3):
            thumbs._memo_put(f"id-{i}", thumbs._as_thumb(b"x", thumbs.THUMB_WEBP))
        thumbs._memo_get("id-0")
        self.assertEqual(list(thumbs._memo)[-1], "id-0")

    # ---- invalidation -----------------------------------------------------

    def test_invalidate_drops_the_memo_and_both_object_names(self):
        """A render check rewrites render.png in place. A memo entry left
        behind is a stale card for as long as the server runs."""
        self.put("step-000/screenshot.png", frame(colour=(10, 10, 10)))
        before = thumbs.thumbnail(WID, DEF)
        self.put("step-000/screenshot.png", frame(colour=(200, 30, 30)))
        thumbs.invalidate(WID, "step-000")
        self.assertNotIn(WID, thumbs._memo)
        for name in thumbs.THUMB_NAMES:
            self.assertNotIn(f"step-000/{name}", self.rels())
        after = thumbs.thumbnail(WID, DEF)
        self.assertNotEqual(before.etag, after.etag,
                            "the new frame must reach the card")

    def test_invalidate_survives_a_bucket_that_will_not_delete(self):
        """It is called from the render check's success path; a cache that
        could not be cleared must not fail the check that cleared it."""
        def boom(key):
            raise blobs.StorageError("nope")
        blobs.delete = boom
        thumbs._memo_put(WID, thumbs._as_thumb(b"x", thumbs.THUMB_WEBP))
        thumbs.invalidate(WID, "step-000")
        self.assertNotIn(WID, thumbs._memo, "the memo clears whatever storage does")

    # ---- the etag ---------------------------------------------------------

    def test_the_etag_is_a_quoted_digest_of_the_bytes(self):
        a = thumbs._as_thumb(b"same", thumbs.THUMB_WEBP)
        b = thumbs._as_thumb(b"same", thumbs.THUMB_PNG)
        c = thumbs._as_thumb(b"other", thumbs.THUMB_WEBP)
        self.assertTrue(a.etag.startswith('"') and a.etag.endswith('"'))
        self.assertEqual(a.etag, b.etag, "it describes the bytes, not the name")
        self.assertNotEqual(a.etag, c.etag)

    # ---- the format upgrade ------------------------------------------------

    @unittest.skipUnless(HAS_WEBP, "this Pillow has no WebP")
    def test_a_legacy_png_thumbnail_is_upgraded_once_and_removed(self):
        """A library made before the WebP change. Two answers to one question
        in the bucket is how a cache starts serving whichever it listed first.
        """
        self.put("step-000/screenshot.png", frame())
        self.put(f"step-000/{thumbs.THUMB_PNG}", frame(640, 400))
        thumb = thumbs.thumbnail(WID, DEF)
        self.assertEqual(thumb.content_type, "image/webp")
        rels = self.rels()
        self.assertIn(f"step-000/{thumbs.THUMB_WEBP}", rels)
        self.assertNotIn(f"step-000/{thumbs.THUMB_PNG}", rels)


class ConditionalGetTests(unittest.TestCase):
    """The route's half of the browser cache. Without a validator an expired
    entry costs the whole image again, because there is nothing for the browser
    to ask *about*."""

    def match(self, header, etag='"abc"'):
        from captcher import server

        class Request:
            headers = {} if header is None else {"If-None-Match": header}

        return server.Handler.if_none_match(Request(), etag)

    def test_no_header_is_not_a_match(self):
        self.assertFalse(self.match(None))
        self.assertFalse(self.match(""))

    def test_the_same_etag_matches(self):
        self.assertTrue(self.match('"abc"'))

    def test_a_different_etag_does_not(self):
        self.assertFalse(self.match('"def"'))

    def test_a_list_matches_on_any_member(self):
        """If-None-Match is a list, and a browser holding two variants sends
        both."""
        self.assertTrue(self.match('"zzz", "abc"'))

    def test_a_weak_validator_still_matches(self):
        """Weak comparison is what the spec asks for on a GET, and a proxy is
        free to weaken a validator in transit."""
        self.assertTrue(self.match('W/"abc"'))
        self.assertTrue(self.match('"abc"', 'W/"abc"'))

    def test_star_matches_anything(self):
        self.assertTrue(self.match("*"))


class PreferredNameTests(unittest.TestCase):
    def test_the_written_format_is_probed_not_assumed(self):
        """Pillow can be built without WebP, and on such a build every encode
        silently falls back to PNG — so "is the cached object the format we
        write?" has to be answerable without attempting one."""
        self.assertIn(thumbs.preferred_name(), thumbs.THUMB_NAMES)
        self.assertIn(thumbs.preferred_name(), thumbs.CONTENT_TYPES)


if __name__ == "__main__":
    unittest.main()
