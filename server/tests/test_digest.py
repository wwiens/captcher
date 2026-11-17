"""The page digest the authoring prompt is built from.

digest.py replaces raw captured HTML in the authoring prompt (a 13-page
Salesforce capture prepared to 2.1M chars — over the API's practical limit)
with a per-page digest: a visible-text outline plus an inventory of
interactive elements, each carrying a selector the SERVER generated and
verified unique against its own parse tree. Everything here defends one
property: a `sel:` line shown to the model resolves, in the player, to exactly
the element the digest described.

The matcher must therefore stay faithful to bridge.js two ways:
`norm(textContent)` semantics for text keys (whitespace collapsed, no
separators between concatenated cells), and querySelectorAll semantics for the
generated selector grammar. It must also refuse — return None, never guess —
on CSS outside that grammar, because validate() reports its verdicts as
warnings and a false "matches 3 times" on a legitimate full-CSS selector
would teach authors to ignore the warning that matters.
"""
import unittest

from captcher import digest
from captcher.digest import match, page_digest, parse_page, target_for


def tree_of(html):
    return parse_page(html)


def find(tree, tag, **attrs):
    """First element with this tag carrying all the given attributes."""
    for n in tree.by_tag.get(tag, []):
        if all(n.attrs.get(k.replace("_", "-")) == v for k, v in attrs.items()):
            return n
    raise AssertionError(f"no <{tag}> with {attrs} in fixture")


class TreeBuilding(unittest.TestCase):
    def test_void_elements_take_no_children(self):
        t = tree_of("<div><input type='text'><span>after</span></div>")
        inp = t.by_tag["input"][0]
        self.assertEqual(inp.element_children(), [])
        span = t.by_tag["span"][0]
        self.assertEqual(span.parent.tag, "div")

    def test_implied_end_tags(self):
        t = tree_of("<ul><li>one<li>two<li>three</ul><p>a<p>b")
        lis = t.by_tag["li"]
        self.assertEqual([li.norm_text() for li in lis], ["one", "two", "three"])
        for li in lis:
            self.assertEqual(li.parent.tag, "ul")
        self.assertEqual([p.norm_text() for p in t.by_tag["p"]], ["a", "b"])

    def test_script_style_template_noscript_content_is_skipped(self):
        t = tree_of("<div><script>var a='<b>bold</b>';</script>"
                    "<style>.x{color:red}</style>"
                    "<template><button>tpl</button></template>"
                    "<noscript><a href='x'>ns</a></noscript>"
                    "<span>kept</span></div>")
        self.assertEqual(t.by_tag["div"][0].norm_text(), "kept")
        self.assertNotIn("button", t.by_tag)
        self.assertNotIn("a", t.by_tag)

    def test_nested_skip_content_does_not_end_the_outer_skip(self):
        t = tree_of("<div><noscript><template><b>x</b></template>"
                    "<i>still-skipped</i></noscript><em>kept</em></div>")
        self.assertNotIn("i", t.by_tag)
        self.assertEqual(t.by_tag["em"][0].norm_text(), "kept")

    def test_entities_resolve_in_text_but_src_stays_byte_exact(self):
        html = '<div><button title="A &amp; B">Save &amp; close</button></div>'
        t = tree_of(html)
        b = t.by_tag["button"][0]
        # norm_text mirrors the DOM's textContent, where charrefs are resolved
        self.assertEqual(b.norm_text(), "Save & close")
        # the src excerpt is the raw start tag from the file — the anchor a
        # capture_edits find string is copied from, so it must not be decoded
        self.assertEqual(b.src, '<button title="A &amp; B">')
        self.assertIn(b.src, html)

    def test_norm_text_concatenates_cells_like_textContent(self):
        # bridge.js norm(textContent): no separator between adjacent cells
        t = tree_of("<table><tr><td>Sandra Whitfield</td><td>s.w@x.com</td></tr></table>")
        self.assertEqual(t.by_tag["tr"][0].norm_text(),
                         "Sandra Whitfields.w@x.com")

    def test_duplicate_attributes_first_wins(self):
        t = tree_of('<div id="a" id="b">x</div>')
        self.assertEqual(t.by_tag["div"][0].attrs["id"], "a")

    def test_empty_page_raises(self):
        with self.assertRaises(digest.DigestError):
            parse_page("just text, no elements at all")


