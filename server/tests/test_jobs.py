"""The job registry, and the endpoints built on it.

Two halves. The first exercises jobs.py directly — exclusion, cancellation,
the deadline, the event log — because those are the properties the HTTP layer
assumes. The second drives the real server: that a background pass returns a
job id instead of blocking, that its progress arrives as events, that a cancel
unwinds it at the next boundary, and that a save or a delete can no longer
interleave with a pass (the bug the old check-then-release AUTHORING set had).
"""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from captcher import author
from captcher import jobs
from captcher import server
from captcher import store
from conftest import new_member, signed_in_client

from . import fixtures


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = jobs.Registry()

    def test_a_second_claim_on_the_same_walkthrough_is_refused(self):
        first = self.reg.claim("w", "author")
        with self.assertRaises(jobs.JobBusy) as raised:
            self.reg.claim("w", "ai-edit")
        # the holder travels with the exception — the 409 names what to wait for
        self.assertIs(raised.exception.job, first)

    def test_exclusion_is_per_walkthrough_not_per_kind(self):
        """The old set keyed render checks as `render-check:<id>` and the
        authoring pass as `<id>`, so the two could run at once on the same
        walkthrough — and the pass reads the sidecar the check writes."""
        self.reg.claim("w", "author")
        with self.assertRaises(jobs.JobBusy):
            self.reg.claim("w", "render-check")

    def test_different_walkthroughs_do_not_block_each_other(self):
        self.reg.claim("a", "author")
        self.reg.claim("b", "author")   # must not raise

    def test_release_frees_the_walkthrough(self):
        job = self.reg.claim("w", "author")
        job.finish({"ok": True})
        self.reg.release(job)
        self.reg.claim("w", "author")   # must not raise

    def test_a_finished_job_stays_readable_by_id_after_release(self):
        """A client whose stream dropped comes back for the result."""
        job = self.reg.claim("w", "author")
        job.finish({"steps_after": 6})
        self.reg.release(job)
        self.assertEqual(self.reg.get(job.id).result, {"steps_after": 6})
        self.assertIsNone(self.reg.active_for("w"))

    def test_finished_jobs_are_swept_after_retention(self):
        job = self.reg.claim("w", "author")
        job.finish()
        self.reg.release(job)
        job.finished_at = time.time() - jobs.RETENTION_SECONDS - 1
        self.reg.claim("other", "author")   # any claim sweeps
        self.assertIsNone(self.reg.get(job.id))


class JobTests(unittest.TestCase):
    def test_progress_raises_once_cancelled(self):
        job = jobs.Job("w", "author")
        job.progress("thinking", "turn 1")      # fine
        self.assertTrue(job.cancel())
        with self.assertRaises(jobs.Cancelled):
            job.progress("thinking", "turn 2")

    def test_cancelling_a_finished_job_reports_that_it_was_too_late(self):
        job = jobs.Job("w", "author")
        job.finish({"ok": True})
        self.assertFalse(job.cancel())

    def test_the_deadline_unwinds_the_pass(self):
        job = jobs.Job("w", "author", timeout=0.01)
        time.sleep(0.02)
        with self.assertRaises(jobs.Cancelled) as raised:
            job.progress("thinking", "turn 2")
        self.assertIn("timed out", str(raised.exception))

    def test_events_carry_increasing_seq_and_replay_from_a_cursor(self):
        job = jobs.Job("w", "author")
        job.progress("digest", "3 pages")
        job.progress("tool", "reading step-002/page.html")
        events, state = job.events_since(0, timeout=0)
        self.assertEqual([e["phase"] for e in events],
                         ["author", "digest", "tool"])
        self.assertEqual(state, jobs.RUNNING)
        later, _ = job.events_since(events[1]["seq"], timeout=0)
        self.assertEqual([e["phase"] for e in later], ["tool"])

    def test_events_since_returns_immediately_once_terminal(self):
        job = jobs.Job("w", "author")
        job.fail("nope")
        events, state = job.events_since(0, timeout=5)   # must not block
        self.assertEqual(state, jobs.ERROR)
        self.assertTrue(events)

    def test_a_waiting_reader_wakes_on_the_next_event(self):
        job = jobs.Job("w", "author")
        seen = []

        def reader():
            seen.extend(job.events_since(job._seq, timeout=5)[0])

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)
        job.progress("tool", "submitting")
        t.join(5)
        self.assertEqual([e["message"] for e in seen], ["submitting"])


