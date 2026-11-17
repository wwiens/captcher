"""The light/dark toggle, checked at the source.

The toggle is four small things in four files and every one of them is silent
when it breaks: a head partial that must stay in the head, a stylesheet block
that must define the light half of every themed token, a control on /account
that must call the partial's API by the name it actually exports, and a rule
that the player is not in scope. Nothing here needs a database or a browser —
these are the invariants a reader would otherwise have to hold in their head.
"""
import re
import unittest
from pathlib import Path

from captcher import templates

ROOT = Path(templates.TEMPLATE_DIR).parent
TEMPLATE_DIR = Path(templates.TEMPLATE_DIR)
PARTIAL = "_theme-head.html"

# Every page that carries the toolbar carries the theme, because "signed in"
# and "themed" are the same set. The signed-out pages are listed too, as the
# other half of the assertion.
SIGNED_IN = ["getting-started.html", "extension.html", "account.html", "usage.html",
             "branding.html", "catalog.html", "review.html"]
# The unlock page is the one app page shown before there is an owner.
SIGNED_OUT = ["unlock.html"]


def read(name):
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


class ThemeHeadPartialTests(unittest.TestCase):
    def setUp(self):
        self.partial = read(PARTIAL)

    def test_it_is_an_inline_script_with_no_src_and_no_defer(self):
        """An external or deferred script paints dark first and then flips.

        This is the whole reason the partial is inlined into the head instead
        of living in static/ beside pair.js, so it is worth a test: the failure
        it prevents is a flash on every navigation, which is easy to introduce
        while tidying and easy to miss on a fast local server.
        """
        tags = re.findall(r"<script\b[^>]*>", self.partial)
        self.assertEqual(len(tags), 1, "the partial is exactly one script")
        # The open tag itself, not the file — the prose above it explains why
        # there is no defer, and would match a naive search of the whole text.
        self.assertEqual(tags[0], "<script>")

    def test_the_three_states_are_system_light_and_dark(self):
        self.assertIn('"system"', self.partial)
        self.assertIn('"light"', self.partial)
        self.assertIn('"dark"', self.partial)

    def test_system_is_never_written_to_the_dom(self):
        """The stylesheet has one [data-sc-theme="dark"] block and no
        prefers-color-scheme query, so the OS answer has to be resolved in JS
        and stamped as an explicit light|dark. A "system" reaching the
        attribute would silently paint light."""
        stamp = re.search(r'setAttribute\("data-sc-theme",\s*(\w+)\)', self.partial)
        self.assertIsNotNone(stamp, "nothing stamps data-sc-theme")
        self.assertEqual(stamp.group(1), "resolved")

    def test_every_storage_access_is_guarded(self):
        """localStorage does not merely return null when a browser blocks site
        data — the accessor itself throws, and an unguarded read in the head
        takes the whole page down before it paints."""
        for call in ("localStorage.getItem", "localStorage.setItem"):
            self.assertIn(call, self.partial)
        self.assertEqual(self.partial.count("catch (e)"), 2,
                         "each localStorage access needs its own try/catch")

    def test_it_exports_the_api_the_account_control_calls(self):
        self.assertIn("window.scTheme", self.partial)
        for name in ("get:", "set:"):
            self.assertIn(name, self.partial)


class ThemeWiringTests(unittest.TestCase):
    def test_every_signed_in_page_carries_it(self):
        for name in SIGNED_IN:
            with self.subTest(page=name):
                self.assertIn("__THEME_HEAD__", read(name))

    def test_it_sits_in_the_head(self):
        """Below </head> the page has already begun to paint."""
        for name in SIGNED_IN:
            with self.subTest(page=name):
                page = read(name)
                self.assertLess(page.index("__THEME_HEAD__"), page.index("</head>"))

    def test_it_precedes_the_stylesheet(self):
        """Stamping after the stylesheet link still beats first paint, but only
        by accident of how the parser blocks. Above it, the attribute is on
        <html> before the first byte of CSS is even requested."""
        for name in SIGNED_IN:
            with self.subTest(page=name):
                page = read(name)
                self.assertLess(page.index("__THEME_HEAD__"),
                                page.index("/static/captcher.css"))

    def test_the_signed_out_pages_do_not(self):
        """No settled preference exists before somebody has an account, and
        these pages are rendered by their own builders rather than _chrome()."""
        for name in SIGNED_OUT:
            with self.subTest(page=name):
                self.assertNotIn("__THEME_HEAD__", read(name))

    def test_the_theme_color_meta_is_there_to_be_rewritten(self):
        """The partial retargets it, so a page without one silently keeps a
        dark browser chrome above a light page."""
        for name in SIGNED_IN:
            with self.subTest(page=name):
                self.assertIn('name="theme-color"', read(name))