class LabelAssociation(unittest.TestCase):
    HTML = """<form>
      <label for="fn">First name</label><input id="fn" type="text">
      <label>Last name<input id="ln" type="text"></label>
      <span id="cap">Nickname</span><input id="nn" aria-labelledby="cap" type="text">
      <input id="ph" placeholder="Phone" type="text">
    </form>"""

    def names(self):
        t = tree_of(self.HTML)
        by_for, wrapping = digest._label_index(t)
        return {n.attrs["id"]: digest._accessible_name(n, t, by_for, wrapping)
                for n in t.by_tag["input"]}

    def test_label_for(self):
        self.assertEqual(self.names()["fn"], "First name")

    def test_wrapping_label(self):
        self.assertEqual(self.names()["ln"], "Last name")

    def test_aria_labelledby(self):
        self.assertEqual(self.names()["nn"], "Nickname")

    def test_placeholder_fallback(self):
        self.assertEqual(self.names()["ph"], "Phone")


class GeneratedIds(unittest.TestCase):
    def test_generated(self):
        for tok in ("695:0", ":r1:", "ember123", "12345",
                    "a1b2c3d4-e5f6-1234-abcd-1234567890ab",
                    "input-39", "brandBand_2", "deadbeef1"):
            self.assertTrue(digest.is_generated_id(tok), tok)

    def test_authored(self):
        for tok in ("main-nav", "search", "loginForm", "app", "content-area"):
            self.assertFalse(digest.is_generated_id(tok), tok)


class Matcher(unittest.TestCase):
    HTML = """<body><div id="app" class="shell dark">
      <div class="modal open"><button class="btn primary" name="save">Save</button>
        <button class="btn">Cancel</button></div>
      <ul><li>alpha</li><li>beta</li><li>beta</li></ul>
      <p data-testid="note">A note</p>
    </div></body>"""

    def setUp(self):
        self.t = tree_of(self.HTML)

    def sel(self, s, **extra):
        return match(self.t, {"selector": s, **extra})

    def test_tag_id_class_attr(self):
        self.assertEqual(len(self.sel("button")), 2)
        self.assertEqual(len(self.sel("#app")), 1)
        self.assertEqual(len(self.sel(".btn.primary")), 1)
        self.assertEqual(len(self.sel('button[name="save"]')), 1)
        self.assertEqual(len(self.sel('p[data-testid="note"]')), 1)
        self.assertEqual(len(self.sel("[data-testid]")), 1)

    def test_class_is_token_membership(self):
        self.assertEqual(len(self.sel("div.modal")), 1)
        self.assertEqual(len(self.sel("div.open")), 1)

    def test_descendant_and_child_combinators(self):
        self.assertEqual(len(self.sel("#app button")), 2)
        self.assertEqual(len(self.sel("div.modal > button")), 2)
        self.assertEqual(len(self.sel("ul > button")), 0)

    def test_nth_of_type(self):
        hits = self.sel("li:nth-of-type(2)")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].norm_text(), "beta")

    def test_text_filters_match_bridge_semantics(self):
        # containment for `text`, normalised equality for `text_exact`
        self.assertEqual(len(self.sel("button", text="Sav")), 1)
        self.assertEqual(len(self.sel("button", text_exact="Save")), 1)
        self.assertEqual(len(self.sel("li", text="beta")), 2)
        self.assertEqual(len(self.sel("li", text_exact="bet")), 0)

    def test_out_of_grammar_returns_none_not_a_verdict(self):
        for s in ("button:hover", "div ~ p", "a[href^='/x']", "li:first-child",
                  "button, a", "*"):
            self.assertIsNone(self.sel(s), s)

    def test_quoted_attribute_values_unescape(self):
        t = tree_of('<div data-x="say &quot;hi&quot;">x</div>')
        self.assertEqual(len(match(t, {"selector": '[data-x="say \\"hi\\""]'})), 1)


