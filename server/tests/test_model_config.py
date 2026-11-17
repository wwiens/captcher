"""Model selection and the loop's model-facing knobs (no database needed).

Pins the model migration (Sonnet 5.5 default): the default model, which models get the
server-side refusal fallback, the tool loop's own token budget, and what the
loop does when a fallback model answers a turn.
"""
import copy
import os
import unittest
from unittest import mock

from captcher import author

FAKE_ENV = {"ANTHROPIC_API_KEY": "sk-test"}


def tool_use(name, inp, tu_id="tu_1"):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": inp}


def reply(*blocks, stop_reason="tool_use", **extra):
    out = {"content": list(blocks), "stop_reason": stop_reason,
           "usage": {"input_tokens": 1, "output_tokens": 1}}
    out.update(extra)
    return out


def run_loop(turns, model="claude-opus-5-5", handlers=None, tools=None):
    log, remaining = [], list(turns)

    def fake_send(params):
        import copy
        log.append(copy.deepcopy(params))
        return remaining.pop(0)(params)

    handlers = handlers or {"submit": lambda inp: ("ok", inp)}
    with mock.patch.dict(os.environ, FAKE_ENV), \
            mock.patch.object(author, "_send_request", fake_send):
        out = author.call_claude_agentic(
            "sys", "user", model, tools=tools or [], tool_handlers=handlers,
            effort="high")
    return out, log


class DefaultModel(unittest.TestCase):
    def test_default_is_sonnet_5_5(self):
        self.assertEqual(author.DEFAULT_MODEL, "claude-sonnet-5-5")

    def test_env_override_still_wins(self):
        with mock.patch.dict(os.environ, {"CAPTCHER_MODEL": "claude-opus-5"}):
            self.assertEqual(author._model_from_env(), "claude-opus-5")
        with mock.patch.dict(os.environ, {"CAPTCHER_MODEL": "  "}):
            self.assertEqual(author._model_from_env(), "claude-sonnet-5-5")