class DarkPaletteTests(unittest.TestCase):
    """Every token the dark block has to answer for.

    Light is the base (Brand Guide v1). A token defined only in :root keeps its
    light value under [data-sc-theme="dark"] — a white card on a near-black
    page, with no error anywhere. This walks the two blocks and compares them.
    """
    def setUp(self):
        css = (ROOT / "static" / "captcher.css").read_text(encoding="utf-8")
        self.css = css
        root = re.search(r":root \{(.*?)\n\}", css, re.S).group(1)
        dark = re.search(r'\[data-sc-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1)
        self.root_tokens = set(re.findall(r"^\s*(--[\w-]+):", root, re.M))
        self.dark_tokens = set(re.findall(r"^\s*(--[\w-]+):", dark, re.M))

    # The tokens that are deliberately theme-independent: the brand palette
    # itself (a hue is assigned, not decorative — it does not change with the
    # ground), the dark neutrals the dark block is built out of, the legacy
    # aliases (they point at semantic tokens, so they follow the theme without
    # a second value), type, spacing, geometry and motion.
    SHARED = re.compile(
        r"^--(indigo|violet|sky|mint|coral|ink|slate|steel|control$|haze|fog|mist|paper|white|night|"
        r"signal|graphite|ash|stone|miss|cyan|green|yellow|pink|lead|on-lead|base|surface-1|surface-2r|"
        r"hairline|text-1|text-2|text-3|paper-raised|paper-border|shadow-ink|"
        r"focus-width|focus-offset|font|weight|type|track|s\d|radius|line|border-active|hit|gutter|content|"
        r"sidebar-w|ease|dur)")

    def test_every_ground_token_has_a_dark_value(self):
        missing = sorted(t for t in self.root_tokens - self.dark_tokens
                         if not self.SHARED.match(t))
        self.assertEqual(missing, [], f"no dark value for: {missing}")

    def test_the_dark_block_introduces_no_token_of_its_own(self):
        """A token that exists only under dark is undefined in light."""
        self.assertEqual(sorted(self.dark_tokens - self.root_tokens), [])

    def test_both_blocks_declare_color_scheme(self):
        """Without it the native widgets, the scrollbar and the flash between
        navigations stay the other theme's."""
        self.assertIn("color-scheme: dark;", self.css)
        self.assertIn("color-scheme: light;", self.css)

    def test_no_colour_is_hardcoded_outside_the_token_blocks(self):
        """The reason the dark block is enough on its own. The exceptions are
        listed by name so adding one is a decision, not a slip: the mark's
        squares (fixed brand colours, drawn as data URIs), the select caret,
        the selection tint and the white-on-colour steps inside components
        that sit on a brand fill."""
        body = self.css[self.css.index('[data-sc-theme="dark"] {'):]
        body = body[body.index("\n}") + 2:]
        # data: URIs carry the mark's fixed colours as %23-escaped hex.
        body = re.sub(r'url\("data:[^"]*"\)', "url()", body)
        found = re.findall(r"(?:#[0-9A-Fa-f]{3,8}\b|rgba?\([^)]*\))", body)
        allowed = {"rgba(59, 73, 198, .22)",            # ::selection tint
                   "rgba(255,255,255,.35)", "rgba(255,255,255,.12)"}  # .button--light on a fill
        self.assertEqual([f for f in found if f not in allowed], [],
                         f"hardcoded colour below the tokens: {found}")

    def test_light_is_the_default(self):
        """Brand Guide v1 is light-first: nothing stored means light, and
        System resolves to dark only when the OS asks for it."""
        partial = (ROOT / "templates" / "_theme-head.html").read_text(encoding="utf-8")
        self.assertIn('if (choice !== "system" && choice !== "dark") choice = "light";', partial)
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', partial)


class PlayerIsOutOfScopeTests(unittest.TestCase):
    """The brand ends at the edge of the player frame, and the player's tokens
    are the white-label slots export.apply_branding() writes over per profile.
    An app-side theme reaching in there would fight a customer's brand and put
    the app and its own exports into disagreement."""

    def test_the_player_stylesheets_do_not_answer_to_the_app_theme(self):
        for name in ("player.css", "player2.css"):
            with self.subTest(stylesheet=name):
                css = (ROOT / "static" / name).read_text(encoding="utf-8")
                self.assertNotIn("data-sc-theme", css)

    def test_no_app_colour_reaches_inside_the_branding_preview(self):
        """The Branding page's live preview is the player frame drawn in the
        app, so the same boundary applies: everything inside .pv-frame comes
        from --b-*, the profile's own tokens.

        A bare element selector in the app stylesheet is the way through that
        wall, because the preview's markup is ordinary HTML. It happened:
        `strong, b { color: var(--text) }` painted both wordmarks in the app's
        theme colour, which put near-white #ECECF5 on a customer's light card
        and near-black on a dark one — and flipped with the theme switch, so
        the preview lied about the export either way.

        Anything the app colours by element name and the preview uses bare has
        to be neutralised inside the frame.
        """
        css = (ROOT / "static" / "captcher.css").read_text(encoding="utf-8")
        # element selectors (no class, id, attribute or pseudo) that set colour
        coloured = set()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if not re.search(r"(?:^|[;{\s])color\s*:", body):
                continue
            for one in selector.split(","):
                one = one.strip()
                if re.fullmatch(r"[a-z][a-z0-9]*", one):
                    coloured.add(one)
        self.assertIn("b", coloured, "the rule this test guards against is gone "
                                     "— check whether the guard is still needed")

        page = read("branding.html")
        frame = page[page.index('class="pv-frame"'):]
        frame = frame[:frame.index("</aside>")]
        used = {t.lower() for t in re.findall(r"<([a-z][a-z0-9]*)\b", frame)}
        # the page's own <style>, so a failure prints the stylesheet rather
        # than the whole document
        style = page[page.index("<style>"):page.index("</style>")]

        for el in sorted(coloured & used):
            with self.subTest(element=el):
                self.assertRegex(
                    style, rf"\.pv-frame[^{{]*\b{el}\b[^{{]*\{{[^}}]*color\s*:",
                    f"<{el}> is coloured by element name in captcher.css and "
                    f"used bare inside .pv-frame, but nothing in branding.html "
                    f"overrides its colour — the preview will paint it in the "
                    f"app's theme instead of the profile's")


if __name__ == "__main__":
    unittest.main()