class SelectorLadder(unittest.TestCase):
    def unique_target(self, html, tag, **attrs):
        t = tree_of(html)
        node = find(t, tag, **attrs)
        target = target_for(t, node)
        hits = match(t, target)
        self.assertEqual(len(hits), 1, target)
        self.assertIs(hits[0], node)
        return target

    def test_authored_id_wins(self):
        t = self.unique_target('<div><a id="main-nav">x</a><a>y</a></div>', "a",
                               id="main-nav")
        self.assertEqual(t, {"selector": "#main-nav"})

    def test_generated_id_is_not_rung_one(self):
        t = self.unique_target('<div><input id="input-39"><input></div>',
                               "input", id="input-39")
        self.assertNotEqual(t, {"selector": "#input-39"})

    def test_name_attr(self):
        t = self.unique_target('<form><input name="email"><input></form>',
                               "input", name="email")
        self.assertEqual(t, {"selector": 'input[name="email"]'})

    def test_aria_label(self):
        t = self.unique_target('<div><button aria-label="Close">x</button>'
                               "<button>x</button></div>", "button", aria_label="Close")
        self.assertEqual(t, {"selector": 'button[aria-label="Close"]'})

    def test_authored_classes_generated_ones_refused(self):
        html = ('<div><button class="btn-danger ladnjH">Del</button>'
                '<button class="btn-safe gpULFc">Keep</button></div>')
        t = self.unique_target(html, "button", **{"class": "btn-danger ladnjH"})
        self.assertEqual(t, {"selector": "button.btn-danger"})

    def test_ancestor_scoping(self):
        html = ('<div id="list"><button class="btn">Go</button></div>'
                '<div id="modal-1"><button class="btn">Go</button></div>')
        t = self.unique_target(html, "button")
        self.assertIn(t["selector"], ("#list button.btn", "#list button"))

    def test_text_anchor_when_structure_gives_nothing(self):
        html = "<div><button>Import</button><button>Export</button></div>"
        t = self.unique_target(html, "button")
        self.assertEqual(t.get("text_exact"), "Import")

    def test_nth_chain_for_identical_siblings(self):
        html = '<div id="app"><span><button>Go</button></span><span><button>Go</button></span></div>'
        t = self.unique_target(html, "button")
        self.assertIn(":nth-of-type(", t["selector"])


