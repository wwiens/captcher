"""End-to-end tests for the collections API, with two accounts.

Boots a real server against the throwaway schema conftest.py created.

Ids are uuids now, on both sides of the API — the collection id in the URL and
the walkthrough ids in the body. That is why nothing here composes an id from a
name any more: a test that could still guess `/api/collections/onboarding`
would be testing a route that no longer exists.
"""
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

from captcher import library
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures


class CollectionsApiTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()
        self.bob = new_member()
        self.wt = {name: fixtures.make_walkthrough(self.alice, title=name)["id"]
                   for name in ("alpha", "beta", "gamma")}
        self.bobs_own = fixtures.make_walkthrough(self.bob, title="bobs own")["id"]

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.alice_client = signed_in_client(self.base, self.alice)
        self.bob_client = signed_in_client(self.base, self.bob)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    # ---- helpers -----------------------------------------------------------

    def call(self, client, method, path, body=None):
        """(status, parsed json) — an HTTP error is a result here, not a raise."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            resp = client.open(req)
            return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw}

    def create(self, client, name, description=""):
        status, body = self.call(client, "POST", "/api/collections",
                                 {"name": name, "description": description})
        self.assertEqual(status, 201, body)
        return body["collection"]

    # ---- the happy path ----------------------------------------------------

    def test_create_list_rename_delete_roundtrip(self):
        created = self.create(self.alice_client, "New Support Rep Onboarding",
                              "Week one.")
        self.assertEqual(uuid.UUID(created["id"]).version, 4)
        self.assertEqual(created["slug"], "new-support-rep-onboarding")

        status, body = self.call(self.alice_client, "GET", "/api/collections")
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in body["collections"]],
                         ["New Support Rep Onboarding"])
        self.assertEqual(body["collections"][0]["count"], 0)

        status, body = self.call(self.alice_client, "POST",
                                 f"/api/collections/{created['id']}",
                                 {"name": "Onboarding"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["collection"]["name"], "Onboarding")
        self.assertEqual(body["collection"]["id"], created["id"], "id must be stable")

        status, body = self.call(self.alice_client, "DELETE",
                                 f"/api/collections/{created['id']}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["collections"], [])

    def test_every_mutation_answers_with_the_whole_list(self):
        """The client replaces its state wholesale rather than merging."""
        a = self.create(self.alice_client, "A")
        status, body = self.call(self.alice_client, "POST", "/api/collections",
                                 {"name": "B"})
        self.assertEqual(status, 201)
        self.assertEqual([c["name"] for c in body["collections"]], ["A", "B"])
        self.assertIn(a["id"], {c["id"] for c in body["collections"]})

    def test_items_replace_sets_the_order(self):
        c = self.create(self.alice_client, "Onboarding")
        status, body = self.call(self.alice_client, "POST",
                                 f"/api/collections/{c['id']}/items",
                                 {"walkthroughs": [self.wt["gamma"], self.wt["alpha"]]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["collection"]["walkthroughs"],
                         [self.wt["gamma"], self.wt["alpha"]])

        status, body = self.call(self.alice_client, "POST",
                                 f"/api/collections/{c['id']}/items",
                                 {"walkthroughs": [self.wt["alpha"], self.wt["gamma"]]})
        self.assertEqual(body["collection"]["walkthroughs"],
                         [self.wt["alpha"], self.wt["gamma"]])

    def test_membership_sets_every_collection_in_one_call(self):
        a = self.create(self.alice_client, "A")
        b = self.create(self.alice_client, "B")
        status, body = self.call(
            self.alice_client, "POST",
            f"/api/walkthroughs/{self.wt['beta']}/collections",
            {"collections": [a["id"], b["id"]]})
        self.assertEqual(status, 200, body)
        by_id = {c["id"]: c for c in body["collections"]}
        self.assertEqual(by_id[a["id"]]["walkthroughs"], [self.wt["beta"]])
        self.assertEqual(by_id[b["id"]]["walkthroughs"], [self.wt["beta"]])

        status, body = self.call(
            self.alice_client, "POST",
            f"/api/walkthroughs/{self.wt['beta']}/collections",
            {"collections": [b["id"]]})
        by_id = {c["id"]: c for c in body["collections"]}
        self.assertEqual(by_id[a["id"]]["walkthroughs"], [])
        self.assertEqual(by_id[b["id"]]["walkthroughs"], [self.wt["beta"]])

    def test_a_walkthrough_may_sit_in_two_collections(self):
        a = self.create(self.alice_client, "A")
        b = self.create(self.alice_client, "B")
        self.call(self.alice_client, "POST",
                  f"/api/walkthroughs/{self.wt['alpha']}/collections",
                  {"collections": [a["id"], b["id"]]})
        _, body = self.call(self.alice_client, "GET", "/api/collections")
        self.assertEqual(sum(c["count"] for c in body["collections"]), 2)

    # ---- tenancy -----------------------------------------------------------

    def test_collections_are_account_private(self):
        self.create(self.alice_client, "Alice only")
        status, body = self.call(self.bob_client, "GET", "/api/collections")
        self.assertEqual(status, 200)
        self.assertEqual(body["collections"], [])

    def test_another_account_cannot_touch_your_collection(self):
        c = self.create(self.alice_client, "Alice only")
        for method, path, payload in (
            ("POST", f"/api/collections/{c['id']}", {"name": "hijacked"}),
            ("POST", f"/api/collections/{c['id']}/items", {"walkthroughs": []}),
            ("DELETE", f"/api/collections/{c['id']}", None),
        ):
            with self.subTest(method=method, path=path):
                status, _ = self.call(self.bob_client, method, path, payload)
                self.assertEqual(status, 404)
        _, body = self.call(self.alice_client, "GET", "/api/collections")
        self.assertEqual(body["collections"][0]["name"], "Alice only")

    def test_membership_cannot_reference_another_accounts_walkthrough(self):
        """Dropped silently — a 404 naming the id would confirm it exists."""
        c = self.create(self.alice_client, "A")
        status, body = self.call(self.alice_client, "POST",
                                 f"/api/collections/{c['id']}/items",
                                 {"walkthroughs": [self.wt["alpha"], self.bobs_own]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["collection"]["walkthroughs"], [self.wt["alpha"]])
        self.assertNotIn(self.bobs_own, json.dumps(body))

    def test_filing_another_accounts_walkthrough_is_a_404(self):
        c = self.create(self.alice_client, "A")
        status, _ = self.call(self.alice_client, "POST",
                              f"/api/walkthroughs/{self.bobs_own}/collections",
                              {"collections": [c["id"]]})
        self.assertEqual(status, 404)

    def test_a_cookie_less_request_is_the_local_owner_not_another_member(self):
        """No login: a browser with no cookie is the local owner. What it
        creates is the local owner's, and never shows up for another member."""
        plain = urllib.request.build_opener()
        status, _ = self.call(plain, "POST", "/api/collections", {"name": "Local"})
        self.assertEqual(status, 201)
        _, body = self.call(self.alice_client, "GET", "/api/collections")
        self.assertNotIn("Local", [c["name"] for c in body["collections"]])

    # ---- errors ------------------------------------------------------------

    def test_an_unknown_collection_404s_and_names_the_id(self):
        missing = str(uuid.uuid4())
        status, body = self.call(self.alice_client, "POST",
                                 f"/api/collections/{missing}", {"name": "x"})
        self.assertEqual(status, 404)
        self.assertIn("collection", body["error"].lower())

    def test_an_empty_name_is_a_400(self):
        status, body = self.call(self.alice_client, "POST", "/api/collections",
                                 {"name": "   "})
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"].lower())

    def test_the_collection_cap_is_a_409(self):
        for i in range(library.MAX_COLLECTIONS):
            self.call(self.alice_client, "POST", "/api/collections", {"name": f"C{i}"})
        status, body = self.call(self.alice_client, "POST", "/api/collections",
                                 {"name": "one too many"})
        self.assertEqual(status, 409, body)

    def test_a_non_object_body_is_a_400(self):
        status, _ = self.call(self.alice_client, "POST", "/api/collections",
                              ["not", "a", "dict"])
        self.assertEqual(status, 400)

    # ---- integrity ---------------------------------------------------------

    def test_deleting_a_walkthrough_prunes_it_from_collections(self):
        """The prune reaches the database, not just the response. It is the
        foreign key's cascade rather than a sweep the delete path has to
        remember, which is the whole reason membership became a table."""
        throwaway = fixtures.make_walkthrough(self.alice, title="throwaway")["id"]
        c = self.create(self.alice_client, "A")
        self.call(self.alice_client, "POST", f"/api/collections/{c['id']}/items",
                  {"walkthroughs": [self.wt["alpha"], throwaway]})
        status, _ = self.call(self.alice_client, "DELETE",
                              f"/api/walkthroughs/{throwaway}")
        self.assertEqual(status, 200)
        stored = library.get_collection(self.alice, c["id"])["walkthroughs"]
        self.assertEqual(stored, [self.wt["alpha"]])

    def test_deleting_a_collection_keeps_its_walkthroughs(self):
        c = self.create(self.alice_client, "A")
        self.call(self.alice_client, "POST", f"/api/collections/{c['id']}/items",
                  {"walkthroughs": [self.wt["alpha"], self.wt["beta"]]})
        self.call(self.alice_client, "DELETE", f"/api/collections/{c['id']}")
        status, body = self.call(self.alice_client, "GET", "/api/walkthroughs")
        self.assertEqual(status, 200)
        self.assertLessEqual({self.wt["alpha"], self.wt["beta"]},
                             {row["id"] for row in body})

    # ---- routing traps -----------------------------------------------------

    def test_put_under_api_walkthroughs_still_reaches_the_editor_save(self):
        """do_PUT takes the WHOLE remainder of the path as a walkthrough id, so
        `PUT /api/walkthroughs/<id>/collections` looks up a walkthrough called
        "<id>/collections" — which cannot exist. That is why every collection
        write is a POST. If someone "tidies" the verbs, this test says no.

        It used to slugify the remainder, which turned the id into
        `alpha-collections`; the id is opaque now, so the miss is cleaner but
        it is still a miss."""
        status, body = self.call(
            self.alice_client, "PUT",
            f"/api/walkthroughs/{self.wt['alpha']}/collections", {"collections": []})
        self.assertEqual(status, 404, body)

    def test_deleting_a_collection_does_not_reach_the_walkthrough_delete(self):
        """do_DELETE's /api/collections/ branch must come first — otherwise a
        collection id would be treated as a walkthrough id. Both are uuids now,
        so a mis-ordered dispatch would no longer even 404: it would delete."""
        c = self.create(self.alice_client, "A")
        self.call(self.alice_client, "DELETE", f"/api/collections/{c['id']}")
        status, body = self.call(self.alice_client, "GET", "/api/walkthroughs")
        self.assertEqual(len({row["id"] for row in body}), 3)
        self.assertIsNotNone(store.get(self.wt["alpha"], self.alice))

    def test_the_walkthrough_summary_contract_is_unchanged(self):
        """Membership is read from /api/collections, never folded into the
        per-walkthrough summary — that would duplicate the source of truth and,
        now, add a join to a query that runs once per catalog load."""
        _, body = self.call(self.alice_client, "GET", "/api/walkthroughs")
        for row in body:
            self.assertNotIn("collections", row)


if __name__ == "__main__":
    unittest.main()
