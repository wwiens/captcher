"""CONTRACT.md, as a test.

Once the recorder is in the Chrome Web Store this repository is the only half
of the pair that can be fixed, so the strings and shapes it shares with the
extension are frozen. Five of the eight fail with no error on either side: the
user sees a feature that stopped working and nothing anywhere says why. That
silence is the whole reason for this file.

Each test extracts the value from the EXTENSION's source and asserts this
repository still matches it, rather than pinning a literal in both places —
pinning a literal is what lets one side be renamed on its own, which is the
failure this is meant to catch.

Nothing here needs a database or a running server; it is all source comparison,
so it is cheap enough to run on every commit.

If a test in this file fails, the answer is almost never to update the test.
Read CONTRACT.md.
"""
import ast
import os
import re
import unittest
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1] / "captcher"
from captcher import paths

EXTENSION_DIR = paths.extension_checkout(SERVER_ROOT, os.environ.get("CAPTCHER_EXTENSION_DIR", ""))

# A checkout with the extension missing cannot verify any of this. Locally that
# is a skip; in CI it has to be a failure, or the guard silently stops guarding
# the moment the layout changes — which is exactly how a contract test becomes
# decoration. Set CAPTCHER_REQUIRE_EXTENSION=1 in CI.
REQUIRE_EXTENSION = os.environ.get("CAPTCHER_REQUIRE_EXTENSION", "").strip() not in ("", "0", "false")