class SalesforceLikeFixture(unittest.TestCase):
    """A synthetic page with the hostile traits of the real captures: Aura
    colon ids, SLDS classes (authored-looking, near-universal), a modal that
    LIES about being hidden (Salesforce wraps its record-edit modal in
    aria-hidden="true" — the salesforce-new-contact-v2 capture), a genuinely
    hidden duplicate, and rows of near-identical markup."""

    ROWS = "\n".join(
        f'<tr class="slds-hint-parent"><td><a href="/c/{i}">Contact {i}</a></td>'
        f'<td><button class="slds-button" aria-label="Edit Contact {i}">Edit</button></td></tr>'
        for i in range(30))
    HTML = f"""<html><head><title>Contacts | Salesforce</title>
      <style>.x{{}}</style></head><body>
      <div id="brandBand_2"><h1 class="slds-page-header__title">Contacts</h1>
      <button class="slds-button slds-button_neutral" name="New">New</button>
      <button class="slds-button slds-button_neutral">Import</button>
      <table><tbody>{ROWS}</tbody></table>
      <div aria-hidden="true" class="uiModal--medium">
      <div role="dialog" aria-modal="true" class="slds-modal">
        <h2>New Contact</h2>
        <label for="695:0">Salutation</label>
        <select id="695:0"><option value="">--None--</option>
          <option value="Mr.">Mr.</option><option selected>Ms.</option></select>
        <label for="input-39">First Name</label><input type="text" id="input-39">
        <button class="slds-button slds-button_brand" title="Save">Save</button>
      </div></div>
      <div hidden><button class="slds-button slds-button_brand">Save</button></div>
      </body></html>"""

    @classmethod
    def setUpClass(cls):
        cls.tree = tree_of(cls.HTML)
        cls.block, cls.entries = page_digest("step-004/page.html", cls.tree,
                                             aliases=["step-005/page.html"])

    def test_every_generated_selector_is_verified_unique(self):
        self.assertGreater(len(self.entries), 60)
        for e in self.entries:
            hits = match(self.tree, e["target"])
            self.assertIsNotNone(hits, e)
            self.assertEqual(len(hits), 1, e)

    def test_hidden_attribute_excludes_but_aria_hidden_does_not(self):
        # the `hidden` duplicate Save is out; the modal's Save — inside the
        # aria-hidden="true" uiModal wrapper — must stay IN: honouring
        # aria-hidden dropped Salesforce's whole record form from the digest
        # and the authoring pass wrote the form entry out of the walkthrough
        saves = [e for e in self.entries if e["name"] == "Save"]
        self.assertEqual(len(saves), 1)

    def test_the_lying_modal_contents_are_in_the_inventory(self):
        names = {e["name"] for e in self.entries}
        self.assertIn("Salutation", names)          # the select, via its label
        self.assertIn("First Name", names)          # the input, via label[for]
        self.assertIn("[h2] New Contact", self.block)   # outline sees it too

    def test_select_line_carries_options_and_label(self):
        line = next(ln for ln in self.block.splitlines() if " select " in ln)
        self.assertIn('"Salutation"', line)
        self.assertIn("--None--", line)
        self.assertIn('selected "Ms."', line)

    def test_outline_has_title_headings_and_row_text(self):
        self.assertIn("title: Contacts | Salesforce", self.block)
        self.assertIn("[h1] Contacts", self.block)
        self.assertIn("Contact 7", self.block)

    def test_aliases_are_named_as_interchangeable_pages(self):
        self.assertIn("step-005/page.html", self.block)

    def test_digest_is_a_fraction_of_the_page(self):
        # the point of the module: the block must stay bounded even as the
        # page grows — 40K chars ≈ 20K est tokens is the per-page budget
        self.assertLess(len(self.block), 40_000)

    def test_src_excerpts_are_copyable_find_strings(self):
        for e in self.entries[:20]:
            node = match(self.tree, e["target"])[0]
            if node.src and len(node.src) <= digest.SRC_EXCERPT_CHARS:
                self.assertIn(node.src, self.HTML)


class SrcExcerptSignal(unittest.TestCase):
    """_src_excerpt spends the budget on signal: class/style are elided (each
    elision visibly marked), inline-handler bodies are clipped individually,
    and everything kept is a contiguous byte-run of the original tag — the
    contract the capture_edits prompt promises."""

    # the real shape that motivated this: a Tailwind class attribute longer
    # than the whole excerpt budget, with the handler after it
    TAG = ('<input class="' + "x-util " * 60 + '" id=pay-amount '
           'data-testid=payment-amount value=3712.15 '
           'oninput="var v=parseFloat(this.value);'
           + "var s='pad';" * 30 + 'update(v)">')

    def test_class_is_elided_and_marked(self):
        out = digest._src_excerpt(self.TAG)
        self.assertNotIn("x-util", out)
        self.assertIn(" …", out)

    def test_identity_and_handler_survive_and_are_copyable(self):
        out = digest._src_excerpt(self.TAG)
        for kept in ("id=pay-amount", "data-testid=payment-amount",
                     "value=3712.15", 'oninput="var v=parseFloat(this.value);'):
            self.assertIn(kept, out)
            # contiguous runs between elisions are byte-runs of the file
            self.assertIn(kept, self.TAG)

    def test_handler_body_is_clipped_not_the_tail(self):
        out = digest._src_excerpt(self.TAG)
        self.assertNotIn("update(v)", out)      # deep body clipped
        self.assertIn("…", out.split("oninput")[1])

    def test_reactive_marker_lists_handler_events(self):
        html = ('<body><input id=a oninput="f()" onchange="g()">'
                '<button onboarding-step="2" name=quiet>Q</button></body>')
        t = tree_of(html)
        block, entries = page_digest("p.html", t)
        self.assertIn("reactive: onchange,oninput", block)
        # a data attribute merely starting with "on" is not an event
        for ln in block.splitlines():
            if "quiet" in ln:
                self.assertNotIn("reactive", ln)

    def test_plain_tags_pass_through_unchanged(self):
        tag = '<button type=button data-testid=save>'
        self.assertEqual(digest._src_excerpt(tag), tag)


