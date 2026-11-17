"""Guards for the file-backed templates.

The risk this refactor introduces is drift: a placeholder renamed in a
template file but not at its call site (or vice versa) would silently render
a literal `__FOO__` into the page. test_pages.py catches that for the rendered
result; these tests catch it at the source, with a clearer failure.
"""
import re
import unittest
from pathlib import Path

from captcher import templates

TEMPLATE_DIR = Path(templates.TEMPLATE_DIR)

# template -> the keys server.py supplies when rendering it.
#
# Two whole keys are now in that category. TOKENS_CSS and TOOLBAR_CSS are still
# supplied by server._chrome() and the auth page builders, but no template holds
# those placeholders any more: the tokens and the top bar's rules moved into
# static/captcher.css, which every page links, and templates/_tokens.css
# and templates/_toolbar.css are deliberately empty. They stay listed here
# because this map documents what server.py supplies, not what each template
# consumes.
#
# Note account.html is supplied TOOLBAR_SCRIPT but has no such placeholder:
# the account page never included the toolbar's scroll-shadow script (only
# catalog_page did). That predates the move to files and is preserved here
# deliberately, so rendered output is unchanged. Supplying an unused key is
# inert; a placeholder with no key is the real bug, and that is what
# test_every_placeholder_is_supplied checks.
SUPPLIED = {
    "_logo.svg":              {"S", "STROKE"},
    "_toolbar.html":          {"LOGO", "CLS_CATALOG", "CLS_BRANDING", "CLS_ACCOUNT", "CLS_EXTENSION",
                               "CLS_GETTING_STARTED", "CLS_USAGE",
                               "CUR_CATALOG", "CUR_BRANDING", "CUR_ACCOUNT", "CUR_EXTENSION",
                               "CUR_GETTING_STARTED", "CUR_USAGE"},
    "_tokens.css":            set(),
    "_toolbar.css":           set(),
    "_toolbar.js":            set(),
    "_theme-head.html":       set(),
    "_extension-panel.html":  set(),
    "_catalog-script.html":   set(),
    # Shown only in access-token mode, before the server is unlocked.
    "unlock.html":            {"LOGO", "ERROR"},
    "getting-started.html":   {"THEME_HEAD", "TOOLBAR", "MAX_STEPS", "MAX_PAGES"},
    "extension.html":         {"THEME_HEAD", "TOOLBAR", "EXTENSION_DIR"},
    "account.html":           {"TOKENS_CSS", "THEME_HEAD", "TOOLBAR_CSS", "TOOLBAR", "TOOLBAR_SCRIPT",
                               "DEVICES", "AI_STATUS"},
    "usage.html":             {"TOKENS_CSS", "THEME_HEAD", "TOOLBAR_CSS", "TOOLBAR",
                               "TOOLBAR_SCRIPT", "PRICES_AS_OF", "PERIODS", "NOTICE",
                               "TOTAL_COST", "TOTAL_CALLS", "PER_AUTHOR", "PER_EDIT",
                               "BY_OPERATION", "BY_WALKTHROUGH", "BY_MODEL", "BY_DAY"},
    "catalog.html":           {"TOKENS_CSS", "THEME_HEAD", "TOOLBAR_CSS", "TOOLBAR", "TOOLBAR_SCRIPT",
                               "CARDS", "EXTENSION_PANEL", "CATALOG_SCRIPT"},
    "review.html":            {"TOKENS_CSS", "THEME_HEAD", "TOOLBAR_CSS", "TOOLBAR", "TOOLBAR_SCRIPT",
                               "ID", "CHECKS"},
    "branding.html":          {"TOKENS_CSS", "THEME_HEAD", "TOOLBAR_CSS", "TOOLBAR", "TOOLBAR_SCRIPT",
                               "COLOR_ROWS", "COLLECTION_JSON", "MYBRAND_JSON"},
}


