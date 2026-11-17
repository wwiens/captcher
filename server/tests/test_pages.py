"""Characterization tests for every server-rendered page.

These exist to make the Phase 3 template extraction provable. ~2,100 lines of
server.py are HTML/CSS/JS embedded as Python strings; moving them into real
template files must not change a single byte of rendered output. Each page is
rendered here and compared against a golden snapshot under tests/golden/.

Regenerate deliberately (and diff the result before committing):

    CAPTCHER_UPDATE_GOLDEN=1 python3 -m pytest tests/test_pages.py

Volatile substrings (timestamps, tokens, uuids) are normalized in `_scrub` so
the goldens are stable across runs and machines.

THE UUIDS ARE THE NEW VOLATILE THING, AND THEY MATTER MOST

Ids used to be title slugs — `test-walkthrough` rendered the same on every
machine, forever. They are uuids the database mints now, so a walkthrough id
appears in `/player/<id>`, in the catalog's embedded JSON and in the review
page, and it is different on every single run. Left unscrubbed, the first
regeneration would bake one run's ids into the goldens and the suite would fail
on its own second run — which is a worse failure than no golden at all, because
it looks like a regression.

So every uuid is replaced with `<UUID>`. That deliberately costs a little
precision: a golden can no longer prove *which* id was rendered. What it still
proves is the thing these files exist for — that the markup around it did not
change.
"""
import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE = os.environ.get("CAPTCHER_UPDATE_GOLDEN") == "1"

# Every uuid, in the canonical hyphenated form psycopg and the routes both use.
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

# Routes that must simply not 500, checked separately from the goldens.
STATIC_SMOKE_ROUTES = [
    "/api/walkthroughs",
    "/api/whoami",
    "/api/branding",
    "/api/collections",
    "/api/settings",
    "/static/player.js",
    "/static/player.css",
    "/static/player2.js",
    "/static/player2.css",
    "/static/bridge.js",
    "/static/embed.js",
]


def _scrub(html: str) -> str:
    """Remove anything that legitimately varies between runs."""
    # ISO timestamps and short dates
    html = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.+Z-]+", "<TIMESTAMP>", html)
    html = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", html)
    # the account page's "Member since <Month> <Year>": the test member is
    # created today, so an unscrubbed golden failed every time the month turned
    html = re.sub(r"Member since [A-Z][a-z]+ \d{4}", "Member since <MONTH>", html)
    # capture tokens and session values (auth.API_TOKEN_PREFIX). A stale
    # pattern here would leave a real token in a committed golden, which is
    # the whole point of the rule.
    html = re.sub(r"cap_[A-Za-z0-9_-]{20,}", "<TOKEN>", html)
    # this run's temporary data folder (Settings names the .env inside it)
    from conftest import test_home
    html = html.replace(str(test_home()), "<CAPTCHER_HOME>")
    # whether this machine has Playwright and its Chromium (the review page)
    html = re.sub(r'"ready": (true|false)', '"ready": <READY>', html)
    # walkthrough, collection, brand-profile and member ids — see the note at
    # the top of this file
    html = UUID_RE.sub("<UUID>", html)
    # cache-busting query strings, if any
    html = re.sub(r"\?v=\d+", "?v=<V>", html)
    return html


