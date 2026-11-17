"""SCORM packages: manifest at the archive root, and every declared file present.

The fixture is a walkthrough row plus capture objects rather than a directory
tree, but nothing about a SCORM package changed — an LMS still unpacks a zip
with imsmanifest.xml at its root.
"""
import io
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from captcher import export
from captcher import scorm
from conftest import new_member

from . import fixtures

ROOT = Path(__file__).resolve().parents[1] / "captcher"

ASSET_REF_RE = re.compile(r'(?:href|src)="(assets/[^"]+)"')

# Tracks whichever playback surface export ships (the popup player), since the
# folder export keeps the player's source filename.
PLAYER_JS = f"assets/{export.EXPORT_PLAYER}.js"

IMSCP_NS_2004 = "http://www.imsglobal.org/xsd/imscp_v1p1"
ADLCP_NS_2004 = "http://www.adlnet.org/xsd/adlcp_v1p3"
IMSCP_NS_12 = "http://www.imsproject.org/xsd/imscp_rootv1p1p2"
ADLCP_NS_12 = "http://www.adlnet.org/xsd/adlcp_rootv1p2"


class ScormFixtureMixin:
    """A synthetic walkthrough row plus its capture objects; see tests/fixtures.py."""

    def setUp(self):
        self.owner = new_member()
        self.row = fixtures.make_walkthrough(self.owner, title="Test Walkthrough")
        self.walkthrough_id = self.row["id"]
        self.slug = self.row["slug"]
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()


class ScormFolderExportTests(ScormFixtureMixin, unittest.TestCase):
    def test_builds_manifest_index_and_assets(self):
        result = scorm.build_export_scorm_folder(self.walkthrough_id, self.out_dir)
        self.assertEqual(result, self.out_dir)

        manifest_path = self.out_dir / "imsmanifest.xml"
        self.assertTrue(manifest_path.is_file())
        index = self.out_dir / "index.html"
        self.assertTrue(index.is_file())
        self.assertTrue((self.out_dir / "assets" / "scorm-api.js").is_file())

        html = index.read_text(encoding="utf-8")
        self.assertNotIn("/static/", html)
        # a version flag must be set BEFORE scorm-api.js, which itself must be
        # wired in BEFORE the player script so it can set window.RC_RESUME in time
        self.assertLess(html.index("window.RC_SCORM_VERSION"),
                        html.index('src="assets/scorm-api.js"'))
        self.assertIn('window.RC_SCORM_VERSION="2004"', html)
        self.assertLess(html.index('src="assets/scorm-api.js"'),
                        html.index(f'src="{PLAYER_JS}"'))

    def test_manifest_is_well_formed_and_references_every_file(self):
        scorm.build_export_scorm_folder(self.walkthrough_id, self.out_dir)
        manifest_path = self.out_dir / "imsmanifest.xml"
        root = ET.fromstring(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(root.tag, f"{{{IMSCP_NS_2004}}}manifest")
        resource = root.find(f"{{{IMSCP_NS_2004}}}resources/{{{IMSCP_NS_2004}}}resource")
        self.assertIsNotNone(resource)
        self.assertEqual(resource.get(f"{{{ADLCP_NS_2004}}}scormType"), "sco")
        self.assertEqual(resource.get("href"), "index.html")

        item = root.find(f"{{{IMSCP_NS_2004}}}organizations/{{{IMSCP_NS_2004}}}organization/{{{IMSCP_NS_2004}}}item")
        self.assertIsNotNone(item)
        self.assertEqual(item.get("identifierref"), resource.get("identifier"))

        declared = {f.get("href") for f in resource.findall(f"{{{IMSCP_NS_2004}}}file")}
        self.assertIn("index.html", declared)
        self.assertIn(PLAYER_JS, declared)
        self.assertIn("assets/scorm-api.js", declared)
        # every declared file actually exists on disk, and vice versa
        for rel in declared:
            self.assertTrue((self.out_dir / rel).is_file(), f"manifest references missing file: {rel}")
        on_disk = {str(p.relative_to(self.out_dir)).replace("\\", "/")
                   for p in self.out_dir.rglob("*") if p.is_file() and p.name != "imsmanifest.xml"}
        self.assertEqual(declared, on_disk)


class Scorm12FolderExportTests(ScormFixtureMixin, unittest.TestCase):
    def test_builds_manifest_with_1p2_schema(self):
        scorm.build_export_scorm_folder(self.walkthrough_id, self.out_dir, version="1.2")

        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('window.RC_SCORM_VERSION="1.2"', html)

        manifest_path = self.out_dir / "imsmanifest.xml"
        root = ET.fromstring(manifest_path.read_text(encoding="utf-8"))

        # SCORM 1.2 uses a different IMS CP namespace and a lowercase
        # adlcp:scormtype attribute — distinct from the 2004 schema entirely
        self.assertEqual(root.tag, f"{{{IMSCP_NS_12}}}manifest")
        schemaversion = root.find(f"{{{IMSCP_NS_12}}}metadata/{{{IMSCP_NS_12}}}schemaversion")
        self.assertEqual(schemaversion.text, "1.2")

        resource = root.find(f"{{{IMSCP_NS_12}}}resources/{{{IMSCP_NS_12}}}resource")
        self.assertIsNotNone(resource)
        self.assertEqual(resource.get(f"{{{ADLCP_NS_12}}}scormtype"), "sco")
        self.assertIsNone(resource.get(f"{{{ADLCP_NS_2004}}}scormType"))

        declared = {f.get("href") for f in resource.findall(f"{{{IMSCP_NS_12}}}file")}
        self.assertIn("index.html", declared)
        self.assertIn("assets/scorm-api.js", declared)

    def test_unknown_version_rejected(self):
        with self.assertRaises(export.ExportError):
            scorm.build_export_scorm_folder(self.walkthrough_id, self.out_dir, version="3.0")


class ScormZipExportTests(ScormFixtureMixin, unittest.TestCase):
    def test_manifest_sits_at_the_zip_root(self):
        zip_bytes = scorm.build_export_scorm_zip(self.walkthrough_id)
        self.assertGreater(len(zip_bytes), 0)

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.assertIsNone(zf.testzip())
        names = set(zf.namelist())

        # unlike export.build_export_zip, nothing is namespaced under
        # "<walkthrough_id>/" — SCORM requires imsmanifest.xml at the root
        self.assertIn("imsmanifest.xml", names)
        self.assertIn("index.html", names)
        self.assertIn(PLAYER_JS, names)
        self.assertIn("assets/scorm-api.js", names)
        self.assertNotIn(f"{self.slug}/imsmanifest.xml", names)

        html = zf.read("index.html").decode("utf-8")
        for rel in ASSET_REF_RE.findall(html):
            self.assertIn(rel, names, f"index.html references {rel}, missing from zip")

    def test_scorm12_zip_builds_too(self):
        zip_bytes = scorm.build_export_scorm_zip(self.walkthrough_id, version="1.2")
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.assertIsNone(zf.testzip())
        manifest = zf.read("imsmanifest.xml").decode("utf-8")
        self.assertIn(IMSCP_NS_12, manifest)


if __name__ == "__main__":
    unittest.main()