class TemplateTests(unittest.TestCase):
    def test_every_template_file_is_accounted_for(self):
        on_disk = {p.name for p in TEMPLATE_DIR.iterdir() if p.is_file()}
        self.assertEqual(on_disk, set(SUPPLIED),
                         "templates/ contents and SUPPLIED have drifted apart")

    def test_every_placeholder_is_supplied(self):
        """A placeholder with no matching key renders as a literal __FOO__."""
        for name, supplied in SUPPLIED.items():
            with self.subTest(template=name):
                present = set(templates.placeholders(name))
                missing = present - supplied
                self.assertFalse(
                    missing,
                    f"{name} contains {sorted(missing)} but server.py never "
                    f"supplies {'it' if len(missing) == 1 else 'them'}")

    def test_render_substitutes_and_leaves_nothing_behind(self):
        out = templates.render("_logo.svg", S=30, STROKE="#1E3B2F")
        self.assertIn('width="30"', out)
        # The Captcher mark is four frame bars and five stepping squares.
        # STROKE is the frame's colour and is the only thing the caller
        # chooses; the squares' colours are part of the mark and are
        # deliberately not substitutable.
        self.assertEqual(out.count('fill="#1E3B2F"'), 4)
        for colour in ("#FF6A5C", "#29B6FF", "#3B49C6", "#3ECFA2"):
            self.assertIn(f'fill="{colour}"', out)
        self.assertNotIn("__", out)

    def test_render_ignores_unknown_keys_but_keeps_markers_visible(self):
        """An unsupplied placeholder must survive as a literal so that
        test_pages' placeholder check can catch it, rather than silently
        rendering an empty string."""
        out = templates.render("_logo.svg", S=30)
        self.assertIn("__STROKE__", out)

    def test_none_renders_as_empty_string(self):
        self.assertNotIn("None", templates.render("_logo.svg", S=None, STROKE="#000"))

    def test_missing_template_raises_a_clear_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            templates.load("nope.html")
        self.assertIn("nope.html", str(ctx.exception))

    def test_mtime_cache_picks_up_edits_in_debug_mode(self):
        tmp = TEMPLATE_DIR / "_cache_probe.html"
        tmp.write_text("one", encoding="utf-8")
        try:
            templates.set_debug(True)
            self.assertEqual(templates.load("_cache_probe.html"), "one")
            import os
            import time
            tmp.write_text("two", encoding="utf-8")
            os.utime(tmp, (time.time() + 1, time.time() + 1))
            self.assertEqual(templates.load("_cache_probe.html"), "two")
        finally:
            tmp.unlink(missing_ok=True)
            templates._CACHE.pop("_cache_probe.html", None)

    def test_script_json_cannot_close_the_script_block(self):
        """json.dumps escapes neither < nor /, so a value containing
        </script> would terminate the block and the rest would parse as
        markup. The branding page is reachable with attacker-influenced
        values via "import from a URL"."""
        payload = {"brand_name": 'Acme </script><img src=x onerror=alert(1)>'}
        out = templates.script_json(payload)
        self.assertNotIn("</script>", out)
        self.assertNotIn("</SCRIPT>", out.upper().replace("\\U003C", ""))
        self.assertIn("\\u003c", out)

    def test_script_json_still_parses_to_the_original_value(self):
        import json
        for payload in ({"a": "<b>"}, {"t": "Café — naïve · 日本語"},
                        ["<", ">", "&"], {"nested": {"x": "</script>"}}):
            with self.subTest(payload=payload):
                self.assertEqual(json.loads(templates.script_json(payload)), payload)

    def test_no_template_still_contains_python_brace_escaping(self):
        """These files came out of f-strings; a stray `{{` would be a
        conversion bug that renders as a literal double brace."""
        for name in SUPPLIED:
            with self.subTest(template=name):
                text = templates.load(name)
                # CSS/JS legitimately use { }, but never doubled in our sources
                self.assertNotIn("{{", text, f"{name} has leftover f-string escaping")
                self.assertNotIn("}}", text, f"{name} has leftover f-string escaping")


class BrandNameTests(unittest.TestCase):
    """The product name is "Captcher", title case, everywhere.

    The design system states it as a rule, which makes it testable, and it has
    already been broken once: the wordmark rendered one spelling in the app
    chrome while `branding.py`'s default profile, the brand-profile preview and
    the export applier all wrote another. So an exported walkthrough spelled
    the product one way and the page that produced it spelled it another — the
    kind of thing nobody files a bug for and everybody notices.
    """

    APP_ROOT = Path(__file__).resolve().parents[1] / "captcher"
    WORDMARK = "Captcher"

    def _brand_surfaces(self):
        """Every file that renders the wordmark or names the product to a user."""
        for d in ("templates", "static"):
            yield from sorted((self.APP_ROOT / d).glob("*.html"))

    def test_every_wordmark_is_the_title_case_name(self):
        """The visible `.brand-word` lockup, on every page that carries one."""
        pattern = re.compile(r'<span class="brand-word">([^<]*)</span>')
        found = 0
        for p in self._brand_surfaces():
            for word in pattern.findall(p.read_text(encoding="utf-8")):
                found += 1
                self.assertEqual(
                    word, self.WORDMARK,
                    f"{p.name} renders the wordmark as {word!r}. The brand is "
                    f"{self.WORDMARK!r} — title case, always (Brand Guide v1).")
        self.assertGreater(found, 0, "no wordmark found; has the markup changed?")

    def test_the_old_product_name_is_gone(self):
        """The product's previous name, in either spelling, is gone.

        Scoped to the brand surfaces, so it cannot fire on the `sc-` CSS
        namespace or the `data-sc-*` embed attributes, which are machine
        spellings and not customer-facing copy.
        """
        for p in self._brand_surfaces():
            text = p.read_text(encoding="utf-8")
            for old in ("ShadowCapture", "Shadow Capture"):
                self.assertNotIn(old, text,
                                 f"{p.name} still says {old!r}; the product is Captcher.")

    def test_the_default_brand_profile_agrees_with_the_chrome(self):
        """The one that actually broke.

        `export.apply_branding` rewrites every `.brand-word` with the active
        profile's `brand_name`, so these two strings appear in the same rendered
        artefact and disagreeing is visible.
        """
        from captcher import branding
        self.assertEqual(branding.DEFAULT_BRANDING["brand_name"], self.WORDMARK)


if __name__ == "__main__":
    unittest.main()
