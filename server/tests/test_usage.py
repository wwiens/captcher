"""AI usage: every billed attempt recorded, attributed and priced.

The pricing and parsing tests need nothing. The rest script the model at
`author._send_request`, as the loop tests do, and read `ai_usage` back: the
point of the table is that a call made deep inside a pass lands with the
operation, walkthrough, member and job that started it, without any of the
code in between knowing.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from captcher import author
from captcher import db
from captcher import jobs
from captcher import langsmith_trace
from captcher import render_check
from captcher import server
from captcher import store
from captcher import usage
from conftest import new_member, signed_in_client

from . import agentic, fixtures


def response(model="claude-opus-5-5", text="ok", **usage_fields):
    """A finished Messages API response, as `_send_request` returns it."""
    return {"id": "msg_1", "model": model, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 20, **usage_fields}}


class PriceTests(unittest.TestCase):
    def test_the_longest_matching_id_wins(self):
        self.assertEqual(usage.price_for("claude-opus-5-5")[0], 4.00)
        self.assertEqual(usage.price_for("claude-opus-5")[0], 5.00)

    def test_a_dated_snapshot_prices_as_its_model(self):
        self.assertEqual(usage.price_for("claude-opus-5-20260601"),
                         usage.PRICES["claude-opus-5"])
        self.assertEqual(usage.price_for("anthropic.claude-sonnet-5-5"),
                         usage.PRICES["claude-sonnet-5-5"])

    def test_an_unknown_model_has_no_price_rather_than_a_wrong_one(self):
        self.assertIsNone(usage.price_for("claude-test"))
        self.assertIsNone(usage.cost("claude-test", {"input_tokens": 10}))

    def test_each_token_kind_is_priced_at_its_own_rate(self):
        million = dict.fromkeys(usage.TOKEN_FIELDS, 1_000_000)
        # $4 input + $5 5m write + $8 1h write + $0.20 read + $20 output
        self.assertAlmostEqual(usage.cost("claude-opus-5-5", million), 37.20)
        self.assertAlmostEqual(
            usage.cost("claude-opus-5-5", {"cache_read_tokens": 1_000_000}), 0.20)


class AttemptTests(unittest.TestCase):
    def test_a_plain_response_is_one_attempt_at_the_served_model(self):
        got = usage.attempts(response(cache_read_input_tokens=500,
                                      cache_creation_input_tokens=50),
                             "claude-opus-5-5")
        self.assertEqual(got, [("claude-opus-5-5", {
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 500,
            "cache_write_5m_tokens": 50, "cache_write_1h_tokens": 0})])

    def test_the_cache_write_breakdown_separates_one_hour_writes(self):
        (_, tokens), = usage.attempts(response(
            cache_creation_input_tokens=70,
            cache_creation={"ephemeral_5m_input_tokens": 30,
                            "ephemeral_1h_input_tokens": 40}), "m")
        self.assertEqual((tokens["cache_write_5m_tokens"],
                          tokens["cache_write_1h_tokens"]), (30, 40))

    def test_a_rescued_call_is_one_attempt_per_model(self):
        data = response(model="claude-opus-5")
        data["usage"]["iterations"] = [
            {"type": "message", "input_tokens": 900, "output_tokens": 5},
            {"type": "fallback_message", "model": "claude-opus-5",
             "input_tokens": 900, "output_tokens": 300},
        ]
        got = usage.attempts(data, "claude-opus-5-5")
        self.assertEqual([m for m, _ in got], ["claude-opus-5-5", "claude-opus-5"])
        self.assertEqual(got[1][1]["output_tokens"], 300)

    def test_garbage_counts_are_zero_not_errors(self):
        (_, tokens), = usage.attempts({"usage": {"input_tokens": None,
                                                 "output_tokens": "7"}}, "m")
        self.assertEqual(tokens["input_tokens"] + tokens["output_tokens"], 0)


class RecordIsBestEffortTests(unittest.TestCase):
    def test_nothing_is_written_without_a_database(self):
        with mock.patch.object(db, "configured", return_value=False), \
                mock.patch.object(db, "execute") as execute:
            usage.record(response(), "claude-opus-5-5")
        execute.assert_not_called()

    def test_a_failed_write_never_reaches_the_paid_call(self):
        with mock.patch.object(db, "configured", return_value=True), \
                mock.patch.object(db, "execute", side_effect=RuntimeError("down")), \
                mock.patch.dict(agentic.os.environ, agentic.FAKE_ENV), \
                mock.patch.object(author, "_send_request", return_value=response(text="hi")):
            self.assertEqual(author.call_claude("s", "u", "claude-opus-5-5"), "hi")


class LangSmithUsageTests(unittest.TestCase):
    def test_cached_input_is_counted_and_broken_out(self):
        out = langsmith_trace.chat_outputs("x", {
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 300})
        self.assertEqual(out["usage_metadata"], {
            "input_tokens": 5400, "output_tokens": 20, "total_tokens": 5420,
            "input_token_details": {"cache_read": 5000, "cache_creation": 300}})


def rows(**where):
    clause = " and ".join(f"{k} = %s" for k in where) or "true"
    return db.query(
        f"""select operation, job_id, walkthrough_id,
                   walkthrough_title, member_id, model,
                   requested_model, attempt, call_id,
                   input_tokens, output_tokens, cache_read_tokens
              from ai_usage where {clause} order by id""",
        tuple(where.values()))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.member = new_member()
        self.wt = fixtures.make_walkthrough(self.member, steps=1)

    def call(self, data, model="claude-opus-5-5"):
        with mock.patch.dict(agentic.os.environ, agentic.FAKE_ENV), \
                mock.patch.object(author, "_send_request", return_value=data):
            return author.call_claude("s", "u", model)

    def test_a_call_lands_with_its_attribution(self):
        with usage.attribute("author", walkthrough_id=self.wt["id"],
                             member_id=self.member, job_id="job-1"):
            self.call(response(cache_read_input_tokens=4000))
        (row,) = rows()
        self.assertEqual(row["operation"], "author")
        self.assertEqual(row["job_id"], "job-1")
        self.assertEqual(row["walkthrough_id"], self.wt["id"])
        self.assertEqual(row["walkthrough_title"], self.wt["title"])
        self.assertEqual(row["member_id"], self.member)
        self.assertEqual(row["cache_read_tokens"], 4000)

    def test_a_call_with_no_attribution_is_other(self):
        self.call(response())
        self.assertEqual([r["operation"] for r in rows()], ["other"])

    def test_a_refused_reply_is_still_recorded(self):
        refused = response()
        refused["stop_reason"] = "refusal"
        with self.assertRaises(author.AuthorError):
            self.call(refused)
        self.assertEqual(len(rows()), 1)

    def test_a_rescued_call_is_two_rows_sharing_a_call_id(self):
        data = response(model="claude-opus-5")
        data["usage"]["iterations"] = [
            {"type": "message", "input_tokens": 10, "output_tokens": 1},
            {"type": "fallback_message", "input_tokens": 10, "output_tokens": 9}]
        self.call(data)
        first, second = rows()
        self.assertEqual(first["call_id"], second["call_id"])
        self.assertEqual((first["model"], second["model"]),
                         ("claude-opus-5-5", "claude-opus-5"))
        self.assertEqual((first["attempt"], second["attempt"]), (0, 1))

    def test_spend_outlives_the_walkthrough(self):
        with usage.attribute("author", walkthrough_id=self.wt["id"]):
            self.call(response(input_tokens=1_000_000, output_tokens=0))
        store.delete(self.wt["id"])
        (line,) = usage.report(None)["by_walkthrough"]
        self.assertEqual(line["label"], self.wt["title"])
        self.assertTrue(line["deleted"])
        self.assertAlmostEqual(line["cost"], 4.00)

    def test_relabel_moves_one_jobs_calls(self):
        for job in ("a", "b"):
            with usage.attribute("ai-edit", job_id=job):
                self.call(response())
        usage.relabel("a", "ai-assist")
        self.assertEqual({r["job_id"]: r["operation"] for r in rows()},
                         {"a": "ai-assist", "b": "ai-edit"})

    def test_the_report_prices_and_divides_by_runs(self):
        for job in ("j1", "j2"):
            with usage.attribute("author", job_id=job, member_id=self.member):
                self.call(response(input_tokens=1_000_000, output_tokens=0))
                self.call(response(input_tokens=0, output_tokens=100_000))
        report = usage.report(30)
        self.assertAlmostEqual(report["total"]["cost"], 2 * (4.00 + 2.00))
        (op,) = report["by_operation"]
        self.assertEqual((op["key"], op["jobs"], op["calls"]), ("author", 2, 4))
        self.assertAlmostEqual(op["per_job"], 6.00)
        (member,) = report["by_member"]
        self.assertIn("@", member["label"])


class RenderCheckAttributionTests(unittest.TestCase):
    """The render check judges pages on a thread pool, and a worker thread
    starts with an empty context — so the attribution has to be carried."""

    def test_vision_calls_in_the_pool_keep_the_jobs_attribution(self):
        member = new_member()
        wt_id = fixtures.make_walkthrough(member, steps=2)["id"]
        png = b"\x89PNG\r\n\x1a\n" + bytes(16)
        for folder in ("step-000", "step-001"):
            store.put_capture(wt_id, f"{folder}/screenshot.png", png)

        def renderer(base_url, cookie, walkthrough_id, jobs_):
            for _rel, _vp, out_rel in jobs_:
                store.put_capture(walkthrough_id, out_rel, png)
            return [rel for rel, _, _ in jobs_]

        seen = []

        def vision(*_a, **_kw):
            seen.append(usage.current())
            return json.dumps({"matches": True, "severity": "ok", "differences": []})

        with usage.attribute("render-check", walkthrough_id=wt_id, job_id="rc"), \
                mock.patch.object(render_check, "render_pages", renderer), \
                mock.patch.object(author, "call_claude_vision", vision):
            render_check.run_check(wt_id, "http://x", model="m")
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(s and s["job_id"] == "rc" for s in seen))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.owner_id = new_member()
        self.row = fixtures.make_walkthrough(self.owner_id, steps=2)
        store.mark_authored(self.row["id"])
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.owner = signed_in_client(self.base, self.owner_id)
        self._orig_run_edit = author.run_edit

    def tearDown(self):
        author.run_edit = self._orig_run_edit
        held = jobs.active_for(self.row["id"])
        if held is not None:
            held.finish()
            jobs.release(held)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def ai_edit(self, kind):
        """POST an AI edit whose run_edit makes one real (scripted) model
        call and ends as an edit or an answer."""
        def fake(walkthrough_id, instruction, **_kw):
            author.call_claude("s", "u", "claude-opus-5-5")
            return {"walkthrough_id": walkthrough_id, "kind": kind, "answer": "No.",
                    "steps_before": 2, "steps_after": 2, "pages_changed": 0,
                    "pages_changed_paths": [], "warnings": [], "summary": "",
                    "stamp": None}
        author.run_edit = fake
        req = urllib.request.Request(
            f"{self.base}/api/walkthroughs/{self.row['id']}/ai-edit",
            data=json.dumps({"instruction": "is this right?"}).encode(),
            headers={"Content-Type": "application/json"})
        with mock.patch.dict(agentic.os.environ, agentic.FAKE_ENV), \
                mock.patch.object(author, "_send_request", return_value=response()):
            return json.loads(self.owner.open(req).read())

    def test_an_edit_is_billed_to_its_job_walkthrough_and_member(self):
        result = self.ai_edit("edit")
        (row,) = rows()
        self.assertEqual(row["operation"], "ai-edit")
        self.assertEqual(row["job_id"], result["job_id"])
        self.assertEqual(row["walkthrough_id"], self.row["id"])
        self.assertEqual(row["member_id"], self.owner_id)

    def test_an_edit_that_only_answered_is_ai_assist(self):
        self.ai_edit("answer")
        self.assertEqual([r["operation"] for r in rows()], ["ai-assist"])

    def test_the_spend_page_shows_what_was_spent(self):
        """Single user: the owner reads their own spend; there is no admin."""
        self.ai_edit("edit")
        page = self.owner.open(self.base + "/usage?days=all").read().decode()
        self.assertIn("AI spend", page)
        self.assertIn(self.row["title"], page)
        self.assertIn("AI edit", page)

    def test_an_unknown_period_falls_back_to_the_default(self):
        page = self.owner.open(self.base + "/usage?days=bogus").read().decode()
        self.assertIn('class="chip is-active" aria-current="page" href="/usage?days=30"',
                      page)