class FallbackSupport(unittest.TestCase):
    def test_listed_models_and_their_snapshots(self):
        for m in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5",
                  "claude-fable-5", "claude-opus-5-5-20260901"):
            with self.subTest(model=m):
                self.assertTrue(author.supports_fallbacks(m))

    def test_unlisted_models_and_lookalikes(self):
        for m in ("claude-sonnet-5", "claude-sonnet-5-50", "claude-opus-50", "claude-haiku-5", "", None):
            with self.subTest(model=m):
                self.assertFalse(author.supports_fallbacks(m))

    def test_loop_attaches_fallbacks_for_opus_5_5(self):
        _, log = run_loop([lambda p: reply(tool_use("submit", {"x": 1}))])
        self.assertEqual(log[0]["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", log[0]["betas"])

    def test_loop_attaches_fallbacks_for_sonnet_5_5(self):
        """Sonnet 5.5 takes only the "default" form, which is all we send."""
        _, log = run_loop([lambda p: reply(tool_use("submit", {"x": 1}))],
                          model="claude-sonnet-5-5")
        self.assertEqual(log[0]["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", log[0]["betas"])


class LoopBudget(unittest.TestCase):
    def test_loop_uses_its_own_budget(self):
        _, log = run_loop([lambda p: reply(tool_use("submit", {}))])
        self.assertEqual(log[0]["max_tokens"], author.AGENTIC_MAX_TOKENS)
        self.assertGreaterEqual(author.AGENTIC_MAX_TOKENS, author.MAX_TOKENS)
        self.assertLessEqual(author.AGENTIC_MAX_TOKENS, 128000)  # Sonnet/Opus 5.5 ceiling

    def test_truncation_is_salvageable_and_still_an_author_error(self):
        with self.assertRaises(author.LoopTruncated) as cm:
            run_loop([lambda p: reply(stop_reason="max_tokens")])
        self.assertIsInstance(cm.exception, author.LoopExhausted)
        self.assertIsInstance(cm.exception, author.AuthorError)

    def test_salvage_prefers_a_clean_candidate_over_a_truncation(self):
        exc = author.LoopTruncated("cut off")
        got = author._salvage_or_raise({"clean": "WT", "errors": []}, exc, "pass")
        self.assertEqual(got, "WT")
        with self.assertRaises(author.LoopTruncated) as cm:
            author._salvage_or_raise({"clean": None, "errors": []}, exc, "pass")
        self.assertIs(cm.exception, exc)


class TurnBudget(unittest.TestCase):
    """A run that explores until its turns run out has nothing to salvage, so
    the model is warned before the end and the failure says what happened."""

    READ = {"read_page_html": lambda inp: ("<html>", None)}

    def _explore_forever(self, tools=None):
        turns = [lambda p: reply(tool_use("read_page_html", {"page": "a"}))
                 ] * author.MAX_LOOP_ITERATIONS
        with self.assertRaises(author.LoopExhausted) as cm:
            run_loop(turns, handlers=self.READ, tools=tools)
        return cm.exception

    def _warnings_in(self, params):
        return [b["text"] for m in params["messages"][1:] if m["role"] == "user"
                for b in m["content"]
                if b.get("type") == "text" and "turns left" in b["text"]]

    def test_warned_once_two_turns_before_the_end(self):
        log = []
        turns = [lambda p: (log.append(copy.deepcopy(p)),
                            reply(tool_use("read_page_html", {})))[1]
                 ] * author.MAX_LOOP_ITERATIONS
        with self.assertRaises(author.LoopExhausted):
            run_loop(turns, handlers=self.READ)
        first_warned = author.MAX_LOOP_ITERATIONS - author.LOOP_WARNING_TURNS_LEFT
        for i, params in enumerate(log):
            with self.subTest(request=i + 1):
                self.assertEqual(len(self._warnings_in(params)),
                                 0 if i < first_warned else 1)
        # the warning rides after the tool results, never between them
        warned = log[first_warned]["messages"][-1]["content"]
        self.assertEqual(warned[-1]["type"], "text")
        self.assertTrue(all(b["type"] == "tool_result" for b in warned[:-1]))

    def test_warning_names_the_loops_own_exits(self):
        text = author._turns_left_warning(
            [author.VALIDATE_TOOL, author.SUBMIT_EDIT_TOOL, author.ANSWER_TOOL])
        self.assertIn("submit_edit", text)
        self.assertIn("answer_author", text)
        text = author._turns_left_warning(
            [author.VALIDATE_TOOL, author.SUBMIT_WALKTHROUGH_TOOL])
        self.assertIn("submit_walkthrough", text)
        self.assertNotIn("answer_author", text)

    def test_exhausted_without_checking_says_so(self):
        exc = self._explore_forever()
        self.assertEqual(exc.tools_called,
                         {"read_page_html": author.MAX_LOOP_ITERATIONS})
        with self.assertRaises(author.LoopExhausted) as cm:
            author._salvage_or_raise({"clean": None, "errors": []}, exc, "edit")
        msg = str(cm.exception)
        self.assertIn("never submitted anything to check", msg)
        self.assertIn(f"{author.MAX_LOOP_ITERATIONS} page reads", msg)
        self.assertNotIn("validated", msg)

    def test_exhausted_after_checking_keeps_the_validation_message(self):
        exc = author.LoopExhausted("x", tools_called={"validate_walkthrough": 1})
        with self.assertRaises(author.LoopExhausted) as cm:
            author._salvage_or_raise({"clean": None, "errors": []}, exc, "edit")
        self.assertIn("never produced a candidate that validated", str(cm.exception))


class FallbackPinning(unittest.TestCase):
    def test_loop_continues_on_the_model_that_answered(self):
        """Thinking blocks are tied to the model that wrote them and go back
        verbatim, so after a fallback serves a turn the loop stays on it."""
        served = reply(tool_use("read", {}), model="claude-sonnet-5")
        served["usage"]["iterations"] = [{"type": "fallback_message"}]
        turns = [lambda p: served,
                 lambda p: reply(tool_use("submit", {"done": True}, "tu_2"))]
        handlers = {"read": lambda inp: ("page", None),
                    "submit": lambda inp: ("ok", inp)}
        out, log = run_loop(turns, handlers=handlers)
        self.assertEqual(out, {"done": True})
        self.assertEqual(log[0]["model"], "claude-opus-5-5")
        self.assertEqual(log[1]["model"], "claude-sonnet-5")
        self.assertNotIn("fallbacks", log[1])   # sonnet-5 does not take it

    def test_no_fallback_no_switch(self):
        turns = [lambda p: reply(tool_use("read", {}), model="claude-opus-5-5"),
                 lambda p: reply(tool_use("submit", {}, "tu_2"))]
        handlers = {"read": lambda inp: ("page", None),
                    "submit": lambda inp: ("ok", inp)}
        _, log = run_loop(turns, handlers=handlers)
        self.assertEqual(log[1]["model"], "claude-opus-5-5")


class EffortKnobs(unittest.TestCase):
    def test_edit_effort_defaults_to_module_effort(self):
        if not os.environ.get("CAPTCHER_EDIT_EFFORT"):
            self.assertEqual(author.EDIT_EFFORT, author.EFFORT)


class WarningsReview(unittest.TestCase):
    def test_render_check_warnings_are_not_reviewed(self):
        ws = ["step-001/page.html: the render check found this page broken — …",
              "step 2 (x): hint is empty"]
        self.assertEqual(author._reviewable_warnings(ws), [ws[1]])
        self.assertEqual(author._reviewable_warnings(None), [])


if __name__ == "__main__":
    unittest.main()
