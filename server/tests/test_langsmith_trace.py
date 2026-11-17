"""LangSmith tracing — opt-in, non-blocking, and never able to break a pass.

Tracing sits in the path of a paid model call that can run for minutes, so the
properties worth pinning are mostly negative ones: off unless asked for, silent
when the endpoint is broken, and incapable of turning a successful authoring
pass into a failed one. The positive half — that a run actually carries the
trace/parent linkage LangSmith needs to draw a tree — is checked against the
recorded request bodies rather than the network.
"""
import json
import os
import threading
import unittest
import urllib.error
from unittest import mock

from captcher import author
from captcher import langsmith_trace

ON = {
    "LANGSMITH_TRACING": "true",
    "LANGSMITH_API_KEY": "lsv2_pt_test",
    "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    "LANGSMITH_PROJECT": "Captcher-test",
}
# The LANGCHAIN_* aliases are read as fallbacks, so a developer machine that
# exports them would otherwise switch tracing on inside the "off" tests.
OFF = dict.fromkeys(
    ["LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_PROJECT",
     "LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_ENDPOINT", "LANGCHAIN_PROJECT"],
    "")


class TraceCase(unittest.TestCase):
    """Records what the worker thread would have sent, instead of sending it."""

    env = ON
    send_effect = None

    def setUp(self):
        self.sent = []
        lock = threading.Lock()

        def fake_send(cfg, method, path, body):
            with lock:
                self.sent.append((method, path, body))
            if self.send_effect is not None:
                raise self.send_effect

        # mock.patch.dict cannot unset, and the LANGCHAIN_* fallbacks have to
        # be genuinely absent for the "off" cases — so pop them by hand and let
        # patch.dict restore the originals on the way out.
        env_patcher = mock.patch.dict(os.environ, {k: v for k, v in self.env.items() if v})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for key, value in self.env.items():
            if not value:
                os.environ.pop(key, None)

        send_patcher = mock.patch.object(langsmith_trace, "_send", fake_send)
        send_patcher.start()
        self.addCleanup(send_patcher.stop)

        langsmith_trace.reset()
        langsmith_trace._warned = False
        self.addCleanup(langsmith_trace.reset)
        # Registered last so it runs first (cleanups are LIFO): the worker
        # thread is a module global that outlives the test, and anything still
        # queued when the stub comes off would go out over the real network.
        self.addCleanup(langsmith_trace.flush, 5.0)

    def drain(self):
        """Wait for the background worker, then return the recorded requests."""
        self.assertTrue(langsmith_trace.flush(5.0), "trace queue did not drain")
        return self.sent

    def posts(self):
        return [b for m, _, b in self.drain() if m == "POST"]

    def patches(self):
        return [(p, b) for m, p, b in self.drain() if m == "PATCH"]


class TestDisabledByDefault(TraceCase):
    env = OFF

    def test_no_requests_and_no_worker(self):
        with langsmith_trace.span("authoring pass", inputs={"a": 1}) as sp:
            sp.set_outputs({"b": 2})
            sp.add_metadata({"c": 3})
            sp.set_error("d")
        self.assertFalse(langsmith_trace.enabled())
        self.assertEqual(self.sent, [])

    def test_key_without_tracing_flag_stays_off(self):
        os.environ["LANGSMITH_API_KEY"] = "lsv2_pt_test"
        self.addCleanup(os.environ.pop, "LANGSMITH_API_KEY", None)
        langsmith_trace.reset()
        self.assertFalse(langsmith_trace.enabled())

    def test_tracing_flag_without_key_stays_off(self):
        """An absent key is 'off', not an error — a missing observability
        backend must never stop someone authoring a walkthrough."""
        os.environ["LANGSMITH_TRACING"] = "true"
        self.addCleanup(os.environ.pop, "LANGSMITH_TRACING", None)
        langsmith_trace.reset()
        self.assertFalse(langsmith_trace.enabled())