class PageRenderTests(unittest.TestCase):
    """Boots a real server against a synthetic walkthrough and snapshots every
    page. Per-test setup rather than per-class, because conftest empties the
    database between tests."""

    def setUp(self):
        self.owner = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner)["id"]

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

        # A client carrying this test member's signed cookie, so the pages
        # render this member's walkthrough rather than the local owner's.
        self.client = signed_in_client(self.base, self.owner)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def page_routes(self):
        """(name, path) for every page rendered from a template."""
        return [
            ("catalog", "/"),
            ("account", "/account"),
            ("branding", "/branding"),
            ("review", f"/review/{self.wt_id}"),
            ("player", f"/player/{self.wt_id}"),
            ("player1", f"/player1/{self.wt_id}"),
        ]

    def _get(self, path):
        return self.client.open(self.base + path)

    def test_every_page_matches_its_golden(self):
        GOLDEN_DIR.mkdir(exist_ok=True)
        mismatches = []
        for name, path in self.page_routes():
            with self.subTest(page=name):
                resp = self._get(path)
                self.assertEqual(resp.status, 200, f"{path} did not return 200")
                html = _scrub(resp.read().decode("utf-8"))

                golden = GOLDEN_DIR / f"{name}.html"
                if UPDATE or not golden.is_file():
                    golden.write_text(html, encoding="utf-8")
                    continue
                expected = golden.read_text(encoding="utf-8")
                if expected != html:
                    mismatches.append(
                        f"{name} ({path}): rendered output differs from "
                        f"tests/golden/{name}.html "
                        f"({len(expected)} -> {len(html)} chars)")
        if mismatches:
            self.fail(
                "Rendered pages changed:\n  " + "\n  ".join(mismatches) +
                "\n\nIf intended, regenerate with:\n"
                "  CAPTCHER_UPDATE_GOLDEN=1 python3 -m pytest tests/test_pages.py")

    def test_a_fresh_install_opens_on_the_catalog_with_no_sign_in(self):
        """No cookie, no accounts: the browser is the local owner."""
        from captcher import auth
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.geturl(), self.base + "/")
            self.assertIn('id="cards"', r.read().decode("utf-8"))
        self.assertIsNotNone(auth.get_member(auth.LOCAL_OWNER_ID))
        with urllib.request.urlopen(self.base + "/api/whoami") as r:
            self.assertEqual(json.loads(r.read())["email"], auth.LOCAL_OWNER_LABEL)

    def test_edit_link_serves_the_classic_player(self):
        """/player/<id> is the popup player, but edit mode only exists in the
        classic one — the catalog's Edit link (?edit=1) must keep landing
        there, or Edit opens a player with no editor in it."""
        popup = self._get(f"/player/{self.wt_id}").read().decode("utf-8")
        editing = self._get(f"/player/{self.wt_id}?edit=1").read().decode("utf-8")
        classic = self._get(f"/player1/{self.wt_id}").read().decode("utf-8")
        self.assertIn('id="step-pop"', popup)
        self.assertNotIn('id="editor"', popup)
        self.assertIn('id="editor"', editing)
        self.assertEqual(editing, classic)

    def test_pages_carry_no_unsubstituted_placeholders(self):
        for name, path in self.page_routes():
            with self.subTest(page=name):
                html = self._get(path).read().decode("utf-8")
                leftover = re.findall(r"__[A-Z][A-Z0-9_]*__", html)
                self.assertFalse(
                    leftover,
                    f"{path} leaked unsubstituted placeholders: {sorted(set(leftover))}")

    def test_catalog_api_keys_match_what_the_catalog_js_reads(self):
        """Pins the /api/walkthroughs contract.

        A blanket rename once turned the dict key "captured_at" into
        "store.captured_at", which broke the catalog's "captured N ago" label
        and its sort-by-date — silently, because nothing rendered server-side
        changed. The keys below are the ones templates/_catalog-script.html
        actually reads.
        """
        rows = json.loads(self._get("/api/walkthroughs").read().decode("utf-8"))
        self.assertTrue(rows, "no walkthroughs returned")
        required = {"id", "title", "track", "level", "estimated_minutes",
                    "steps", "todos", "scenario", "authored", "authored_at",
                    "captured_at"}
        for row in rows:
            with self.subTest(walkthrough=row.get("id")):
                self.assertTrue(required <= set(row),
                                f"missing keys: {sorted(required - set(row))}")
                malformed = [k for k in row if "." in k]
                self.assertFalse(malformed, f"malformed keys: {malformed}")

    def test_smoke_routes_do_not_error(self):
        routes = STATIC_SMOKE_ROUTES + [
            f"/api/walkthroughs/{self.wt_id}",
            f"/api/walkthroughs/{self.wt_id}/pages",
            f"/api/walkthroughs/{self.wt_id}/embed",
        ]
        for path in routes:
            with self.subTest(route=path):
                try:
                    resp = self._get(path)
                    status = resp.status
                except urllib.error.HTTPError as e:
                    status = e.code
                self.assertLess(status, 500, f"{path} returned {status}")

    def test_settings_say_what_the_catalog_needs_and_nothing_else(self):
        body = json.loads(self._get("/api/settings").read())
        self.assertEqual(set(body), {"ai_configured", "verify_available"})

    def test_billing_routes_are_gone(self):
        """Open source: nothing is sold, so none of the purchase routes exist."""
        for path in ("/plans", "/checkout", "/checkout/done", "/account/plan/cancel",
                     "/api/account/balances"):
            with self.subTest(route=path):
                try:
                    status = self._get(path).status
                except urllib.error.HTTPError as e:
                    status = e.code
                self.assertEqual(status, 404, f"{path} returned {status}")

    def test_unknown_walkthrough_is_not_a_server_error(self):
        """Every route that resolves a walkthrough id must 404 on a bad one.

        Each of these is a separate branch with its own id variable, so they
        are listed individually — collapsing the 404s into a shared helper
        previously introduced an UnboundLocalError on the fallthrough branch
        that only shows up when the id does not exist.
        """
        # Both shapes: an id that could never be a uuid, and a well-formed one
        # that names nothing. The first used to be the only case a slug store
        # had; the second is the one a uuid column can raise on.
        missing = "does-not-exist"
        for path in (f"/player/{missing}",
                     f"/review/{missing}",
                     f"/capture/{missing}/page.html",
                     f"/api/walkthroughs/{missing}",
                     f"/api/walkthroughs/{missing}/pages",
                     f"/api/walkthroughs/{missing}/embed",
                     f"/api/walkthroughs/{missing}/export",
                     f"/api/walkthroughs/{missing}/export-zip",
                     f"/api/walkthroughs/{missing}/export-scorm"):
            with self.subTest(route=path):
                try:
                    status = self._get(path).status
                except urllib.error.HTTPError as e:
                    status = e.code
                self.assertEqual(status, 404, f"{path} returned {status}, expected 404")

    def test_a_well_formed_but_unknown_uuid_is_also_a_404(self):
        """`store.get` returns None for a malformed id and for one that simply
        is not there, and both are the same 404 — deliberately, so the API
        cannot be used to probe for ids."""
        missing = str(uuid.uuid4())
        for path in (f"/player/{missing}", f"/api/walkthroughs/{missing}",
                     f"/review/{missing}"):
            with self.subTest(route=path):
                try:
                    status = self._get(path).status
                except urllib.error.HTTPError as e:
                    status = e.code
                self.assertEqual(status, 404)

    def test_unknown_walkthrough_404s_name_the_id(self):
        """A 404 that does not say which id was missing is not much use."""
        for path in ("/player/nope-1", "/api/walkthroughs/nope-2"):
            with self.subTest(route=path):
                try:
                    body = self._get(path).read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8")
                self.assertIn(path.rsplit("/", 1)[-1], body)


