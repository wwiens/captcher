"""Heavy-page pruning for the authoring prompt.

A QuickBooks Online snapshot trims to ~200K chars per page, and dense markup
tokenises at roughly two chars per token — so a 13-page capture reached 1.3M
input tokens and the API rejected the request outright.

prepare_page() prunes such pages further. The risk it carries is precision: it
must strip build-generated class hashes (which selector hardening must never
target anyway) while leaving authored classes and every stable targeting handle
intact. Both directions are pinned here because a silent false positive would
degrade selector quality on every heavy capture with nothing to notice.

The threshold matters just as much: every capture recorded before this existed
trims to under 32K per page, so none of them may see any of this.
"""
import os
import unittest
from unittest import mock

from captcher import author

# Real tokens from a QuickBooks Online capture.
GENERATED = [
    "Typography-body-2-e793b1d",       # css-modules, hex tail
    "Button-button-6d04afb",
    "IconControl-iconControlButton-8b7fde6",
    "Ywjq71DjN5WEOh-yEHfxIw==",        # base64-ish
    "HeaderItem__NotificationWrapper-paluxf-0",   # styled-components
    "Avatar__AvatarSpan-sc-19y5ooy-1",
    "qbss-accessPointContainer--2dtZ-qcz",        # css-modules, -- tail
    "ladnjH", "gpULFc", "dBjEla",      # emotion short hashes
    "txp-capability-tableNameAndSettingsWrapper-6eEjc",   # random 5-6 tail
    "txp-capability-centerFooterButtonsContainer-+9fR9",
]

# Authored classes from the same pages. Losing these costs real targeting.
AUTHORED = [
    "fusion-leftrail--item",
    "fusion-leftrail--item--icon",
    "fusion-leftrail--group",
    "global-header-item-button",
    "headerItem", "widget-wrapper", "flyout-menu-trigger",
    "carousel-chip", "chip-icon", "chip-title",
    "sprite", "spritePadding", "solid-border", "fusionSlide",
    "idsF", "idsTSLink", "idsTSButton", "idsTSIconControl",
    "marketing-ipd-tsa-widgets-global-header-wrapper",
    "erd_scroll_detection_container_animation_active",
    "IndeterminateShort-circularSpinnerCircle-Inner-Secondary",
    "QUICKFILL-BaseQuickfill-type:term-subTypes:none",
]


class ClassClassifier(unittest.TestCase):
    def test_generated_tokens_are_dropped(self):
        for tok in GENERATED:
            with self.subTest(tok=tok):
                self.assertTrue(author.is_generated_class(tok))

    def test_authored_tokens_survive(self):
        for tok in AUTHORED:
            with self.subTest(tok=tok):
                self.assertFalse(author.is_generated_class(tok))


class PruneThreshold(unittest.TestCase):
    def test_light_page_is_untouched(self):
        """Below the threshold prepare_page must equal plain trim_html."""
        html = ('<div class="Typography-body-2-e793b1d card">'
                '<svg viewBox="0 0 8 8"><path d="M1 2 L3 4"/></svg>Hi</div>')
        self.assertLess(len(html), author.PRUNE_ABOVE_CHARS)
        self.assertEqual(author.prepare_page(html), author.trim_html(html))

    def test_heavy_page_is_pruned(self):
        html = ('<div class="Typography-body-2-e793b1d card">x</div>'
                + "<p>filler</p>" * 8000)
        self.assertGreater(len(author.trim_html(html)), author.PRUNE_ABOVE_CHARS)
        out = author.prepare_page(html)
        self.assertNotIn("Typography-body-2-e793b1d", out)
        self.assertIn("card", out)


class SvgGeometry(unittest.TestCase):
    def test_geometry_goes_but_handles_stay(self):
        html = ('<svg role="img" aria-label="Delete" viewBox="0 0 8 8">'
                '<title>Delete</title><path d="M1 2 L3 4 L9 9 Z"/>'
                '<g><circle cx="1" cy="2" r="3"/></g></svg>')
        out = author.strip_svg_geometry(html)
        self.assertIn('aria-label="Delete"', out)
        self.assertIn("<title>Delete</title>", out)
        self.assertNotIn("<path", out)
        self.assertNotIn("<circle", out)

    def test_multiple_svgs_collapse_independently(self):
        html = '<svg><path d="a"/></svg><b>keep</b><svg><path d="b"/></svg>'
        out = author.strip_svg_geometry(html)
        self.assertIn("<b>keep</b>", out)
        self.assertNotIn("<path", out)