class ExtensionContractTests(unittest.TestCase):

    def ext(self, name):
        path = EXTENSION_DIR / name
        if not path.is_file():
            message = (f"{name} not found at {path} — the extension repo must be "
                       f"checked out beside this one for the contract tests to run")
            if REQUIRE_EXTENSION:
                self.fail(message)
            self.skipTest(message)
        return path.read_text(encoding="utf-8")

    def server(self, name):
        return (SERVER_ROOT / name).read_text(encoding="utf-8")

    def accepted_schemas(self):
        """_ACCEPTED_SCHEMAS, read out of ingest.py rather than imported.

        Importing it drags in the store layer, a database driver and an
        environment — for a tuple of three literals. A contract test that only
        runs where production runs is one that stops being run.
        """
        tree = ast.parse(self.server("ingest.py"))
        constants = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        # a tuple naming an earlier constant, e.g.
                        # (CAPTURE_SCHEMA, None, ""), which literal_eval will
                        # not resolve — substitute what is already known.
                        if isinstance(node.value, ast.Tuple):
                            resolved = []
                            for element in node.value.elts:
                                if isinstance(element, ast.Name):
                                    resolved.append(constants.get(element.id))
                                else:
                                    resolved.append(ast.literal_eval(element))
                            constants[target.id] = tuple(resolved)
        self.assertIn("_ACCEPTED_SCHEMAS", constants,
                      "ingest.py no longer defines _ACCEPTED_SCHEMAS")
        return constants["_ACCEPTED_SCHEMAS"]

    # -- 1. the ping ------------------------------------------------------
    def test_ping_route_and_key_still_exist(self):
        """The extension decides whether an origin is Captcher by asking
        this route and reading this key. Rename either and every installed
        recorder stops recognising the real server, with nothing logged."""
        panel = self.ext("sidepanel/panel.js")
        route = re.search(r'\$\{origin\}(/api/extension/ping)', panel).group(1)
        server = self.server("server.py")
        self.assertIn(f'path == "{route}"', server,
                      f"the extension asks {route}; this server no longer answers it")
        self.assertIn('"captcher": True', server,
                      "the extension reads the `captcher` key of the ping answer")

    def test_ping_still_advertises_the_pairing_version(self):
        """From recorder 0.1.0 this is read, and it is the only channel through
        which this server can tell an installed extension anything."""
        self.assertIn('"pairing": PAIRING_API_VERSION', self.server("server.py"))
        self.assertIn("body.pairing", self.ext("sidepanel/panel.js"))

    def test_the_optional_ping_fields_are_still_sent_under_the_names_read(self):
        """Additive and unset by default, so nothing breaks when they are
        absent — but they are useless if the two ends spell them differently,
        and that would only be discovered during the incident they exist for."""
        panel, server = self.ext("sidepanel/panel.js"), self.server("server.py")
        for key in ("notice", "minExtensionVersion", "maxUploadBytes"):
            self.assertIn(f'body.{key}', panel, f"the extension no longer reads {key}")
            self.assertIn(f'"{key}"', server, f"this server no longer sends {key}")

    # -- 3. claim ---------------------------------------------------------
    def test_claim_route_and_response_keys(self):
        """Break these and no browser can pair again."""
        background = self.ext("background.js")
        route = re.search(r'\$\{origin\}(/api/extension/claim)', background).group(1)
        server = self.server("server.py")
        self.assertIn(f'"{route}"', server,
                      f"the extension posts to {route}; this server no longer names it")
        self.assertIn("data.token", background)
        self.assertIn('"token"', server, "the extension reads `token` from the claim answer")

    # -- 4. upload --------------------------------------------------------
    def test_upload_route_field_and_auth_scheme(self):
        """Break any of these and every recording fails at Send, on a
        walkthrough the panel holds only in memory."""
        panel = self.ext("sidepanel/panel.js")
        server = self.server("server.py")
        route = re.search(r'\$\{url\}(/api/upload)', panel).group(1)
        field = re.search(r'form\.append\("([a-z_]+)", zip', panel).group(1)
        self.assertIn(f'"{route}"', server,
                      f"the extension posts captures to {route}; this server no longer names it")
        self.assertIn(f'"{field}"', server, f"the extension sends the ZIP as `{field}`")
        self.assertIn("Bearer ${token}", panel)
        self.assertIn("Bearer ", server, "the extension authenticates with a Bearer token")

    def test_capture_log_field_and_schema(self):
        """Optional on both sides, so a rename fails silently: uploads keep
        working and the troubleshooting logs just stop arriving."""
        panel = self.ext("sidepanel/panel.js")
        field = re.search(r'form\.append\("([a-z_]+)", new Blob\(\[JSON\.stringify\(sessionLog', panel)
        self.assertIsNotNone(field, "the extension no longer sends its capture log")
        self.assertIn(f'fields.get("{field.group(1)}")', self.server("server.py"))
        schema = re.search(r'CAPTURE_LOG_SCHEMA = "([^"]+)"', panel).group(1)
        self.assertIn(f'"{schema}"', self.server("diagnostics.py"))

    def test_upload_response_keys_the_extension_reads(self):
        """These degrade rather than break, which is why they are easy to
        rename without noticing."""
        panel, server = self.ext("sidepanel/panel.js"), self.server("server.py")
        for key in ("walkthrough_id", "player_url"):
            self.assertIn(f"data.{key}", panel)
            self.assertIn(f'"{key}"', server, f"the extension reads {key} from the upload answer")

    # -- 6. the capture schema -------------------------------------------
    def test_the_schema_the_extension_sends_is_still_accepted(self):
        """The one contract that is properly versioned, and the one whose
        failure mode is worst: a server that drops @1 rejects every installed
        recorder with a 400 that blames the extension for being too new.

        Additive fields on @1 are safe in both directions and are expected.
        Removing @1 is not, and will not be for years — a Web Store extension
        outlives the release that replaces it by as long as its users take.
        """
        accepted = self.accepted_schemas()
        sent = set(re.findall(r'schema: "([a-z0-9@.-]+)"', self.ext("sidepanel/panel.js")))
        self.assertTrue(sent, "the extension no longer declares a schema in its manifest")
        for schema in sent:
            self.assertIn(schema, accepted,
                          f"the extension sends {schema!r} and this server would reject it")

    def test_the_extension_sends_exactly_one_schema(self):
        """Two would mean a half-finished migration, and this test is where
        that should be noticed rather than in ingest."""
        sent = set(re.findall(r'schema: "([a-z0-9@.-]+)"', self.ext("sidepanel/panel.js")))
        self.assertEqual(len(sent), 1, f"the extension declares {sorted(sent)}")

    # -- knowing what is installed ---------------------------------------
    def test_the_recorder_still_identifies_its_build(self):
        """Not a contract the server depends on, but the only way to learn
        which builds are in use before deciding whether an old one matters."""
        self.assertIn("X-Captcher-Recorder", self.ext("sidepanel/panel.js"))

    # -- the document -----------------------------------------------------
    def test_contract_md_exists_and_names_every_frozen_thing(self):
        """The list is only useful if it is where the person about to break it
        will look, and only true if it stays complete."""
        doc = (SERVER_ROOT.parent.parent / "docs" / "contract.md")
        self.assertTrue(doc.is_file(), "CONTRACT.md is what this test file enforces")
        text = doc.read_text(encoding="utf-8")
        for needle in ("/api/extension/ping", "/api/extension/claim", "/api/upload",
                       "capture_zip", "capture_log", "page-capture-walkthrough@1",
                       "captcher:capture-present", "minExtensionVersion"):
            self.assertIn(needle, text, f"CONTRACT.md no longer mentions {needle}")


if __name__ == "__main__":
    unittest.main()
