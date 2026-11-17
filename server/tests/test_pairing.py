"""Extension pairing: codes, per-device tokens, and the two repos' contract.

The flow is specified in PAIRING.md. What is asserted here is everything that,
if it broke, would break silently:

  - a pairing code is single-use, short-lived, and useless without a session to
    have minted it;
  - pairing a second browser does NOT revoke the first (the bug the per-device
    model exists to fix, and one with no error message when it happens);
  - a capture token resolves to the same *type* of identity a session does,
    because a uuid that comes back as a UUID object on one path and a str on
    the other breaks ownership silently on exactly one of them;
  - the message names the page and the extension agree on still match, since a
    rename on one side fails by silence rather than by error.

The legacy-token tests are gone. They pinned the migration from a single
`api_token` object inside users.json to the per-device list — both the file and
that shape are gone.
"""
import http.cookiejar
import json
import re
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from captcher import auth
from captcher import db
from captcher import server
from conftest import new_member, signed_in_client

import os
from captcher import paths

EXTENSION_DIR = paths.extension_checkout(Path(__file__).resolve().parents[1] / "captcher", os.environ.get("CAPTCHER_EXTENSION_DIR", ""))
PAIR_JS = Path(__file__).resolve().parents[1] / "captcher" / "static" / "pair.js"
APP_ROOT_TEMPLATES = Path(__file__).resolve().parents[1] / "captcher" / "templates"


