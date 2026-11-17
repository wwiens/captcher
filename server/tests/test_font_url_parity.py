"""The Google Fonts URL builder exists twice — in Python and in JavaScript.

branding.google_fonts_url() builds the webfont URL for exports; the same logic
lives in templates/branding.html as googleFontsUrl(), because the branding
page's live preview recomputes it on every keystroke and a server round-trip
per keypress would be both slow and a network dependency.

That is a defensible duplication, but an undetected one would mean exports and
the preview silently disagreeing about which weights get requested. This test
runs both implementations over the same cases and asserts identical output.

Skipped when node is unavailable.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from captcher import branding

NODE = shutil.which("node")
TEMPLATE = Path(branding.ROOT) / "templates" / "branding.html"

# (heading, body) pairs, including the awkward ones
CASES = [
    ("Roboto", "Open Sans"),
    ("Roboto", "Roboto"),                 # same family twice -> listed once
    ("", ""),                             # nothing selected -> empty URL
    ("Instrument Serif", "IBM Plex Sans"),  # both self-hosted -> empty URL
    ("Instrument Serif", "Roboto"),         # one self-hosted, one not
    ("Playfair Display", "Open Sans"),    # multi-word family -> '+' joined
    ('"Roboto"', "Open Sans"),            # stray quotes are stripped
    ("  Roboto  ", "Open Sans"),          # whitespace is trimmed
    ("Nonexistent Face", "Open Sans"),    # unknown family -> no :wght@
    ("Lobster", "Open Sans"),             # 400-only family -> no :wght@
]

# family -> variants, mirroring what /api/fonts returns
CATALOG = [
    {"family": "Roboto", "category": "sans-serif",
     "variants": ["100", "300", "regular", "500", "700", "900", "italic"]},
    {"family": "Open Sans", "category": "sans-serif",
     "variants": ["300", "regular", "600", "700", "800"]},
    {"family": "Playfair Display", "category": "serif",
     "variants": ["regular", "500", "600", "700", "800", "900"]},
    {"family": "Lobster", "category": "display", "variants": ["regular"]},
]


def _extract_inlined_fonts():
    """The template's own INLINED_FONTS literal.

    This used to be hardcoded in the harness below, which defeated the point:
    the set is the other half of the duplication being tested, and a stale copy
    here kept the test green while the template and branding.py disagreed about
    which faces are self-hosted. Read the real one.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"var INLINED_FONTS = (\{.*?\});", html, re.S)
    if not m:
        raise AssertionError(
            "could not find INLINED_FONTS in templates/branding.html — if it "
            "was renamed or moved, update this test")
    return m.group(1)


def _extract_js_functions():
    """Pull the three font helpers out of the branding template."""
    html = TEMPLATE.read_text(encoding="utf-8")
    out = []
    for name in ("weightsFromVariants", "familyCss2Part", "googleFontsUrl"):
        m = re.search(rf"^  function {name}\(.*?^  \}}", html, re.S | re.M)
        if not m:
            raise AssertionError(
                f"could not find function {name}() in templates/branding.html — "
                f"if it was renamed or moved, update this test")
        out.append(m.group(0))
    return "\n".join(out)


@unittest.skipUnless(NODE, "node is not installed")
class FontUrlParityTests(unittest.TestCase):
    def test_the_self_hosted_set_agrees_across_the_two_implementations(self):
        """Both sides skip a Google URL for the faces we bundle ourselves. If
        they disagree, one of them requests a webfont that is already inlined —
        or, worse, fails to request one that is not."""
        js_set = {k.strip().strip('"\'').lower()
                  for k in re.findall(r'["\']([^"\']+)["\']\s*:', _extract_inlined_fonts())}
        self.assertEqual(js_set, set(branding._INLINED_FONTS))

    def test_python_and_js_build_the_same_url(self):
        js = f"""
{_extract_js_functions()}
var INLINED_FONTS = {_extract_inlined_fonts()};
var FONT_WEIGHTS = {{}};
{json.dumps(CATALOG)}.forEach(function (it) {{
  FONT_WEIGHTS[it.family.toLowerCase()] = weightsFromVariants(it.variants);
}});
var out = {json.dumps(CASES)}.map(function (c) {{
  return googleFontsUrl(c[0], c[1]);
}});
console.log(JSON.stringify(out));
"""
        proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"node failed: {proc.stderr}")
        js_urls = json.loads(proc.stdout)

        # point the Python side at the same catalog
        saved = dict(branding._FONT_WEIGHTS)
        try:
            branding._FONT_WEIGHTS.clear()
            for it in CATALOG:
                branding._FONT_WEIGHTS[it["family"].lower()] = \
                    branding._variants_to_weights(it["variants"])
            py_urls = [branding.google_fonts_url(h, b) for h, b in CASES]
        finally:
            branding._FONT_WEIGHTS.clear()
            branding._FONT_WEIGHTS.update(saved)

        for (heading, body), py, js_url in zip(CASES, py_urls, js_urls):
            with self.subTest(heading=heading, body=body):
                self.assertEqual(py, js_url,
                                 f"Python and JS disagree for ({heading!r}, {body!r})")

    def test_weights_parsing_agrees(self):
        variant_sets = [
            ["regular"], ["italic"], ["regular", "italic"],
            ["100", "300", "regular", "500", "700", "900"],
            ["300italic", "700italic"], [], ["not-a-weight"],
        ]
        js = f"""
{_extract_js_functions()}
console.log(JSON.stringify({json.dumps(variant_sets)}.map(weightsFromVariants)));
"""
        proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"node failed: {proc.stderr}")
        js_weights = json.loads(proc.stdout)
        py_weights = [branding._variants_to_weights(v) for v in variant_sets]
        self.assertEqual(py_weights, js_weights)


if __name__ == "__main__":
    unittest.main()
