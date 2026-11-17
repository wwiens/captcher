"""Folder and ZIP exports: self-contained, and named after the slug.

The export reads its pages out of the bucket now rather than off a resolved
capture directory, so the fixture is a walkthrough row plus objects — but
everything asserted here is unchanged, because an export is still a folder on
somebody's disk and that is what makes it worth having.
"""
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from captcher import export
from conftest import new_member

from . import fixtures

ROOT = Path(__file__).resolve().parents[1] / "captcher"

ASSET_REF_RE = re.compile(r'(?:href|src)="(assets/[^"]+)"')

# Folder exports keep the player's source filenames, so these track whichever
# playback surface export ships (the popup player) rather than pinning one.
PLAYER_CSS = f"assets/{export.EXPORT_PLAYER}.css"
PLAYER_JS = f"assets/{export.EXPORT_PLAYER}.js"


class ExportFixtureMixin:
    """A synthetic walkthrough row plus its capture objects, so the suite never
    depends on content in anybody's own data folder."""

    def setUp(self):
        self.owner = new_member()
        self.row = fixtures.make_walkthrough(self.owner, title="Test Walkthrough")
        self.walkthrough_id = self.row["id"]
        self.slug = self.row["slug"]
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()


class FolderExportTests(ExportFixtureMixin, unittest.TestCase):

    def _assert_self_contained(self, index_html: str, root: Path):
        """No leftover /static/ or external http(s) asset references, and
        every assets/… path the HTML points at actually exists on disk."""
        self.assertNotIn("/static/", index_html)
        self.assertNotIn("data:font", index_html)
        refs = ASSET_REF_RE.findall(index_html)
        self.assertIn(PLAYER_CSS, refs)
        self.assertIn(PLAYER_JS, refs)
        self.assertIn("assets/fonts/fonts.css", refs)
        for rel in refs:
            self.assertTrue((root / rel).is_file(), f"referenced asset missing on disk: {rel}")

    def test_builds_index_and_assets_folder(self):
        result = export.build_export_folder(self.walkthrough_id, self.out_dir)
        self.assertEqual(result, self.out_dir)

        index = self.out_dir / "index.html"
        self.assertTrue(index.is_file())
        html = index.read_text(encoding="utf-8")
        self._assert_self_contained(html, self.out_dir)

        # fonts.css must reference sibling files, not /static/fonts/…
        fonts_css = (self.out_dir / "assets" / "fonts" / "fonts.css").read_text(encoding="utf-8")
        self.assertNotIn("/static/", fonts_css)
        for m in re.finditer(r"url\('([^']+)'\)", fonts_css):
            self.assertTrue((self.out_dir / "assets" / "fonts" / m.group(1)).is_file())

        # the walkthrough content is still embedded (no fetch() needed to render)
        self.assertIn('id="sc-embed"', html)
        self.assertIn(fixtures.MARKER, html)
        player_js = (self.out_dir / PLAYER_JS).read_text(encoding="utf-8")
        self.assertIn("sim.srcdoc = html", player_js)
        self.assertNotIn("URL.createObjectURL(new Blob", player_js)

        # the popup step card ships too — exports use the same floating-card
        # presentation as /player/<id>, not the side panel
        self.assertIn('id="step-pop"', html)

    def test_rewrites_downloaded_brand_images_to_local_paths(self):
        branding = {
            "brand_name": "Acme",
            "logo_light_url": "https://acme.example/logo-light.png",
            "logo_dark_url": "https://acme.example/logo-light.png",  # same URL twice
            "favicon_url": "https://acme.example/favicon.ico",
            "colors": {}, "fonts": {},
        }

        def fake_fetch(url):
            if "favicon" in url:
                return url, "image/x-icon", b"FAKEICO"
            return url, "image/png", b"\x89PNGFAKE"

        with patch("captcher.brandfetch.fetch_public_image", side_effect=fake_fetch):
            export.build_export_folder(self.walkthrough_id, self.out_dir, brand=branding)

        images_dir = self.out_dir / "assets" / "images"
        self.assertEqual((images_dir / "logo_light.png").read_bytes(), b"\x89PNGFAKE")
        self.assertEqual((images_dir / "favicon.ico").read_bytes(), b"FAKEICO")
        # logo_dark_url reused the same URL as logo_light_url — downloaded once,
        # but still a real local file (not a dangling reference to the other's name)
        self.assertTrue((images_dir / "logo_light.png").is_file())

        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("acme.example", html)  # no external URL leaked into the output
        self.assertIn("assets/images/logo_light.png", html)
        self.assertIn("assets/images/favicon.ico", html)

    def test_built_in_profile_ships_our_assets_without_touching_the_network(self):
        """The built-in profile's three URLs are files in this repo, so a
        default export copies them off disk. Any fetch here would mean every
        download now depends on the marketing site being up."""
        from captcher import branding as branding_mod

        def explode(url):
            raise AssertionError(f"exported over the network for {url}")

        with patch("captcher.brandfetch.fetch_public_image", side_effect=explode):
            export.build_export_folder(self.walkthrough_id, self.out_dir,
                                       brand=branding_mod.my_brand_profile())

        images_dir = self.out_dir / "assets" / "images"
        self.assertEqual((images_dir / "logo_dark.svg").read_bytes(),
                         (ROOT / "static" / "logo-horizontal.svg").read_bytes())
        self.assertEqual((images_dir / "logo_light.svg").read_bytes(),
                         (ROOT / "static" / "logo-horizontal-light.svg").read_bytes())
        self.assertEqual((images_dir / "favicon.png").read_bytes(),
                         (ROOT / "static" / "favicon-32.png").read_bytes())

        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("shadowcapture.ai", html)  # no origin leaked into the output
        # The built-in palette is light (white surface), so the Ink lockup is the
        # one the chrome gets — the reversed one would be white on white.
        self.assertIn('mark(".intro-brand","assets/images/logo_light.svg"', html)
        self.assertIn('mark(".tb-left .brand","assets/images/logo_light.svg"', html)

    def test_bad_brand_image_url_fails_the_export(self):
        from captcher import author
        branding = {"brand_name": "Acme", "logo_light_url": "https://acme.example/missing.png",
                    "colors": {}, "fonts": {}}
        with patch("captcher.brandfetch.fetch_public_image",
                   side_effect=author.AuthorError("Could not fetch image (HTTP 404)")):
            with self.assertRaises(export.ExportError):
                export.build_export_folder(self.walkthrough_id, self.out_dir, brand=branding)