class ClassAttrPruning(unittest.TestCase):
    def test_mixed_attribute_keeps_authored_tokens(self):
        html = '<button class="Button-button-6d04afb idsTSButton ladnjH">Go</button>'
        self.assertEqual(author.prune_class_attrs(html),
                         '<button class="idsTSButton">Go</button>')

    def test_all_generated_drops_the_attribute(self):
        html = '<div class="ladnjH gpULFc">x</div>'
        self.assertEqual(author.prune_class_attrs(html), "<div>x</div>")

    def test_unquoted_attribute_is_handled(self):
        """SingleFile emits unquoted values wherever it can."""
        self.assertEqual(author.prune_class_attrs("<div class=ladnjH>x</div>"),
                         "<div>x</div>")
        self.assertEqual(author.prune_class_attrs("<div class=headerItem>x</div>"),
                         "<div class=headerItem>x</div>")


class Preflight(unittest.TestCase):
    def test_under_budget_passes(self):
        author.preflight_prompt_size("sys", "user")  # must not raise

    def test_over_budget_raises_actionable_error(self):
        huge = "x" * (author.INPUT_TOKEN_BUDGET * 2 + 10)
        with self.assertRaises(author.AuthorError) as cm:
            author.preflight_prompt_size("sys", huge)
        self.assertIn("too large", str(cm.exception))
        self.assertIn("fewer capture steps", str(cm.exception))

    def test_estimate_is_pessimistic_for_markup(self):
        """Must not under-count: 2 chars/token, not 4."""
        self.assertEqual(author.estimate_tokens("y" * 1000), 500)

    def test_under_budget_never_calls_the_counter(self):
        """The fast path costs nothing — no network round trip when the
        pessimistic estimate is already inside the budget."""
        with mock.patch.object(author, "_count_tokens",
                               side_effect=AssertionError("counter called")):
            author.preflight_prompt_size("sys", "user")

    def test_exact_count_rescues_a_prose_heavy_prompt(self):
        """len//2 overestimates prose ~2x; the count_tokens rescue converts
        that false refusal into an accepted pass."""
        huge = "x" * (author.INPUT_TOKEN_BUDGET * 2 + 10)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_count_tokens",
                                  return_value=author.INPUT_TOKEN_BUDGET - 1):
            author.preflight_prompt_size("sys", huge)  # must not raise

    def test_genuinely_over_budget_is_still_refused_with_the_real_count(self):
        huge = "x" * (author.INPUT_TOKEN_BUDGET * 2 + 10)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_count_tokens",
                                  return_value=1_234_567):
            with self.assertRaises(author.AuthorError) as cm:
                author.preflight_prompt_size("sys", huge)
        self.assertIn("1,234,567", str(cm.exception))

    def test_a_broken_counter_falls_back_to_the_estimate(self):
        """A dead counter endpoint must never block (or admit) a pass — the
        pessimistic estimate stays the guard."""
        huge = "x" * (author.INPUT_TOKEN_BUDGET * 2 + 10)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_count_tokens", return_value=None):
            with self.assertRaises(author.AuthorError):
                author.preflight_prompt_size("sys", huge)


