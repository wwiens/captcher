"""Framing policy, per route.

Nothing here was sending `X-Frame-Options` or a framing CSP, so every
authenticated page — /account, the editor, the branding form — could be framed
by any site and clicked through. `Handler.send()` now defaults every response to
`SAMEORIGIN`.

The two exceptions are what make this worth a test rather than a one-liner:

  - **/embed/<id> must stay framable by anyone.** It is the customer-site widget
    and deliberately sends `frame-ancestors *`. The default must not be added
    alongside it — `X-Frame-Options: SAMEORIGIN` would win in browsers that
    honour both and silently break every embed.
  - **/capture/<id>/... must stay framable by us.** The player renders captured
    pages in a same-origin iframe, so `DENY` would break playback. SAMEORIGIN is
    the strongest setting that does not.
"""
import threading
import unittest
import urllib.request

from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures


class FramingHeaderTests(unittest.TestCase):
    def setUp(self):
        self.owner = new_member()
        self.row = fixtures.make_walkthrough(self.owner, title="Headers Test")
        self.walkthrough_id = self.row["id"]
        # published for embedding, so /embed/<id> is reachable. A column now,
        # not a sidecar file — but the rule it encodes is the same one: nothing
        # is public until `enabled` is explicitly true.
        store.save_embed_settings(self.walkthrough_id,
                                  {"enabled": True, "intro": True, "chrome": "full"})

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.client = signed_in_client(self.base, self.owner)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def _headers(self, path, authed=True):
        opener = self.client if authed else urllib.request.build_opener()
        with opener.open(self.base + path) as r:
            return r.headers

    def test_authenticated_pages_refuse_third_party_framing(self):
        for path in ("/", "/account", "/branding",
                     f"/player/{self.walkthrough_id}",
                     f"/review/{self.walkthrough_id}"):
            with self.subTest(path=path):
                self.assertEqual(self._headers(path).get("X-Frame-Options"),
                                 "SAMEORIGIN")

    def test_the_unlock_page_refuses_third_party_framing(self):
        """The one page shown before an owner exists (access-token mode)."""
        from captcher import auth
        saved = auth.ACCESS_TOKEN
        auth.ACCESS_TOKEN = "correct horse battery staple"
        self.addCleanup(setattr, auth, "ACCESS_TOKEN", saved)
        self.assertEqual(
            self._headers("/unlock", authed=False).get("X-Frame-Options"),
            "SAMEORIGIN")

    def test_json_apis_carry_it_too(self):
        self.assertEqual(self._headers("/api/walkthroughs").get("X-Frame-Options"),
                         "SAMEORIGIN")

    def test_every_response_declines_mime_sniffing(self):
        """The server declares a type on everything it sends — captured pages,
        JSON, plain-text errors — and nosniff stops a browser second-guessing
        that into something executable. Checked across the page kinds, the
        API, a captured page and the public embed."""
        for path, authed in (("/", True), ("/favicon.ico", False),
                             ("/api/walkthroughs", True),
                             (f"/capture/{self.walkthrough_id}/step-000/page.html", True),
                             (f"/embed/{self.walkthrough_id}", False),
                             ("/static/logo.svg", False)):
            with self.subTest(path=path):
                self.assertEqual(
                    self._headers(path, authed=authed).get("X-Content-Type-Options"),
                    "nosniff")

    def test_captured_pages_stay_framable_by_the_player(self):
        """Same-origin framing is how the player works at all. If this ever
        reads DENY, playback is broken."""
        headers = self._headers(f"/capture/{self.walkthrough_id}/step-000/page.html")
        self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_the_public_embed_stays_framable_by_any_site(self):
        """The customer-site widget. It sends its own frame-ancestors CSP, and
        the default must not be bolted on next to it."""
        headers = self._headers(f"/embed/{self.walkthrough_id}", authed=False)
        self.assertIn("frame-ancestors *",
                      headers.get("Content-Security-Policy", ""))
        self.assertIsNone(headers.get("X-Frame-Options"),
                          "X-Frame-Options alongside frame-ancestors * would "
                          "break embeds in browsers that honour both")


if __name__ == "__main__":
    unittest.main()
