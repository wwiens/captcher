"""brandfetch.py — SSRF defences and the brand-signal parser.

This module had no tests at all, which is how three undefined names (unescape,
strip_data_uris, os) shipped in it after it was split out of author.py: every
one sits on the live "import brand from a URL" path, so the feature raised
NameError rather than working. The import smoke tests below exist to catch
exactly that class of breakage.

The SSRF checks matter more than their size suggests: infer_branding_from_url()
fetches a URL supplied by the user, so without a public-address allowlist it is
a request forgery primitive against anything the server can reach.
"""
import http.server
import threading
import unittest
from unittest.mock import patch

from captcher import brandfetch


class ImportSurfaceTests(unittest.TestCase):
    """Every name each public function needs must actually resolve."""

    def test_extract_brand_signals_runs(self):
        html = ('<html><head><title>Acme &amp; Co</title>'
                '<meta property="og:site_name" content="Acme">'
                '<link rel="icon" href="/fav.png">'
                '</head><body></body></html>')
        signals = brandfetch.extract_brand_signals(html, "https://acme.example/")
        # HTML entities decoded -> `unescape` is in scope
        self.assertEqual(signals["title"], "Acme & Co")
        self.assertIn("https://acme.example/fav.png", signals["favicons"])

    def test_trim_html_for_branding_runs(self):
        html = '<html><body><img src="data:image/png;base64,' + "A" * 200 + '"></body></html>'
        out = brandfetch._trim_html_for_branding(html)   # needs strip_data_uris
        self.assertNotIn("A" * 200, out)

    def test_infer_branding_reads_the_model_env_var(self):
        """Exercises the `os` reference at the top of infer_branding_from_url."""
        with patch.object(brandfetch, "fetch_public_html",
                          side_effect=brandfetch.AuthorError("stop here")):
            with self.assertRaises(brandfetch.AuthorError):
                brandfetch.infer_branding_from_url("https://acme.example/")


class PublicHostTests(unittest.TestCase):
    """_is_public_host resolves the name and rejects non-public space."""

    def _resolving_to(self, ip):
        # getaddrinfo -> [(family, type, proto, canonname, sockaddr)]
        return patch.object(brandfetch.socket, "getaddrinfo",
                            return_value=[(2, 1, 6, "", (ip, 80))])

    def test_public_address_is_allowed(self):
        with self._resolving_to("93.184.216.34"):
            self.assertTrue(brandfetch._is_public_host("example.com"))

    def test_private_and_local_ranges_are_rejected(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1",
                   "169.254.169.254",   # cloud metadata
                   "0.0.0.0", "224.0.0.1",
                   "100.64.0.1",        # shared address space (CGNAT); is_private misses it
                   "192.0.0.1",         # IETF protocol assignments
                   "::ffff:127.0.0.1",  # IPv4-mapped loopback
                   "::ffff:10.0.0.5",   # IPv4-mapped private
                   "fc00::1", "::1"):
            with self.subTest(ip=ip), self._resolving_to(ip):
                self.assertFalse(brandfetch._is_public_host("evil.example"),
                                 f"{ip} should not be reachable")

    def test_unresolvable_host_is_rejected(self):
        with patch.object(brandfetch.socket, "getaddrinfo",
                          side_effect=OSError("no such host")):
            self.assertFalse(brandfetch._is_public_host("nope.invalid"))


class ValidateFetchUrlTests(unittest.TestCase):
    def test_non_http_schemes_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://acme.example/x",
                    "gopher://acme.example/", "data:text/html,<h1>x"):
            with self.subTest(url=url):
                with self.assertRaises(brandfetch.AuthorError):
                    brandfetch._validate_fetch_url(url)

    def test_a_private_target_is_rejected(self):
        with patch.object(brandfetch, "_is_public_host", return_value=False):
            with self.assertRaises(brandfetch.AuthorError):
                brandfetch._validate_fetch_url("http://127.0.0.1:8080/")

    def test_a_public_https_url_passes(self):
        with patch.object(brandfetch, "_is_public_host", return_value=True):
            brandfetch._validate_fetch_url("https://acme.example/brand")


if __name__ == "__main__":
    unittest.main()


