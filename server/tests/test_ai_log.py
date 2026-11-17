"""The AI log: every authoring pass and AI assist request, kept with the
walkthrough and opened from its catalog menu — and the catalog card that, as a
result, says only whether a pass worked.

The status strip under a card used to print every validator warning a pass
produced. Warnings now go to the log and nowhere else on the card; a failed
pass shows the first sentence of its error and a link to the rest.
"""
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from captcher import author
from captcher import diagnostics
from captcher import jobs
from captcher import server
from conftest import new_member, signed_in_client

from . import fixtures

ROOT = Path(__file__).resolve().parents[1] / "captcher"
CATALOG = ROOT / "templates" / "_catalog-script.html"


class LogKindTests(unittest.TestCase):
    def test_every_kind_the_server_writes_is_allowed_by_the_schema(self):
        """KINDS and the table's check constraint are two lists of one thing."""
        schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
        (listed,) = re.findall(r"kind\s+text not null check \(kind in \(([^)]*)\)\)", schema)
        allowed = set(re.findall(r"'(\w+)'", listed))
        self.assertEqual(allowed, set(diagnostics.KINDS))

    def test_the_trail_is_timed_from_the_start_of_the_pass(self):
        t0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        trail = diagnostics._trail(
            [{"seq": 1, "at": t0.timestamp() + 2.04, "phase": "thinking",
              "message": "turn 1 of 8"}], t0)
        self.assertEqual(trail, [{"t": 2.0, "phase": "thinking", "message": "turn 1 of 8"}])


class CatalogStatusTests(unittest.TestCase):
    """The card's strip after a pass."""

    def setUp(self):
        self.js = CATALOG.read_text(encoding="utf-8")

    def test_warnings_never_reach_the_card(self):
        body = self.js[self.js.index("function showAuthoringOutcome"):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("warnings", body)
        self.assertNotIn("<br>Warnings: ", self.js)

    def test_both_ends_of_a_pass_report_the_same_way(self):
        """A pass the page watched and one it re-attached to after a reload
        end in the same strip."""
        self.assertEqual(self.js.count("showAuthoringOutcome(id, job);"), 1)
        self.assertEqual(self.js.count("showAuthoringOutcome(pass.walkthrough_id, job);"), 1)

    def test_a_failure_points_at_the_log(self):
        self.assertIn("data-open-log=", self.js)
        self.assertIn("errorHeadline(job.error)", self.js)

    def test_the_menu_opens_the_log(self):
        self.assertIn('"AI log\\u2026"', self.js)
        self.assertIn('"/logs"', self.js)

    def test_the_log_is_built_as_text_not_markup(self):
        """Its strings came from a model, a validator or the author's typing."""
        start = self.js.index("const logDialog")
        body = self.js[start:self.js.index("eb(\"log-close\")", start)]
        self.assertNotIn("innerHTML", body)


class AiLogTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.intruder_id = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner_id, steps=2)["id"]
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.owner = signed_in_client(self.base, self.owner_id)
        self.intruder = signed_in_client(self.base, self.intruder_id)
        self._orig_run_pass = author.run_pass

    def tearDown(self):
        author.run_pass = self._orig_run_pass
        for job in jobs.active():
            job.finish()
            jobs.release(job)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def author(self, result=None, error=None):
        def fake(walkthrough_id, scenario=None, model=None, progress=None):
            progress("thinking", "turn 1 of 8")
            if error:
                raise error
            return result
        author.run_pass = fake
        req = urllib.request.Request(
            self.base + "/api/author", data=json.dumps({"walkthrough_id": self.wt_id}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return self.owner.open(req).status
        except urllib.error.HTTPError as e:
            return e.code

    def log(self, client=None):
        return json.loads((client or self.owner).open(
            f"{self.base}/api/walkthroughs/{self.wt_id}/logs").read())

    def test_a_finished_pass_is_logged_with_its_warnings(self):
        self.author(result={"walkthrough_id": self.wt_id, "title": "T",
                            "steps_before": 8, "steps_after": 6,
                            "warnings": ["step 'a': hint is long"]})
        [row] = self.log()
        self.assertEqual(row["kind"], "author")
        e = row["entry"]
        self.assertEqual(e["outcome"], "authored")
        self.assertEqual(e["result"]["warnings"], ["step 'a': hint is long"])
        self.assertIn("turn 1 of 8", [t["message"] for t in e["trail"]])

    def test_a_failed_pass_is_logged_in_full_without_its_traceback(self):
        self.assertEqual(self.author(error=RuntimeError("boom")), 500)
        [row] = self.log()
        self.assertEqual(row["entry"]["outcome"], "failed")
        self.assertIn("boom", row["entry"]["error"]["message"])
        self.assertNotIn("traceback", row["entry"]["error"])
        # …which is still stored, for whoever reads the table directly
        [stored] = diagnostics.entries(self.wt_id, "author")
        self.assertIn("boom", stored["entry"]["error"]["traceback"])

    def test_an_ai_edit_is_shown_without_its_diffs(self):
        diagnostics.record(self.wt_id, self.owner_id, "ai_edit", {
            "request": {"instruction": "rename", "current_step": 0,
                        "selection": {"selector": "#x", "html": "<b>big</b>"}},
            "outcome": "saved", "result": {"summary": "Renamed."},
            "changes": {"definition_diff": "x" * 1000,
                        "pages": [{"page": "step-001/page.html", "diff": "y" * 1000}]}})
        [row] = self.log()
        self.assertEqual(row["kind"], "ai_edit")
        self.assertNotIn("changes", row["entry"])
        self.assertEqual(row["entry"]["pages_rewritten"], ["step-001/page.html"])
        self.assertNotIn("html", row["entry"]["request"]["selection"])

    def test_newest_first_and_capture_logs_left_out(self):
        diagnostics.record(self.wt_id, self.owner_id, "capture", {"entries": []})
        diagnostics.record(self.wt_id, self.owner_id, "ai_edit", {"outcome": "failed"})
        time.sleep(0.01)
        diagnostics.record(self.wt_id, self.owner_id, "ai_edit", {"outcome": "saved"})
        rows = self.log()
        self.assertEqual([r["entry"]["outcome"] for r in rows], ["saved", "failed"])

    def test_another_account_cannot_read_it(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.log(self.intruder)
        self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