class DisplayInventory(unittest.TestCase):
    """Named read-only elements get d-refs: anchors for capture_edits,
    never step targets."""

    HTML = ('<body><button data-testid=submit>Record $10</button>'
            '<span data-testid=grand-total>$10</span>'
            '<div role=status>Saved</div>'
            '<output>42</output>'
            '<p aria-live=polite>3 items</p>'
            '<span>anonymous, not inventoried</span>'
            '<span data-testid=hidden-total hidden>$0</span></body>')

    @classmethod
    def setUpClass(cls):
        cls.block, cls.entries = page_digest("p.html", tree_of(cls.HTML))

    def test_display_section_lists_named_readonly_elements(self):
        self.assertIn("DISPLAY ELEMENTS", self.block)
        section = self.block.split("DISPLAY ELEMENTS")[1]
        for expected in ("grand-total", "role=status", "output", "aria-live"):
            self.assertIn(expected, section)

    def test_interactive_elements_are_not_double_listed(self):
        section = self.block.split("DISPLAY ELEMENTS")[1]
        self.assertNotIn("data-testid=submit", section)

    def test_anonymous_and_hidden_elements_stay_out(self):
        self.assertNotIn("anonymous", self.block.split("DISPLAY ELEMENTS")[1])
        self.assertNotIn("hidden-total", self.block)

    def test_display_entries_are_flagged_with_d_refs(self):
        disp = [e for e in self.entries if e.get("display")]
        self.assertEqual(len(disp), 4)
        for e in disp:
            self.assertTrue(e["ref"].startswith("d"), e["ref"])
        inter = [e for e in self.entries if not e.get("display")]
        self.assertTrue(all(e["ref"].startswith("e") for e in inter))

    def test_display_src_excerpt_is_a_find_anchor(self):
        section = self.block.split("DISPLAY ELEMENTS")[1]
        self.assertIn("src: <span data-testid=grand-total>", section)


class OutlineShape(unittest.TestCase):
    def test_outline_dedups_and_caps(self):
        blocks = "".join("<p>repeated line</p>" for _ in range(50))
        filler = "".join(f"<p>unique paragraph number {i} with some padding text</p>"
                         for i in range(400))
        t = tree_of(f"<body>{blocks}{filler}</body>")
        out = digest._outline(t)
        self.assertEqual(out.count("repeated line"), 1)
        self.assertLessEqual(len(out), digest.OUTLINE_MAX_CHARS + 100)
        self.assertIn("outline elided", out)

    def test_inventory_caps_prefer_dialog_content(self):
        many = "".join(f"<button>b{i}</button>" for i in range(digest.MAX_ELEMENTS_PER_PAGE))
        html = (f"<body>{many}<div role='dialog'><button name='inside'>In</button>"
                f"</div></body>")
        t = tree_of(html)
        nodes = digest._interactive_nodes(t)
        self.assertEqual(len(nodes), digest.MAX_ELEMENTS_PER_PAGE)
        self.assertTrue(any(n.attrs.get("name") == "inside" for n in nodes))


if __name__ == "__main__":
    unittest.main()
