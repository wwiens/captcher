"""AI assist: the edit loop's second exit (answering a question) and the
editor tab that drives it.

The loop tests script the model at `author._send_request`, so no network and
no API key; the static tests read the player's files, because the tab spans
three of them (player.html, player.js, bridge.js) and none works alone.
"""
import re
import unittest
from pathlib import Path

from captcher import author

from . import agentic

STATIC = Path(__file__).resolve().parents[1] / "captcher" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


class AnswerToolTests(unittest.TestCase):
    def _loop(self, turns, tools):
        handlers = {
            "submit_edit": lambda inp: ('{"accepted": true}', ("edited",)),
            "answer_author": lambda inp: ('{"accepted": true}',
                                          {"answer": inp["answer"],
                                           "suggested_change": inp.get("suggested_change", "")}),
        }
        with agentic.scripted(turns) as log:
            final = author.call_claude_agentic(
                "system", "user", "claude-test", tools=tools,
                tool_handlers=handlers, effort="low")
        return final, log

    def test_an_answer_ends_the_loop_with_the_answer(self):
        final, _ = self._loop(
            [lambda p: agentic.reply(agentic.tool_use(
                "answer_author", {"answer": "No, the options are not in the capture.",
                                  "suggested_change": "Add them"}))],
            [author.SUBMIT_EDIT_TOOL, author.ANSWER_TOOL])
        self.assertEqual(final, {"answer": "No, the options are not in the capture.",
                                 "suggested_change": "Add them"})

    def test_the_nudge_points_a_narrated_answer_at_the_answer_tool(self):
        """A model that wrote its answer as text must not be told its only way
        out is to submit an edit nobody asked for."""
        _, log = self._loop(
            [lambda p: agentic.reply({"type": "text", "text": "The options are missing."},
                                     stop_reason="end_turn"),
             lambda p: agentic.reply(agentic.tool_use("answer_author", {"answer": "Missing."}))],
            [author.SUBMIT_EDIT_TOOL, author.ANSWER_TOOL])
        nudge = log[1]["messages"][-1]["content"][0]["text"]
        self.assertIn("call the submit", nudge)
        self.assertIn("answer_author", nudge)

    def test_without_the_tool_the_nudge_does_not_mention_it(self):
        _, log = self._loop(
            [lambda p: agentic.reply({"type": "text", "text": "chat"}, stop_reason="end_turn"),
             lambda p: agentic.reply(agentic.tool_use("submit_edit", {"walkthrough": {}}))],
            [author.SUBMIT_EDIT_TOOL])
        self.assertNotIn("answer_author", log[1]["messages"][-1]["content"][0]["text"])

    def test_the_answer_requires_prose(self):
        self.assertEqual(author.ANSWER_TOOL["input_schema"]["required"], ["answer"])

    def test_the_progress_line_reads_as_words(self):
        self.assertEqual(author._describe_tool_call("answer_author", {}), "writing the answer")


class EditPromptTests(unittest.TestCase):
    def test_the_edit_prompt_tells_questions_from_instructions(self):
        prompt = author.EDIT_SYSTEM_PROMPT
        self.assertIn("QUESTIONS:", prompt)
        self.assertIn("`answer_author`", prompt)
        # the screenshot's case: a problem report phrased as a question
        self.assertIn("do we have the dropdown's options from the capture?", prompt)
        self.assertIn("suggested_change", prompt)

    def test_run_edit_can_withhold_the_answer_exit(self):
        """The verify pass's repair turn is an instruction it wrote itself; an
        answer there would be a repair that silently did nothing."""
        import inspect
        from captcher import verify_pass
        self.assertIn("answerable", inspect.signature(author.run_edit).parameters)
        self.assertIn("answerable=False", inspect.getsource(verify_pass.run_verify))


class EditorTabWiringTests(unittest.TestCase):
    """The tab replaced a floating window. These pin the pieces that only
    work together."""

    def test_ai_assist_is_a_rail_tab_not_a_floating_window(self):
        html = _read("player.html")
        self.assertIn('id="tab-ai"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertNotIn("draggable", html)
        css = _read("player.css")
        self.assertNotIn("resize: both", css)

    def test_requests_run_as_background_jobs_the_tab_can_follow_and_stop(self):
        js = _read("player.js")
        self.assertIn("/ai-edit?background=1", js)
        self.assertIn("/events`", js)
        self.assertIn("/cancel`", js)
        # a reload re-attaches rather than offering a Send that would 409
        self.assertIn('j.kind === "ai-edit"', js)

    def test_an_answer_is_shown_as_a_reply_not_applied(self):
        js = _read("player.js")
        self.assertIn('.kind === "answer"', js)
        body = js[js.index("function aiRenderAnswer"):js.index("function aiRenderStopped")]
        self.assertNotIn("adoptWalkthrough", body)

    def test_the_bridge_holds_the_pointed_at_element_and_answers_escape(self):
        bridge = _read("bridge.js")
        self.assertIn('m.cmd === "mark"', bridge)
        self.assertIn("br-pick-mark", bridge)
        self.assertIn('type: "pick-cancelled"', bridge)
        js = _read("player.js")
        self.assertIn('cmd: "mark"', js)
        self.assertIn('m.type === "pick-cancelled"', js)

    def test_the_mark_never_leaks_into_what_the_model_is_told(self):
        """markupExcerpt drops every br-* class; the mark must be one."""
        bridge = _read("bridge.js")
        self.assertTrue(re.search(r'\.br-pick-mark\{', bridge))

    def test_every_class_the_panel_builds_is_styled(self):
        """The thread is built in JS, so a class typo there is an unstyled
        element and nothing else notices."""
        js = _read("player.js")
        css = _read("player.css")
        block = js[js.index("/* ---- AI assist"):js.index("function esc(")]
        built = set()
        for m in re.finditer(r'aiEl\("\w+",\s*"([^"]+)"', block):
            built.update(m.group(1).split())
        built = {c for c in built if c.startswith("ai-")}
        missing = sorted(c for c in built if f".{c}" not in css)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