class _RedirectingSite(http.server.BaseHTTPRequestHandler):
    """/r -> 302 -> /final (HTML); /img -> 302 -> /logo (PNG-ish);
    /away -> 302 to another host; /headless serves an image but refuses HEAD;
    anything else 404."""
    def do_GET(self):
        if self.path == "/r":
            return self._redirect("/final")
        if self.path == "/img":
            return self._redirect("/logo")
        if self.path == "/away":
            return self._redirect("http://internal.example/admin")
        if self.path == "/final":
            return self._body(b"<html><title>Landed</title></html>", "text/html; charset=utf-8")
        if self.path in ("/logo", "/headless"):
            return self._body(b"\x89PNG\r\n\x1a\n....", "image/png")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        """Plenty of real servers answer a HEAD with 405 and the same GET with
        200 — the probe has to survive that, so one path here does it."""
        if self.path == "/headless":
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.head_only = True
        self.do_GET()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not getattr(self, "head_only", False):
            self.wfile.write(data)

    def log_message(self, *args):
        pass


class _LocalSiteMixin:
    """One loopback server standing in for a public site, shared by the suites
    below. The allowlist itself is exercised in PublicHostTests."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), _RedirectingSite)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def _allow_local(self):
        return patch.object(brandfetch, "_is_public_host",
                            side_effect=lambda host: host == "127.0.0.1")


class RedirectTests(_LocalSiteMixin, unittest.TestCase):
    """Redirects are followed by hand so every hop is re-validated.

    urllib's redirect handler raises HTTPError when its subclass declines to
    follow, so a `redirect_request -> None` override made the manual loop in
    fetch_public_html unreachable: every URL that redirected (http->https, a
    www. hop) failed as "Could not fetch that URL (HTTP 301)". The handler now
    hands the 3xx response back, and these tests pin that against a real
    local server rather than a stubbed opener.
    """

    def test_an_html_redirect_is_followed_to_the_final_page(self):
        with self._allow_local():
            final_url, html = brandfetch.fetch_public_html(self.base + "/r")
        self.assertEqual(final_url, self.base + "/final")
        self.assertIn("Landed", html)

    def test_an_image_redirect_is_followed_to_the_final_image(self):
        with self._allow_local():
            final_url, ctype, data = brandfetch.fetch_public_image(self.base + "/img")
        self.assertEqual(final_url, self.base + "/logo")
        self.assertEqual(ctype, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_every_hop_is_revalidated_against_the_allowlist(self):
        """A public page that redirects to an internal host is refused at the
        hop — the whole point of following redirects by hand."""
        with self._allow_local():
            with self.assertRaises(brandfetch.AuthorError) as raised:
                brandfetch.fetch_public_html(self.base + "/away")
        self.assertIn("private or internal", str(raised.exception))

    def test_a_non_redirect_error_status_is_still_reported(self):
        with self._allow_local():
            with self.assertRaises(brandfetch.AuthorError) as raised:
                brandfetch.fetch_public_html(self.base + "/missing")
        self.assertIn("404", str(raised.exception))


class ImageProbeTests(_LocalSiteMixin, unittest.TestCase):
    """Candidate image URLs are confirmed before the model is offered them.

    What went wrong without this: extract_brand_signals appends `/favicon.ico`
    as an outright guess, the model had nothing else to pick, and the guess was
    saved into a brand profile. It only surfaced at export time, where a 404 on
    a tab icon failed an entire SCORM build.
    """

    def resolves(self, path):
        with self._allow_local():
            return brandfetch.image_url_resolves(self.base + path)

    def test_an_image_resolves(self):
        self.assertTrue(self.resolves("/logo"))

    def test_a_404_does_not(self):
        self.assertFalse(self.resolves("/favicon.ico"))

    def test_a_page_that_is_not_an_image_does_not(self):
        """A site that answers every path with its HTML shell would otherwise
        have every guess pass."""
        self.assertFalse(self.resolves("/final"))

    def test_a_redirect_to_an_image_resolves(self):
        self.assertTrue(self.resolves("/img"))

    def test_a_server_that_refuses_head_still_resolves(self):
        self.assertTrue(self.resolves("/headless"))

    def test_a_redirect_onto_an_internal_host_does_not(self):
        """The probe re-validates every hop like the fetchers do, so it cannot
        be used to reach inside the network."""
        self.assertFalse(self.resolves("/away"))

    def test_pruning_keeps_only_what_answers(self):
        signals = {
            "favicons": [self.base + "/favicon.ico", self.base + "/logo"],
            "logo_candidates": [self.base + "/logo", self.base + "/missing.png"],
        }
        with self._allow_local():
            brandfetch.prune_unreachable_images(signals)
        self.assertEqual(signals["favicons"], [self.base + "/logo"])
        self.assertEqual(signals["logo_candidates"], [self.base + "/logo"])

    def test_pruning_an_empty_candidate_list_is_fine(self):
        signals = {"favicons": [], "logo_candidates": []}
        self.assertIs(brandfetch.prune_unreachable_images(signals), signals)
