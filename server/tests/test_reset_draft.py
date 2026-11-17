"""POST /api/walkthroughs/<id>/reset-draft — undo the authoring pass.

The pass refuses a walkthrough that has been through it, and a history restore
brings back an old definition without clearing `authored_at`, so re-testing the
pass on one capture used to mean recording it again. The store half
(store.reset_to_draft) is pinned in test_store.py; this is the route, and the
card menu that reaches it.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from captcher import author
from captcher import jobs
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures

ROOT = Path(__file__).resolve().parents[1] / "captcher"


class ResetDraftEndpointTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.intruder_id = new_member()
        self.row = fixtures.make_walkthrough(self.owner_id, steps=2)
        self.wt_id = self.row["id"]
        store.save_with_history(self.wt_id, dict(self.row["definition"], title="Authored"))
        store.mark_authored(self.wt_id)

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.owner = signed_in_client(self.base, self.owner_id)
        self.intruder = signed_in_client(self.base, self.intruder_id)

    def tearDown(self):
        held = jobs.active_for(self.wt_id)
        if held is not None:
            held.finish()
            jobs.release(held)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def reset(self, client):
        req = urllib.request.Request(
            f"{self.base}/api/walkthroughs/{self.wt_id}/reset-draft", data=b"",
            method="POST")
        return client.open(req)

    def test_it_returns_the_walkthrough_to_its_draft_and_the_queue(self):
        body = json.loads(self.reset(self.owner).read())
        self.assertTrue(body["ok"])
        row = store.get(self.wt_id)
        self.assertEqual(row["definition"], row["draft"])
        self.assertEqual(store.authoring_state(row), {})
        self.assertIsNone(jobs.active_for(self.wt_id))

    def test_the_passs_refusal_is_lifted(self):
        """The refusal the pass raises is the whole reason this exists."""
        with self.assertRaises(author.AuthorError):
            author.run_pass(self.wt_id)
        self.reset(self.owner)
        self.assertEqual(store.authoring_state(store.get(self.wt_id)), {})

    def test_another_accounts_walkthrough_is_a_plain_404(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.reset(self.intruder)
        self.assertEqual(raised.exception.code, 404)
        self.assertTrue(store.authoring_state(store.get(self.wt_id)))

    def test_it_waits_for_a_pass_in_flight(self):
        held = jobs.claim(self.wt_id, "author", owner=self.owner_id)
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.reset(self.owner)
            self.assertEqual(raised.exception.code, 409)
            self.assertEqual(json.loads(raised.exception.read())["kind"], "author")
        finally:
            held.finish()
            jobs.release(held)
        self.assertTrue(store.authoring_state(store.get(self.wt_id)))


class ResetDraftWiringTests(unittest.TestCase):
    def test_the_card_menu_offers_it_only_on_authored_walkthroughs(self):
        js = (ROOT / "templates" / "_catalog-script.html").read_text(encoding="utf-8")
        item = js.find('"Reset to draft')
        self.assertGreater(item, 0)
        guard = js.rfind("if (!ctx.queued)", 0, item)
        self.assertGreater(guard, js.rfind("menuItem(dropdown, ICON_REVIEW", 0, item))
        self.assertIn('"/reset-draft"', js)

    def test_the_passs_refusal_points_at_it(self):
        src = (ROOT / "author.py").read_text(encoding="utf-8")
        self.assertIn("Reset to draft", src)
        self.assertNotIn("restore one of those first", src)


if __name__ == "__main__":
    unittest.main()
