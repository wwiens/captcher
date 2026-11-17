"""Targets inside captured iframes — the HubSpot finding (FIELD-NOTES.md).

Real products render whole surfaces in same-origin iframes; the capture
engine inlines each one as a `srcdoc` attribute. A target that lives in one
carries `frames`: one verified selector per hop from the top document down,
and every consumer must descend identically — digest.match() with
querySelector semantics per hop (FIRST match, no text filter), bridge.js
contextDocFor() the same way, and the recorder's framePath is where the hops
come from at capture time. These tests pin the server side of that chain plus
the serve-time sandbox rewrite the bridge depends on.
"""
import html as html_mod
import unittest

from captcher import export
from captcher import ingest
from captcher.digest import match, page_digest, parse_page


def framed_page(inner, iframe_attrs='id="panel-frame"'):
    """A page whose right-hand panel is an iframe capturing `inner`."""
    doc = "<html><head><title>inner</title></head><body>" + inner + "</body></html>"
    return ("<html><head><title>outer</title></head><body>"
            "<h1>Companies</h1><button id='add'>Add companies</button>"
            f"<iframe {iframe_attrs} srcdoc=\"{html_mod.escape(doc)}\"></iframe>"
            "</body></html>")


FORM = ("<h2>Create Company</h2>"
        "<label for='domain'>Company domain name</label>"
        "<input id='domain' type='text'>"
        "<button class='primary'>Create</button>")


class FrameParsing(unittest.TestCase):
    def test_srcdoc_parses_into_a_tree_on_the_iframe_node(self):
        t = parse_page(framed_page(FORM))
        frame = t.by_tag["iframe"][0]
        self.assertIsNotNone(frame.frame_tree)
        self.assertEqual(len(frame.frame_tree.by_tag["input"]), 1)

    def test_frame_content_is_a_separate_document(self):
        """Selectors must not leak across the boundary: the inner input is
        not reachable from the outer tree without a frames hop."""
        t = parse_page(framed_page(FORM))
        self.assertNotIn("input", t.by_tag)

    def test_an_iframe_without_srcdoc_gets_no_tree(self):
        t = parse_page("<body><iframe id='x' src='a.html'></iframe></body>")
        self.assertIsNone(t.by_tag["iframe"][0].frame_tree)

    def test_an_unparseable_srcdoc_degrades_to_no_tree(self):
        t = parse_page('<body><iframe id="x" srcdoc="   "></iframe></body>')
        self.assertIsNone(t.by_tag["iframe"][0].frame_tree)


class FrameInventory(unittest.TestCase):
    def test_frame_elements_are_inventoried_with_the_hop(self):
        t = parse_page(framed_page(FORM))
        block, entries = page_digest("p", t)
        framed = [e for e in entries if e["target"].get("frames")]
        self.assertEqual({e["tag"] for e in framed}, {"input", "button"})
        for e in framed:
            self.assertEqual(e["target"]["frames"], ["#panel-frame"])
        # the arrow notation is display-only, for the model's benefit
        self.assertIn("#panel-frame >>> ", block)

    def test_frame_outline_text_reaches_the_digest(self):
        block, _ = page_digest("p", parse_page(framed_page(FORM)))
        self.assertIn("Create Company", block)
        self.assertIn("[content of frame #panel-frame]", block)

    def test_a_hidden_frame_is_left_out(self):
        page = framed_page(FORM, iframe_attrs='id="panel-frame" hidden')
        _, entries = page_digest("p", parse_page(page))
        self.assertEqual([e for e in entries if e["target"].get("frames")], [])

    def test_labels_resolve_inside_the_frame_document(self):
        _, entries = page_digest("p", parse_page(framed_page(FORM)))
        inp = next(e for e in entries if e["tag"] == "input")
        self.assertEqual(inp["name"], "Company domain name")

    def test_every_inventoried_target_round_trips_through_match(self):
        t = parse_page(framed_page(FORM))
        _, entries = page_digest("p", t)
        for e in entries:
            hits = match(t, e["target"])
            self.assertIsNotNone(hits, e["target"])
            self.assertEqual(len(hits), 1, e["target"])
            self.assertIs(hits[0], e["node"], e["target"])