class OutputBudget(unittest.TestCase):
    """`max_tokens` caps thinking AND response text together.

    The pass sent no `thinking` and no `output_config` for a long time, so
    claude-sonnet-5 ran adaptive thinking at the default "high" effort inside
    the same 16000-token budget as the JSON, and a 16-step QuickBooks
    walkthrough truncated mid-emission. Nothing in the payload said so — the
    defaults are invisible — which is exactly why they are pinned here.
    """

    def _payload(self):
        """The request params call_claude() builds, without touching the network."""
        sent = {}

        def fake_send(params):
            sent["body"] = params
            return {"content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_send_request", fake_send):
            author.call_claude("sys", "user", "claude-sonnet-5")
        return sent["body"]

    def test_thinking_and_effort_are_explicit(self):
        body = self._payload()
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"], {"effort": author.EFFORT})

    def test_no_parameters_the_current_models_reject(self):
        """budget_tokens and the sampling params are 400s on sonnet-5/opus-5."""
        body = self._payload()
        for dead in ("budget_tokens", "temperature", "top_p", "top_k"):
            self.assertNotIn(dead, body)
        self.assertNotIn("budget_tokens", body.get("thinking", {}))

    def test_budget_clears_the_measured_output_rate(self):
        """Authored output runs ~950 chars/step ≈ 320 tokens/step (measured on
        approve-goodwill and test-after-phase-8). A 20-step walkthrough is ~7K
        tokens of JSON; the rest of the budget is thinking headroom."""
        twenty_step_json = 20 * 320
        self.assertGreater(author.MAX_TOKENS, twenty_step_json * 4)

    def test_truncation_error_points_at_effort_not_just_max_tokens(self):
        """Raising MAX_TOKENS was the wrong first move — thinking was the
        consumer. The message has to say so or the next person repeats it."""
        payload = {"stop_reason": "max_tokens", "content": [],
                   "usage": {"input_tokens": 630000, "output_tokens": author.MAX_TOKENS}}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_send_request", lambda params: payload):
            with self.assertRaises(author.AuthorError) as cm:
                author.call_claude("sys", "user", "claude-sonnet-5")
        msg = str(cm.exception)
        self.assertIn("thinking", msg.lower())
        self.assertIn("EFFORT", msg)
        self.assertIn(str(author.MAX_TOKENS), msg)


class PerCallKnobs(unittest.TestCase):
    """effort / max_tokens / output_schema are per-call-site knobs.

    The authoring pass, the AI edit, the render-check verdict and brand
    inference have very different shapes; a single module-global effort was
    serving all four. These pin that the keyword knobs actually reach the
    request params — and that the vision wrapper forwards them.
    """

    def _payload(self, fn="call_claude", user="user",
                 model="claude-sonnet-5", **kwargs):
        sent = {}

        def fake_send(params):
            sent["body"] = params
            return {"content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
                mock.patch.object(author, "_send_request", fake_send):
            getattr(author, fn)("sys", user, model, **kwargs)
        return sent["body"]

    def test_effort_and_max_tokens_are_per_call(self):
        body = self._payload(effort="low", max_tokens=4000)
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["max_tokens"], 4000)

    def test_output_schema_switches_on_structured_outputs(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}},
                  "required": ["x"], "additionalProperties": False}
        body = self._payload(output_schema=schema)
        self.assertEqual(body["output_config"]["format"],
                         {"type": "json_schema", "schema": schema})
        # …and without the kwarg there is no format key at all.
        self.assertNotIn("format", self._payload()["output_config"])

    def test_vision_wrapper_forwards_the_knobs(self):
        content = [{"type": "text", "text": "compare"},
                   {"type": "image", "source": {"type": "base64",
                                                "media_type": "image/png",
                                                "data": "aGk="}}]
        schema = {"type": "object", "properties": {},
                  "additionalProperties": False}
        body = self._payload(fn="call_claude_vision", user=content,
                             effort="low", max_tokens=4000,
                             output_schema=schema)
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["max_tokens"], 4000)
        self.assertEqual(body["output_config"]["format"]["schema"], schema)
        self.assertEqual(body["messages"][0]["content"], content)

    def test_authoring_pass_thinks_harder_than_the_default(self):
        """run_pass runs at AUTHOR_EFFORT ("high" unless overridden) — the
        deliberate cost increase from the SDK-upgrade pass."""
        self.assertEqual(author.AUTHOR_EFFORT, "high")

    def test_system_prompt_is_a_cached_block(self):
        """The system prompt is a stable prefix — sent as a text block with a
        cache_control marker so repeat calls read it at ~0.1x input price."""
        self.assertEqual(self._payload()["system"],
                         [{"type": "text", "text": "sys",
                           "cache_control": {"type": "ephemeral"}}])

    def test_refusal_fallback_attaches_only_on_supporting_models(self):
        """`fallbacks: "default"` re-runs a classifier decline on Anthropic's
        recommended fallback — but the parameter is a 400 on models that
        don't support it, so it is model-guarded."""
        body = self._payload(model="claude-opus-5")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["betas"], ["server-side-fallback-2026-07-01"])
        sonnet = self._payload()  # claude-sonnet-5
        self.assertNotIn("fallbacks", sonnet)
        self.assertNotIn("betas", sonnet)


if __name__ == "__main__":
    unittest.main()