class TestRunPayload(TraceCase):
    def test_post_then_patch_with_the_fields_langsmith_needs(self):
        with langsmith_trace.span("authoring pass", inputs={"walkthrough_id": "w1"},
                                  metadata={"model": "claude-sonnet-5"},
                                  tags=["authoring"]) as sp:
            sp.set_outputs({"steps_after": 6})

        methods = [m for m, _, _ in self.drain()]
        self.assertEqual(methods, ["POST", "PATCH"],
                         "the PATCH that closes a run must not overtake its POST")

        post = self.posts()[0]
        self.assertEqual(post["name"], "authoring pass")
        self.assertEqual(post["run_type"], "chain")
        self.assertEqual(post["session_name"], "Captcher-test")
        self.assertEqual(post["inputs"], {"walkthrough_id": "w1"})
        self.assertEqual(post["tags"], ["authoring"])
        self.assertEqual(post["extra"]["metadata"]["model"], "claude-sonnet-5")
        self.assertNotIn("parent_run_id", post)
        # A root run traces itself, and its dotted_order is one segment of
        # "<sortable start stamp>Z<run id>".
        self.assertEqual(post["trace_id"], post["id"])
        self.assertTrue(post["dotted_order"].endswith(post["id"]))
        self.assertRegex(post["dotted_order"], r"^\d{8}T\d{12}Z[0-9a-f-]{36}$")

        path, patch = self.patches()[0]
        self.assertEqual(path, f"/runs/{post['id']}")
        self.assertEqual(patch["outputs"], {"steps_after": 6})
        self.assertIn("end_time", patch)
        self.assertNotIn("error", patch)

    def test_body_is_json_serialisable(self):
        """Bodies carry whatever a call site passed — including Paths and sets
        in a result dict — so the encoder must not be the thing that fails."""
        from pathlib import Path
        with langsmith_trace.span("x", inputs={"p": Path("/tmp/a")}) as sp:
            sp.set_outputs({"warnings": ("a", "b")})
        for _, _, body in self.drain():
            json.dumps(body, default=str)

    def test_nested_span_carries_parent_linkage(self):
        with langsmith_trace.span("authoring pass") as parent:
            del parent
            with langsmith_trace.span("anthropic.messages", run_type="llm") as child:
                del child

        posts = {p["name"]: p for p in self.posts()}
        parent, child = posts["authoring pass"], posts["anthropic.messages"]
        self.assertEqual(child["parent_run_id"], parent["id"])
        self.assertEqual(child["trace_id"], parent["trace_id"])
        self.assertTrue(child["dotted_order"].startswith(parent["dotted_order"] + "."),
                        "a child's dotted_order must extend its parent's")
        self.assertEqual(child["run_type"], "llm")

    def test_exception_is_recorded_and_re_raised(self):
        with self.assertRaises(author.AuthorError):
            with langsmith_trace.span("authoring pass"):
                raise author.AuthorError("AI output failed validation")

        _, patch = self.patches()[0]
        self.assertIn("AuthorError", patch["error"])
        self.assertIn("failed validation", patch["error"])

    def test_strings_are_clipped_to_the_configured_budget(self):
        """Prompts here are captured third-party pages; the whole capture must
        not leave the machine just because tracing is on."""
        os.environ["CAPTCHER_LANGSMITH_MAX_CHARS"] = "50"
        self.addCleanup(os.environ.pop, "CAPTCHER_LANGSMITH_MAX_CHARS", None)
        langsmith_trace.reset()

        html = "x" * 5000
        with langsmith_trace.span("x", inputs=langsmith_trace.chat_inputs("sys", html)):
            pass

        sent = self.posts()[0]["inputs"]["messages"][1]["content"][0]["text"]
        self.assertTrue(sent.startswith("x" * 50))
        self.assertLess(len(sent), 200)
        self.assertIn("4,950 more characters not sent", sent)


class TestFailuresStayContained(TraceCase):
    send_effect = urllib.error.URLError("langsmith unreachable")

    def test_a_broken_endpoint_does_not_break_the_caller(self):
        with langsmith_trace.span("authoring pass") as sp:
            sp.set_outputs({"ok": True})
        self.assertTrue(langsmith_trace.flush(5.0))
        self.assertTrue(self.sent, "the worker should still have attempted the send")

    def test_it_warns_once_not_once_per_span(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(4):
                with langsmith_trace.span("authoring pass"):
                    pass
            langsmith_trace.flush(5.0)
        self.assertEqual(buf.getvalue().count("[langsmith]"), 1)


class TestCallClaudeSpan(TraceCase):
    """The one integration point: author.call_claude, which every model call
    in the project goes through."""

    def _reply(self, usage=None, stop_reason="end_turn"):
        return {
            "content": [{"type": "text", "text": '{"title": "ok"}'}],
            "stop_reason": stop_reason,
            "usage": usage or {"input_tokens": 1200, "output_tokens": 340},
        }

    def test_llm_run_carries_prompts_model_and_token_usage(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
                mock.patch.object(author, "_send_request",
                                  return_value=self._reply()):
            out = author.call_claude("SYSTEM PROMPT", "USER PROMPT", "claude-sonnet-5")
        self.assertEqual(out, '{"title": "ok"}')

        post = self.posts()[0]
        self.assertEqual(post["run_type"], "llm")
        roles = [m["role"] for m in post["inputs"]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(post["inputs"]["messages"][1]["content"][0]["text"], "USER PROMPT")
        # ls_model_name is what LangSmith matches to price a custom LLM run.
        self.assertEqual(post["extra"]["metadata"]["ls_provider"], "anthropic")
        self.assertEqual(post["extra"]["metadata"]["ls_model_name"], "claude-sonnet-5")

        _, patch = self.patches()[0]
        self.assertEqual(patch["outputs"]["usage_metadata"],
                         {"input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540})
        self.assertEqual(patch["extra"]["metadata"]["stop_reason"], "end_turn")

    def test_a_truncated_reply_is_traced_as_an_error(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
                mock.patch.object(author, "_send_request",
                                  return_value=self._reply(stop_reason="max_tokens")):
            with self.assertRaises(author.AuthorError):
                author.call_claude("SYSTEM", "USER", "claude-sonnet-5")

        _, patch = self.patches()[0]
        self.assertIn("max_tokens", patch["error"])
        self.assertEqual(patch["extra"]["metadata"]["stop_reason"], "max_tokens")

    def test_a_missing_api_key_raises_before_any_span_is_opened(self):
        """The guard fires before the request, so it must not leave an empty
        LLM run hanging in the trace."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(author.AuthorError):
                author.call_claude("SYSTEM", "USER", "claude-sonnet-5")
        self.assertEqual(self.drain(), [])


if __name__ == "__main__":
    unittest.main()