class FrameMatching(unittest.TestCase):
    def test_a_missing_frame_is_a_definite_miss(self):
        t = parse_page(framed_page(FORM))
        self.assertEqual(match(t, {"selector": "button",
                                   "frames": ["#no-such-frame"]}), [])

    def test_an_uncaptured_frame_is_the_empty_document_the_player_sees(self):
        t = parse_page("<body><iframe id='x' src='a.html'></iframe></body>")
        self.assertEqual(match(t, {"selector": "button", "frames": ["#x"]}), [])

    def test_a_hop_outside_the_grammar_gives_no_verdict(self):
        t = parse_page(framed_page(FORM))
        self.assertIsNone(match(t, {"selector": "button",
                                    "frames": ["iframe:not([hidden])"]}))

    def test_the_hop_takes_the_first_match_like_querySelector(self):
        """Two frames match the hop; the bridge descends into the FIRST.
        Only the first frame's button exists, so a match against the second
        frame's content must miss."""
        inner_a = "<html><body><button id='a-btn'>A</button></body></html>"
        inner_b = "<html><body><button id='b-btn'>B</button></body></html>"
        page = ("<body>"
                f"<iframe class='panel' srcdoc=\"{html_mod.escape(inner_a)}\"></iframe>"
                f"<iframe class='panel' srcdoc=\"{html_mod.escape(inner_b)}\"></iframe>"
                "</body>")
        t = parse_page(page)
        self.assertEqual(len(match(t, {"selector": "#a-btn",
                                       "frames": ["iframe.panel"]})), 1)
        self.assertEqual(match(t, {"selector": "#b-btn",
                                   "frames": ["iframe.panel"]}), [])


class IngestFramePath(unittest.TestCase):
    def test_frame_path_rides_into_the_target(self):
        target = ingest.guess_target({"type": "click", "element": "button",
                                      "selector": "#create",
                                      "framePath": ["#panel-frame"]})
        self.assertEqual(target, {"selector": "#create",
                                  "frames": ["#panel-frame"]})

    def test_the_label_guess_searches_the_frame_too(self):
        target = ingest.guess_target({"type": "click", "element": "button",
                                      "label": "Create",
                                      "framePath": ["#panel-frame"]})
        self.assertEqual(target["frames"], ["#panel-frame"])

    def test_junk_frame_paths_are_dropped(self):
        for junk in (None, "not-a-list", [], ["", "  "], [1, 2]):
            target = ingest.guess_target({"type": "click", "selector": "#x",
                                          "framePath": junk})
            self.assertNotIn("frames", target, junk)

    def test_a_frameless_action_is_byte_identical_to_before(self):
        self.assertEqual(ingest.guess_target({"selector": "#create"}),
                         {"selector": "#create"})


class SandboxRewrite(unittest.TestCase):
    """The capture engine stamps frames sandbox="allow-popups allow-top-
    navigation-by-user-activation" — no allow-same-origin, so contentDocument
    is null and no frame target could resolve. unlock_frames() adds it at
    serve/export time; anything else sandbox-shaped is left alone."""

    def test_the_stamp_gains_allow_same_origin(self):
        html = f'<iframe {export.FRAME_SANDBOX_STAMP}" srcdoc="x"></iframe>'
        self.assertIn('sandbox="allow-same-origin allow-popups', export.unlock_frames(html))

    def test_the_scripted_variant_is_covered_too(self):
        """SingleFile appends allow-scripts etc. after the same prefix."""
        html = (f'<iframe {export.FRAME_SANDBOX_STAMP} allow-scripts'
                '" srcdoc="x"></iframe>')
        out = export.unlock_frames(html)
        self.assertIn("allow-same-origin", out)
        self.assertIn("allow-scripts", out)

    def test_an_authored_sandbox_attribute_is_untouched(self):
        html = '<iframe sandbox="allow-forms" src="x.html"></iframe>'
        self.assertEqual(export.unlock_frames(html), html)

    def test_exported_pages_get_the_rewrite(self):
        html = f'<html><body><iframe {export.FRAME_SANDBOX_STAMP}"></iframe></body></html>'
        self.assertIn("allow-same-origin", export.preprocess_page(html))


class BridgeParity(unittest.TestCase):
    """The bridge must descend hops exactly as match() does. Its source is
    string-pinned here the way test_merged_runs pins the run pacing — the
    real proof is the shared fixtures above resolving to one element."""

    def setUp(self):
        from pathlib import Path
        self.js = (Path(__file__).resolve().parents[1] / "captcher"
                   / "static" / "bridge.js").read_text(encoding="utf-8")

    def test_hops_use_querySelector_not_querySelectorAll(self):
        body = self.js[self.js.index("function contextDocFor"):]
        body = body[:body.index("\n  }")]
        self.assertIn("doc.querySelector(frames[i])", body)

    def test_target_matches_resolves_its_document_through_the_hops(self):
        body = self.js[self.js.index("function targetMatches"):]
        body = body[:body.index("\n  }")]
        self.assertIn("contextDocFor(t)", body)
        self.assertIn("doc.querySelectorAll(t.selector)", body)