class BrandAssetTests(unittest.TestCase):
    """Which of a profile's two logo fields renders, and where our own come from.
    Pure functions — no database, no walkthrough fixture."""

    def test_logo_is_chosen_by_the_ground_it_lands_on(self):
        both = {"logo_light_url": "L", "logo_dark_url": "D"}
        self.assertEqual(export._logo_on(both, "#131320"), "D")
        self.assertEqual(export._logo_on(both, "#F2F2F7"), "L")
        # one field set is used on either ground, rather than falling back to
        # the wordmark
        self.assertEqual(export._logo_on({"logo_light_url": "L"}, "#0B0B10"), "L")
        self.assertEqual(export._logo_on({"logo_dark_url": "D"}, "#FFFFFF"), "D")
        # an unreadable or missing ground colour keeps the old preference
        self.assertEqual(export._logo_on(both, None), "L")
        self.assertEqual(export._logo_on({}, "#0B0B10"), "")

    def test_a_dead_favicon_is_skipped_but_a_dead_logo_fails(self):
        """The two losses are not the same size.

        A logo that silently vanishes ships someone else's training under a
        wordmark instead of their mark, and they hear about it from a customer
        — so that still fails loudly. A favicon is the browser tab icon, and
        losing a whole SCORM package over a decorative 404 is the worse
        outcome. It is also the one that happened: "Fetch brand" saved an
        unverified /favicon.ico guess, and every export of that profile died.
        """
        from captcher import author

        def dead(url):
            raise author.AuthorError("Could not fetch image (HTTP 404)")

        with tempfile.TemporaryDirectory() as d:
            images = Path(d) / "assets" / "images"
            with patch("captcher.brandfetch.fetch_public_image", side_effect=dead):
                out = export._download_branding_images(
                    {"brand_name": "Acme", "favicon_url": "https://acme.example/none.ico"},
                    images)
                self.assertEqual(out["favicon_url"], "")

                with self.assertRaises(export.ExportError) as raised:
                    export._download_branding_images(
                        {"brand_name": "Acme", "logo_light_url": "https://acme.example/none.png"},
                        images)

        # the message has to say whose profile and where to fix it
        self.assertIn("Acme", str(raised.exception))
        self.assertIn("Branding page", str(raised.exception))

    def test_own_asset_only_resolves_real_files_under_static(self):
        self.assertIsNone(export._own_asset("https://acme.example/logo.png"))
        self.assertIsNone(export._own_asset("https://shadowcapture.ai/assets/../server.py"))
        self.assertIsNone(export._own_asset("https://shadowcapture.ai/assets/nope.svg"))
        self.assertEqual(export._own_asset("https://shadowcapture.ai/assets/logo-horizontal.svg"),
                         ROOT / "static" / "logo-horizontal.svg")


class ZipExportTests(ExportFixtureMixin, unittest.TestCase):
    def test_zip_contains_the_full_folder_with_correct_asset_paths(self):
        zip_bytes = export.build_export_zip(self.walkthrough_id)
        self.assertGreater(len(zip_bytes), 0)

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.assertIsNone(zf.testzip())  # every member's CRC checks out
        names = set(zf.namelist())

        prefix = f"{self.slug}/"
        self.assertIn(prefix + "index.html", names)
        self.assertIn(prefix + PLAYER_CSS, names)
        self.assertIn(prefix + PLAYER_JS, names)
        self.assertIn(prefix + "assets/fonts/fonts.css", names)
        self.assertIn(prefix + "assets/images/logo.svg", names)

        html = zf.read(prefix + "index.html").decode("utf-8")
        refs = ASSET_REF_RE.findall(html)
        self.assertTrue(refs, "index.html referenced no assets/ paths")
        for rel in refs:
            self.assertIn(prefix + rel, names, f"index.html references {rel}, missing from zip")

    def test_zip_extracts_to_a_working_relative_layout(self):
        zip_bytes = export.build_export_zip(self.walkthrough_id)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(dest)
            index = dest / self.slug / "index.html"
            self.assertTrue(index.is_file())
            html = index.read_text(encoding="utf-8")
            for rel in ASSET_REF_RE.findall(html):
                self.assertTrue((dest / self.slug / rel).is_file(),
                                f"extracted zip missing referenced asset: {rel}")


if __name__ == "__main__":
    unittest.main()