def _post(opener, url, payload=None):
    """POST JSON, returning (status, parsed body). A 4xx is a result here, not
    an exception — most of these tests are about which one comes back."""
    data = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _delete(opener, url):
    request = urllib.request.Request(url, method="DELETE")
    try:
        with opener.open(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class PairingTokenTests(unittest.TestCase):
    """auth.py alone — no server, no HTTP."""

    def setUp(self):
        self.member = new_member()

    def test_pairing_a_second_device_keeps_the_first(self):
        first, first_id = auth.generate_api_token(self.member, "Laptop")
        second, second_id = auth.generate_api_token(self.member, "Desktop")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(auth.find_member_by_token(first), self.member)
        self.assertEqual(auth.find_member_by_token(second), self.member)
        self.assertEqual(len(auth.list_devices(self.member)), 2)

    def test_a_capture_token_resolves_to_the_same_type_a_session_does(self):
        """`members.id` is a uuid column, and psycopg maps uuid to uuid.UUID.
        A session id is decoded out of a cookie and is therefore always a str,
        so a missed `::text` cast here would break ownership on capture uploads
        while browser sessions kept working — with no type error to point at
        it. See the convention note in db.py."""
        token, _ = auth.generate_api_token(self.member, "Laptop")
        self.assertIsInstance(auth.find_member_by_token(token), str)
        self.assertEqual(auth.find_member_by_token(token),
                         auth.verify_session(auth.make_session(self.member)))

    def test_revoking_one_device_leaves_the_others(self):
        keep, _ = auth.generate_api_token(self.member, "Laptop")
        drop, drop_id = auth.generate_api_token(self.member, "Desktop")
        self.assertTrue(auth.revoke_device(self.member, drop_id))
        self.assertIsNone(auth.find_member_by_token(drop))
        self.assertEqual(auth.find_member_by_token(keep), self.member)

    def test_revoking_an_unknown_device_is_false_not_an_error(self):
        self.assertFalse(auth.revoke_device(self.member, "no-such-device"))
        self.assertFalse(auth.revoke_device(self.member, ""))

    def test_a_device_belongs_to_one_member_and_cannot_be_revoked_by_another(self):
        """Filtered on member_id as well as device id, so knowing another
        member's device id is not enough to revoke it."""
        token, device_id = auth.generate_api_token(self.member, "Laptop")
        stranger = new_member()
        self.assertFalse(auth.revoke_device(stranger, device_id))
        self.assertEqual(auth.find_member_by_token(token), self.member)

    def test_a_member_that_does_not_exist_gets_no_token(self):
        self.assertEqual(auth.generate_api_token(str(uuid.uuid4()), "Laptop"), (None, None))

    def test_deleting_the_member_stops_an_existing_token(self):
        """`find_member_by_token` re-checks the member on every use, so the
        token stops working on its next upload rather than at some renewal."""
        token, _ = auth.generate_api_token(self.member, "Laptop")
        db.execute("delete from members where id = %s", (self.member,))
        auth.forget_member(self.member)
        self.assertIsNone(auth.find_member_by_token(token))

    def test_a_token_that_was_never_issued_resolves_to_nobody(self):
        self.assertIsNone(auth.find_member_by_token(auth.API_TOKEN_PREFIX + "nope"))
        self.assertIsNone(auth.find_member_by_token("wrong-prefix"))
        self.assertIsNone(auth.find_member_by_token(""))
        self.assertIsNone(auth.find_member_by_token(None))

    def test_a_pairing_code_is_single_use(self):
        code, _ = auth.issue_pairing_code(self.member)
        member_id, token, _ = auth.redeem_pairing_code(code, "Chrome")
        self.assertEqual(member_id, self.member)
        self.assertTrue(token.startswith(auth.API_TOKEN_PREFIX))
        self.assertEqual(auth.redeem_pairing_code(code, "Chrome"), (None, None, None))

    def test_an_expired_pairing_code_is_refused(self):
        saved = auth.PAIRING_CODE_TTL
        try:
            auth.PAIRING_CODE_TTL = -1
            code, _ = auth.issue_pairing_code(self.member)
            self.assertEqual(auth.redeem_pairing_code(code), (None, None, None))
        finally:
            auth.PAIRING_CODE_TTL = saved

    def test_a_made_up_code_is_refused(self):
        self.assertEqual(auth.redeem_pairing_code(auth.PAIRING_CODE_PREFIX + "nonsense"),
                         (None, None, None))
        self.assertEqual(auth.redeem_pairing_code("rc_wrong_prefix"), (None, None, None))
        self.assertEqual(auth.redeem_pairing_code(""), (None, None, None))

    def test_device_labels_are_bounded(self):
        _, device_id = auth.generate_api_token(self.member, "x" * 500)
        device = next(d for d in auth.list_devices(self.member)
                      if d["id"] == device_id)
        self.assertEqual(len(device["label"]), auth.MAX_LABEL)

    def test_a_blank_label_gets_a_default(self):
        _, device_id = auth.generate_api_token(self.member, "   ")
        device = next(d for d in auth.list_devices(self.member)
                      if d["id"] == device_id)
        self.assertEqual(device["label"], "Browser extension")

    def test_the_device_list_never_carries_a_token_digest(self):
        """Dropped by the SELECT rather than by the caller, so no template can
        print it by accident."""
        auth.generate_api_token(self.member, "Laptop")
        for device in auth.list_devices(self.member):
            self.assertNotIn("token_hash", device)

    def test_using_a_token_records_last_used(self):
        token, device_id = auth.generate_api_token(self.member, "Laptop")
        before = next(d for d in auth.list_devices(self.member)
                      if d["id"] == device_id)
        self.assertIsNone(before["last_used_at"])
        auth.find_member_by_token(token)
        after = next(d for d in auth.list_devices(self.member)
                     if d["id"] == device_id)
        self.assertIsNotNone(after["last_used_at"])


class PairingEndpointTests(unittest.TestCase):
    """The HTTP surface, including who is allowed to call what.

    Per-test rather than per-class setup: conftest truncates between tests, so
    a member minted in setUpClass would not survive to the second one.
    """

    def setUp(self):
        self.member = new_member()
        self.email = auth.get_member(self.member)["email"]
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.client = signed_in_client(self.base, self.member)
        # No cookie jar at all: this is the extension's posture.
        self.anonymous = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def test_ping_is_public_and_says_nothing_private(self):
        with self.anonymous.open(self.base + "/api/extension/ping") as response:
            body = json.loads(response.read())
        self.assertTrue(body["captcher"])
        self.assertEqual(body["pairing"], server.PAIRING_API_VERSION)
        self.assertEqual(body["maxUploadBytes"], server.MAX_UPLOAD)
        self.assertEqual(set(body), {"captcher", "pairing", "maxUploadBytes"})

    def test_pair_then_claim_yields_a_working_token(self):
        status, offer = _post(self.client, self.base + "/api/extension/pair")
        self.assertEqual(status, 201)
        self.assertGreater(offer["expires_in"], 0)

        status, claim = _post(self.anonymous, self.base + "/api/extension/claim",
                              {"code": offer["code"], "label": "Chrome on macOS"})
        self.assertEqual(status, 201)
        self.assertEqual(claim["email"], self.email)

        # The token works where it has to: an upload's Authorization header.
        request = urllib.request.Request(
            self.base + "/api/whoami",
            headers={"Authorization": f"Bearer {claim['token']}"})
        with self.anonymous.open(request) as response:
            self.assertEqual(json.loads(response.read())["email"], self.email)

    def test_claiming_needs_no_session_but_a_second_claim_fails(self):
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        status, _ = _post(self.anonymous, self.base + "/api/extension/claim",
                          {"code": offer["code"]})
        self.assertEqual(status, 201)
        status, body = _post(self.anonymous, self.base + "/api/extension/claim",
                             {"code": offer["code"]})
        self.assertEqual(status, 400)
        self.assertIn("expired or was already used", body["error"])

    def test_a_bad_code_and_a_used_code_answer_identically(self):
        """Distinguishing them tells a guesser which half of the space to
        search, and no legitimate user behaves differently in the two cases."""
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        _post(self.anonymous, self.base + "/api/extension/claim", {"code": offer["code"]})
        _, used = _post(self.anonymous, self.base + "/api/extension/claim",
                        {"code": offer["code"]})
        _, never = _post(self.anonymous, self.base + "/api/extension/claim",
                         {"code": auth.PAIRING_CODE_PREFIX + "never-issued"})
        self.assertEqual(used, never)

    def test_pairing_requires_a_session_not_a_capture_token(self):
        """A leaked capture token must not be able to mint its own
        replacements — revoking the device would not stop it."""
        token, _ = auth.generate_api_token(self.member, "Stolen")
        request = urllib.request.Request(
            self.base + "/api/extension/pair", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.anonymous.open(request)
        self.assertEqual(raised.exception.code, 401)

    def test_pairing_a_locked_server_is_rejected(self):
        """With an access token set, a browser that has not unlocked the
        server has no owner and cannot pair."""
        saved = auth.ACCESS_TOKEN
        auth.ACCESS_TOKEN = "correct horse battery staple"
        self.addCleanup(setattr, auth, "ACCESS_TOKEN", saved)
        status, _ = _post(self.anonymous, self.base + "/api/extension/pair")
        self.assertEqual(status, 401)

    def test_a_cookie_less_browser_pairs_as_the_local_owner(self):
        status, offer = _post(self.anonymous, self.base + "/api/extension/pair")
        self.assertEqual(status, 201)
        _, claim = _post(self.anonymous, self.base + "/api/extension/claim",
                         {"code": offer["code"]})
        self.assertEqual(claim["email"], auth.LOCAL_OWNER_LABEL)
        self.assertEqual(auth.find_member_by_token(claim["token"]), auth.LOCAL_OWNER_ID)

    def test_revoking_a_device_stops_its_token(self):
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        _, claim = _post(self.anonymous, self.base + "/api/extension/claim",
                         {"code": offer["code"]})
        status, _ = _delete(
            self.client, f"{self.base}/api/extension/devices/{claim['device_id']}")
        self.assertEqual(status, 200)
        request = urllib.request.Request(
            self.base + "/api/whoami",
            headers={"Authorization": f"Bearer {claim['token']}"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.anonymous.open(request)
        self.assertEqual(raised.exception.code, 401)

    def test_revoking_needs_a_session(self):
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        _, claim = _post(self.anonymous, self.base + "/api/extension/claim",
                         {"code": offer["code"]})
        request = urllib.request.Request(
            f"{self.base}/api/extension/devices/{claim['device_id']}",
            method="DELETE", headers={"Authorization": f"Bearer {claim['token']}"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.anonymous.open(request)
        self.assertEqual(raised.exception.code, 401)

    def test_the_manual_token_route_adds_rather_than_replaces(self):
        """It used to revoke the member's only other token, silently."""
        _, first = _post(self.client, self.base + "/api/token", {"label": "One"})
        _, second = _post(self.client, self.base + "/api/token", {"label": "Two"})
        self.assertEqual(auth.find_member_by_token(first["token"]), self.member)
        self.assertEqual(auth.find_member_by_token(second["token"]), self.member)

    def test_the_account_page_lists_devices_and_never_prints_a_hash(self):
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        _post(self.anonymous, self.base + "/api/extension/claim",
              {"code": offer["code"], "label": "Chrome on macOS"})
        with self.client.open(self.base + "/account") as response:
            html = response.read().decode()
        self.assertIn("Chrome on macOS", html)
        self.assertNotIn(auth._hash_api_token("anything")[:8], html)
        for device in auth.list_devices(self.member):
            self.assertIn(device["id"], html)

    def test_a_device_label_is_escaped_on_the_account_page(self):
        """The label is chosen by whoever redeemed the code. auth bounds its
        length and nothing else, so escaping is the renderer's job."""
        _, offer = _post(self.client, self.base + "/api/extension/pair")
        _post(self.anonymous, self.base + "/api/extension/claim",
              {"code": offer["code"], "label": "<script>alert(1)</script>"})
        with self.client.open(self.base + "/account") as response:
            html = response.read().decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class PairingWireContractTests(unittest.TestCase):
    """The names both repos have to agree on.

    Pairing fails by SILENCE when these drift: the offer is simply never heard,
    which on the page is indistinguishable from "no extension installed". There
    is no error to notice, which is exactly why this is a test.
    """

    def _extension_file(self, name):
        path = EXTENSION_DIR / name
        if not path.is_file():
            self.skipTest(f"{name} not checked out beside the server repo")
        return path.read_text(encoding="utf-8")

    def _names(self, text):
        """Every `<something>:pair-<verb>` message name in a file.

        Deliberately not anchored to a brand. The property is that both ends
        say the SAME thing, and matching only `captcher:` would report a
        renamed page and an unrenamed extension as "the extension names no
        messages" rather than as the disagreement it is.
        """
        return set(re.findall(r'"([a-z-]+:pair-[a-z]+)"', text))

    def test_the_page_and_the_extension_use_the_same_two_names(self):
        """The whole wire is two messages: the extension asks, the page answers.
        Both ends must name both, identically.

        Neither side raises anything when this drifts: the request is simply
        never heard, so on a clean install Connect appears to do nothing at
        all. That silence is why the pair is compared here rather than left to
        a person to notice — the two halves were renamed in separate passes
        once, and this test is what caught it.
        """
        page = self._names(PAIR_JS.read_text(encoding="utf-8"))
        connect = self._names(self._extension_file("connect.js"))
        self.assertEqual(len(page), 2, f"the page names {page}")
        self.assertEqual(page, connect,
                         "the app and the extension name different messages, "
                         "so pairing fails by silence")

    def test_the_announcer_takes_no_part_in_pairing(self):
        """announce.js is registered only for the origin already in storage, so
        it cannot help on a clean install — which is exactly when pairing is
        needed. It was briefly a second pairing path; keeping it one-way is what
        stops there being two paths, one of which silently cannot work."""
        announce = self._extension_file("announce.js")
        self.assertEqual(self._names(announce), set())
        self.assertNotIn("pair-claim", announce)

    def test_only_the_extension_initiates(self):
        """A page-side Connect button would be a second entry point and the one
        that fails from cold. The page answers; it never asks.

        Each side declares who it *is* and filters on who the other claims to
        be, so the two `source` strings are a contract in the same way the
        message names are — and fail the same silent way.

        Fails the same silent way as the message names above, and for the same
        reason: a sender nobody recognises is indistinguishable from a message
        nobody sent.
        """
        page = PAIR_JS.read_text(encoding="utf-8")
        connect = self._extension_file("connect.js")
        senders = re.compile(r'source: "([a-z-]+)"|source !== "([a-z-]+)"')
        page_names = {a or b for a, b in senders.findall(page)}
        connect_names = {a or b for a, b in senders.findall(connect)}
        self.assertEqual(page_names, connect_names,
                         "the two ends disagree about who is talking")

    def test_the_page_reports_why_it_has_no_code(self):
        """A signed-out page looks exactly like a page with no listener unless
        it says so, and "sign in" is the actual next step."""
        self.assertIn("error", PAIR_JS.read_text(encoding="utf-8"))
        self.assertIn("message.error", self._extension_file("connect.js"))

    def test_pair_js_is_served_from_the_shared_toolbar(self):
        """It is on the toolbar so that every signed-in page can answer, rather
        than the extension having to send people to one particular page."""
        toolbar = (APP_ROOT_TEMPLATES / "_toolbar.html").read_text(encoding="utf-8")
        self.assertIn("/static/pair.js", toolbar)

    def test_the_extension_claims_against_the_route_the_server_serves(self):
        background = self._extension_file("background.js")
        self.assertIn("/api/extension/claim", background)

    def test_the_panel_pings_the_route_the_server_serves(self):
        panel = self._extension_file("sidepanel/panel.js")
        self.assertIn("/api/extension/ping", panel)

    def test_the_panel_offers_no_manual_entry(self):
        """One option, deliberately. A URL field and a token field alongside a
        Connect button is three ways to do one thing, two of which are worse."""
        markup = self._extension_file("sidepanel/panel.html")
        self.assertNotIn('id="server-url"', markup)
        self.assertNotIn('id="server-token"', markup)

    def test_the_account_page_does_not_offer_to_connect_or_mint(self):
        """Connecting happens in the side panel — the only surface that can see
        which tab is open. A second button here would be the one that cannot
        work from a clean install."""
        account = (APP_ROOT_TEMPLATES / "account.html").read_text(encoding="utf-8")
        self.assertNotIn('id="connect"', account)
        self.assertNotIn('id="gen"', account)
        # …but revoking still lives here, which is the point of the page.
        self.assertIn("device-revoke", account)

    def test_the_presence_handshake_uses_the_names_both_ends_agree_on(self):
        """The marker attribute and the presence event are a contract between
        announce.js and the library page that decides whether to show the
        install prompt. They are machine strings, not copy: both ends must be
        changed in the same commit or the prompt shows forever for everyone.

        Compared end-to-end rather than pinned to a literal, because pinning
        the literal is what let the app be renamed on its own.

        The failure mode here is the mildest-looking and the most confusing to
        diagnose: a marker the page never reads leaves the "install the
        extension" prompt showing for someone who has already installed it, and
        nothing anywhere reports a problem.
        """
        announce = self._extension_file("announce.js")
        panel = (APP_ROOT_TEMPLATES / "_extension-panel.html").read_text(
            encoding="utf-8")
        marker = re.search(r'const MARKER = "([A-Za-z]+)"', announce).group(1)
        event = re.search(r'const PRESENT_EVENT = "([a-z:-]+)"', announce).group(1)
        self.assertIn(f"dataset.{marker}", panel,
                      "the page reads a marker attribute the extension never sets")
        self.assertIn(f'"{event}"', panel,
                      "the page listens for an event the extension never fires")


if __name__ == "__main__":
    unittest.main()