class JobEndpointTests(unittest.TestCase):
    """The HTTP surface, against the real server with author.run_pass stubbed."""

    def setUp(self):
        self.owner_id = new_member()
        self.intruder_id = new_member()
        self.row = fixtures.make_walkthrough(self.owner_id, steps=2)
        self.wt_id = self.row["id"]

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

    def post(self, client, path, obj=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj or {}).encode(),
            headers={"Content-Type": "application/json"})
        return client.open(req)

    def get(self, client, path):
        return json.loads(client.open(self.base + path).read())

    def stub_pass(self, gate=None, error=None, emit=()):
        """Replace author.run_pass with one that reports progress, optionally
        blocks on `gate`, and honours cancellation the way the real loop does
        — by calling progress() at its boundaries."""
        def fake(walkthrough_id, scenario=None, model=None, progress=None):
            progress = progress or (lambda *a, **k: None)
            for phase, message in emit:
                progress(phase, message)
            if gate is not None:
                while not gate.wait(0.02):
                    progress("thinking", "still going")
            if error:
                raise error
            return {"walkthrough_id": walkthrough_id, "steps_before": 8,
                    "steps_after": 6, "warnings": []}
        author.run_pass = fake

    # ---- background mode ------------------------------------------------

    def test_background_author_returns_a_job_id_without_blocking(self):
        gate = threading.Event()
        self.stub_pass(gate=gate)
        r = self.post(self.owner, "/api/author?background=1",
                      {"walkthrough_id": self.wt_id})
        self.assertEqual(r.status, 202)
        body = json.loads(r.read())
        self.assertEqual(body["kind"], "author")

        # the pass is still running, and visible
        active = self.get(self.owner, "/api/jobs")
        self.assertEqual([j["job_id"] for j in active], [body["job_id"]])

        gate.set()
        job = jobs.get(body["job_id"])
        for _ in range(200):
            if job.finished:
                break
            time.sleep(0.02)
        self.assertEqual(job.state, jobs.DONE)
        self.assertEqual(job.result["steps_after"], 6)

    def test_synchronous_mode_still_returns_the_finished_result(self):
        """The default shape is unchanged — three front ends depend on it."""
        self.stub_pass()
        body = json.loads(self.post(self.owner, "/api/author",
                                    {"walkthrough_id": self.wt_id}).read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["steps_after"], 6)

    def test_a_failed_pass_still_releases_the_walkthrough(self):
        self.stub_pass(error=author.AuthorError("nope"))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post(self.owner, "/api/author", {"walkthrough_id": self.wt_id})
        self.assertEqual(raised.exception.code, 422)
        self.assertIsNone(jobs.active_for(self.wt_id))

    # ---- cancellation ---------------------------------------------------

    def test_cancel_unwinds_the_pass_at_its_next_boundary(self):
        gate = threading.Event()
        self.stub_pass(gate=gate)
        body = json.loads(self.post(self.owner, "/api/author?background=1",
                                    {"walkthrough_id": self.wt_id}).read())
        job = jobs.get(body["job_id"])
        self.addCleanup(gate.set)

        cancelled = json.loads(
            self.post(self.owner, f"/api/jobs/{job.id}/cancel").read())
        self.assertTrue(cancelled["cancel_requested"])
        for _ in range(200):
            if job.finished:
                break
            time.sleep(0.02)
        self.assertEqual(job.state, jobs.CANCELLED)
        # and the walkthrough is free again
        self.assertIsNone(jobs.active_for(self.wt_id))

    def test_cancelling_a_finished_job_is_not_an_error(self):
        self.stub_pass()
        body = json.loads(self.post(self.owner, "/api/author",
                                    {"walkthrough_id": self.wt_id}).read())
        r = json.loads(self.post(self.owner, f"/api/jobs/{body['job_id']}/cancel").read())
        self.assertTrue(r["already_finished"])

    def test_another_account_can_neither_see_nor_cancel_the_job(self):
        gate = threading.Event()
        self.stub_pass(gate=gate)
        self.addCleanup(gate.set)
        body = json.loads(self.post(self.owner, "/api/author?background=1",
                                    {"walkthrough_id": self.wt_id}).read())
        self.assertEqual(self.get(self.intruder, "/api/jobs"), [])
        for path in (f"/api/jobs/{body['job_id']}",):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.intruder.open(self.base + path)
            self.assertEqual(raised.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post(self.intruder, f"/api/jobs/{body['job_id']}/cancel")
        self.assertEqual(raised.exception.code, 404)

    # ---- streaming ------------------------------------------------------

    def test_the_event_stream_replays_progress_and_ends_with_a_done_event(self):
        self.stub_pass(emit=[("digest", "3 pages"), ("tool", "reading a page")])
        body = json.loads(self.post(self.owner, "/api/author?background=1",
                                    {"walkthrough_id": self.wt_id}).read())
        job = jobs.get(body["job_id"])
        for _ in range(200):
            if job.finished:
                break
            time.sleep(0.02)

        raw = self.owner.open(
            self.base + f"/api/jobs/{job.id}/events").read().decode()
        self.assertIn("event: progress", raw)
        self.assertIn("3 pages", raw)
        self.assertIn("reading a page", raw)
        self.assertIn("event: done", raw)
        # the terminal event carries the result the POST would have returned
        done = json.loads(raw.rsplit("event: done\ndata: ", 1)[1].strip())
        self.assertEqual(done["state"], jobs.DONE)
        self.assertEqual(done["result"]["steps_after"], 6)

    # ---- the guard gaps -------------------------------------------------

    def test_a_save_cannot_interleave_with_a_running_pass(self):
        held = jobs.claim(self.wt_id, "author", owner=self.owner_id)
        self.addCleanup(lambda: (held.finish(), jobs.release(held)))
        definition = store.get(self.wt_id)["definition"]
        req = urllib.request.Request(
            self.base + f"/api/walkthroughs/{self.wt_id}", method="PUT",
            data=json.dumps(definition).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.owner.open(req)
        self.assertEqual(raised.exception.code, 409)

    def test_a_delete_cannot_interleave_with_a_running_pass(self):
        held = jobs.claim(self.wt_id, "author", owner=self.owner_id)
        self.addCleanup(lambda: (held.finish(), jobs.release(held)))
        req = urllib.request.Request(
            self.base + f"/api/walkthroughs/{self.wt_id}", method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.owner.open(req)
        self.assertEqual(raised.exception.code, 409)
        self.assertIsNotNone(store.get(self.wt_id), "the row survived the refusal")

    def test_the_409_names_which_kind_of_pass_is_holding_it(self):
        held = jobs.claim(self.wt_id, "render-check", owner=self.owner_id)
        self.addCleanup(lambda: (held.finish(), jobs.release(held)))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post(self.owner, "/api/author", {"walkthrough_id": self.wt_id})
        body = json.loads(raised.exception.read())
        self.assertEqual(raised.exception.code, 409)
        self.assertIn("render check", body["error"].lower())
        self.assertEqual(body["kind"], "render-check")


if __name__ == "__main__":
    unittest.main()