class PublicPageTests(unittest.TestCase):
    """The request gate: Host and cross-site refusals, access-token mode.

    Every route here answers before any
    owner is resolved or any store is consulted — refusals come first, and in
    access-token mode a cookie-less request has no owner — so these keep
    running on a laptop with no database.
    """

    def setUp(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def _status(self, path, method="GET", headers=None, data=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers=headers or {}, data=data)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            r = opener.open(req)
            return r.status, r.headers.get("Location", ""), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", ""), e.read().decode("utf-8")

    def test_a_foreign_host_name_is_refused(self):
        """DNS rebinding: a page on evil.example re-pointed at 127.0.0.1 sends
        its own name in Host. Refused before any owner is resolved."""
        status, _, _ = self._status("/", headers={"Host": "evil.example"})
        self.assertEqual(status, 421)
        status, _, _ = self._status("/api/collections", method="POST",
                                    headers={"Host": "evil.example"}, data=b"{}")
        self.assertEqual(status, 421)

    def test_cross_site_writes_are_refused(self):
        """CSRF: with no login, a page on another site could otherwise POST
        here as the owner. Both browser signals are honoured."""
        for headers in ({"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, body = self._status(
                    "/api/collections", method="POST", data=b"{}",
                    headers={**headers, "Content-Type": "application/json"})
                self.assertEqual(status, 403)
                self.assertIn("Cross-site", body)

    def test_access_token_mode_locks_everything_behind_unlock(self):
        from captcher import auth
        saved = auth.ACCESS_TOKEN
        auth.ACCESS_TOKEN = "correct horse battery staple"
        self.addCleanup(setattr, auth, "ACCESS_TOKEN", saved)
        for path in ("/", "/account", "/branding"):
            with self.subTest(path=path):
                status, location, _ = self._status(path)
                self.assertEqual((status, location), (302, "/unlock"))
        status, _, page = self._status("/unlock")
        self.assertEqual(status, 200)
        self.assertIn('action="/unlock"', page)
        self.assertFalse(re.findall(r"__[A-Z][A-Z0-9_]*__", page))
        status, _, page = self._status("/unlock", method="POST", data=b"token=wrong")
        self.assertEqual(status, 403)
        self.assertIn("not right", page)

    def test_the_right_access_token_sets_the_cookie(self):
        from captcher import auth
        saved = auth.ACCESS_TOKEN
        auth.ACCESS_TOKEN = "correct horse battery staple"
        self.addCleanup(setattr, auth, "ACCESS_TOKEN", saved)
        req = urllib.request.Request(
            self.base + "/unlock", method="POST",
            data=urllib.parse.urlencode({"token": auth.ACCESS_TOKEN}).encode())
        opener = urllib.request.build_opener(_NoRedirect())
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(req)
        self.assertEqual(raised.exception.code, 302)
        cookie = raised.exception.headers.get("Set-Cookie", "")
        self.assertIn(auth.SESSION_COOKIE + "=", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_without_an_access_token_unlock_just_goes_home(self):
        status, location, _ = self._status("/unlock")
        self.assertEqual((status, location), (302, "/"))

    def test_listening_beyond_loopback_needs_an_access_token(self):
        from unittest import mock

        from captcher import auth
        saved = auth.ACCESS_TOKEN
        auth.ACCESS_TOKEN = ""
        self.addCleanup(setattr, auth, "ACCESS_TOKEN", saved)
        with mock.patch.dict(os.environ, {"CAPTCHER_HOST": "0.0.0.0"}), \
                mock.patch("sys.argv", ["captcher"]), \
                self.assertRaises(SystemExit) as raised:
            server.main()
        self.assertIn("CAPTCHER_ACCESS_TOKEN", str(raised.exception))

    def test_old_setting_names_are_named_rather_than_silently_ignored(self):
        """Every setting is CAPTCHER_* now. SHADOWCAPTURE_HOST=0.0.0.0 left in
        a carried-over .env would otherwise just bind to loopback with no
        word about why."""
        from unittest import mock
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SHADOWCAPTURE_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(server.legacy_settings(), [])
            os.environ.update(SHADOWCAPTURE_MODEL="x", SHADOWCAPTURE_HOST="0.0.0.0")
            self.assertEqual(server.legacy_settings(),
                             ["SHADOWCAPTURE_HOST", "SHADOWCAPTURE_MODEL"])

    def test_favicon_is_served_rather_than_redirecting(self):
        """It is in PUBLIC_PATHS deliberately, so the browser's automatic
        request does not bounce through /unlock. It used to 404 because there
        was no icon to serve; the assertion that matters either way is that it
        never redirects."""
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            resp = opener.open(self.base + "/favicon.ico")
            status, ctype, body = resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            status, ctype, body = e.code, "", b""
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/x-icon")
        # multi-size ICO: reserved=0, type=1 (icon), then the image count
        self.assertEqual(body[:4], b"\x00\x00\x01\x00")
        self.assertGreaterEqual(int.from_bytes(body[4:6], "little"), 2)


class PublishedExamplePageTests(unittest.TestCase):
    """`/examples/<slug>` — the one public route that reads a store.

    It is addressed by SLUG, not by id. That is the whole reason the slug
    column outlived the slug-shaped primary key: the example list is written by
    a person in configuration, and a row of uuids would be unmaintainable. So
    these tests name the walkthrough the way production does — through the
    title the slug is derived from — and never compose an id.

    Publication is still two gates, and both are exercised: the slug has to be
    in `server.EXAMPLE_WALKTHROUGH_SLUGS`, and the row has to be embed-enabled.
    """

    def setUp(self):
        self.owner = new_member()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def publish(self, title):
        """A walkthrough published for embedding, with its slug configured as
        an example. Returns the slug."""
        row = fixtures.make_walkthrough(self.owner, title=title)
        store.save_embed_settings(row["id"], {"enabled": True, "intro": True,
                                              "chrome": "full"})
        saved = server.EXAMPLE_WALKTHROUGH_SLUGS
        server.EXAMPLE_WALKTHROUGH_SLUGS = (row["slug"],)
        self.addCleanup(setattr, server, "EXAMPLE_WALKTHROUGH_SLUGS", saved)
        return row["slug"]

    def test_a_published_example_page_is_public(self):
        slug = self.publish("Public Example")
        with urllib.request.urlopen(f"{self.base}/examples/{slug}") as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
        self.assertIn("Public Example", body)
        self.assertIn('id="step-pop"', body)
        self.assertNotIn('title="Interactive walkthrough:', body)

    def test_a_published_example_player_uses_popup_design_without_settings(self):
        slug = self.publish("Public Popup Example")
        with urllib.request.urlopen(f"{self.base}/examples/{slug}") as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
        self.assertIn('id="step-pop"', body)
        self.assertIn('"example": true', body)
        self.assertIn("body.sc-example #btn-settings", body)
        self.assertNotIn('<aside class="panel">', body)

    def test_configuration_alone_does_not_publish_a_private_walkthrough(self):
        """Both gates, not either. Naming a slug in the examples list must not
        make an unpublished walkthrough readable without a login — that is the
        rule `store.is_embeddable` exists to keep in one place, and the reason
        `published_example` checks the list AND the row."""
        row = fixtures.make_walkthrough(self.owner, title="Private Example")
        saved = server.EXAMPLE_WALKTHROUGH_SLUGS
        server.EXAMPLE_WALKTHROUGH_SLUGS = (row["slug"],)
        self.addCleanup(setattr, server, "EXAMPLE_WALKTHROUGH_SLUGS", saved)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/examples/{row['slug']}")
        self.assertEqual(raised.exception.code, 404)

    def test_an_unconfigured_slug_is_a_404_even_when_published(self):
        """The other gate. A walkthrough its owner published for embedding is
        reachable at /embed/<id>; it does not thereby become one of *this*
        server's featured examples."""
        row = fixtures.make_walkthrough(self.owner, title="Published Not Featured")
        store.save_embed_settings(row["id"], {"enabled": True, "intro": True,
                                              "chrome": "full"})
        self.assertEqual(server.EXAMPLE_WALKTHROUGH_SLUGS, ())
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/examples/{row['slug']}")
        self.assertEqual(raised.exception.code, 404)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
