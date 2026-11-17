"""The FREEZE_SHIM on the live /capture/ route.

Exports always injected export.FREEZE_SHIM (network primitives hang before any
product script runs); the live route did not, and the divergence produced a
real symptom: a captured page's survey widget popped up in the player render
but was never on the user's screen (FIELD-NOTES.md 2026-07-30). send_file now
injects the bytes twin on served capture HTML. These pin the injection point
and its limits.
"""
import threading
import unittest

from captcher import export
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures


class FreezePageBytes(unittest.TestCase):
    def test_the_shim_lands_first_thing_in_head(self):
        body = b'<html><head data-x="1"><script>fetch("/api")</script></head><body></body></html>'
        out = export.freeze_page_bytes(body)
        # after the head tag, before the page's own script
        self.assertLess(out.index(export.FREEZE_SHIM_BYTES),
                        out.index(b'fetch("/api")'))
        self.assertTrue(out.startswith(b'<html><head data-x="1">' + export.FREEZE_SHIM_BYTES))

    def test_a_page_with_no_prologue_gets_it_prepended(self):
        out = export.freeze_page_bytes(b"<body>x</body>")
        self.assertTrue(out.startswith(export.FREEZE_SHIM_BYTES))

    def test_the_doctype_stays_the_first_token(self):
        """SingleFile pages usually have no <head>. Prepending put the shim
        ahead of `<!DOCTYPE html>`, which then no longer counts, and every
        such page rendered in quirks mode — on a real HubSpot deal page that
        pushed three sidebar cards out of view (FIELD-NOTES.md 2026-10-06)."""
        body = (b"<!DOCTYPE html> <html lang=en-us class=lang-en "
                b"style=--copilot-sidebar-width:0px;--global-nav-height:44px>"
                b"<meta charset=utf-8><body>x</body>")
        out = export.freeze_page_bytes(body)
        self.assertTrue(out.startswith(b"<!DOCTYPE html> <html lang=en-us "))
        self.assertEqual(out.index(export.FREEZE_SHIM_BYTES), body.index(b"<meta"))

    def test_a_header_element_is_not_mistaken_for_head(self):
        """compressHTML pages often have no <head> at all; the first
        <head…>-shaped bytes are then a <header> deep inside the body, and
        the shim must not be spliced into the middle of the page there —
        found on a real HubSpot capture, and the export-path regex had the
        same flaw."""
        body = b'<html><meta charset=utf-8><body><header class=top>x</header></body></html>'
        out = export.freeze_page_bytes(body)
        self.assertTrue(out.startswith(b"<html>" + export.FREEZE_SHIM_BYTES))
        self.assertIn(b"<header class=top>x</header>", out)
        # and the str twin used by exports behaves the same
        processed = export.preprocess_page(body.decode())
        self.assertLess(processed.index("rc-export"), processed.index("<header"))

    def test_a_head_inside_an_iframe_srcdoc_is_not_matched(self):
        """SingleFile inlines frames as srcdoc with raw markup, so the first
        `<head>` in a head-less page can belong to a frame."""
        body = b"<!DOCTYPE html><body><iframe srcdoc='<html><head><p>f'></iframe></body>"
        out = export.freeze_page_bytes(body)
        self.assertTrue(out.startswith(b"<!DOCTYPE html>" + export.FREEZE_SHIM_BYTES))
        self.assertIn(b"srcdoc='<html><head><p>f'", out)

    def test_the_prologue_is_read_like_the_tokenizer_reads_it(self):
        body = (b'\xef\xbb\xbf<!-- saved --><!doctype html>\n'
                b'<html data-x="a>b" lang=en>\n<head>\n<title>t</title>')
        out = export.freeze_page_bytes(body)
        self.assertEqual(out.index(export.FREEZE_SHIM_BYTES), body.index(b"<title>"))

    def test_bytes_and_str_placement_agree(self):
        for body in ("<!DOCTYPE html><html lang=en><meta charset=utf-8><body>",
                     "<html><head data-x=1><script>fetch('/a')</script>",
                     "<body>x", "\ufeff<!doctype html>\n<head>\n<title>"):
            self.assertEqual(export.freeze_page_bytes(body.encode()),
                             export.freeze_page(body).encode(), body)

    def test_bytes_and_str_shims_are_the_same_script(self):
        """The export path injects the str constant; the live route the bytes
        twin. They must never drift apart."""
        self.assertEqual(export.FREEZE_SHIM_BYTES.decode("ascii"), export.FREEZE_SHIM)


class ServedCapturePages(unittest.TestCase):
    """GET /capture/<id>/<step>/page.html carries the shim; other files don't."""

    def setUp(self):
        self.owner_id = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner_id, steps=1)["id"]
        store.put_capture(self.wt_id, "step-000/screenshot.png",
                          b"\x89PNG\r\n\x1a\n" + bytes(16))

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.owner = signed_in_client(self.base, self.owner_id)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def _get(self, path):
        with self.owner.open(self.base + path) as r:
            return r.read()

    def test_served_page_html_carries_the_shim_before_page_content(self):
        body = self._get(f"/capture/{self.wt_id}/step-000/page.html")
        self.assertIn(export.FREEZE_SHIM_BYTES, body)
        # right after the prologue: before the fixture page's own <title>
        self.assertLess(body.index(export.FREEZE_SHIM_BYTES), body.index(b"<title>"))
        # and the bridge still rides at the end, untouched by the new splice
        self.assertIn(b"bridge.js", body.split(export.FREEZE_SHIM_BYTES, 1)[1])

    def test_non_html_capture_files_are_untouched(self):
        body = self._get(f"/capture/{self.wt_id}/step-000/screenshot.png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertNotIn(export.FREEZE_SHIM_BYTES, body)


if __name__ == "__main__":
    unittest.main()
