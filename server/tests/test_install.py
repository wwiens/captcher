"""First run and the `captcher` command (install.py, cli.py).

What a new user meets before anything else: the data folder and its .env, the
copy of the extension Chrome loads, and the command line. Each test points
CAPTCHER_HOME at its own empty folder so "first run" really is one.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from captcher import cli, envfile, install


class FreshHome(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="captcher-install-"))
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"CAPTCHER_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class PrepareTests(FreshHome):
    def test_first_run_creates_a_private_folder_and_an_env_with_a_blank_key(self):
        notes = install.prepare()
        env = self.home / ".env"
        self.assertTrue(env.is_file())
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        text = env.read_text(encoding="utf-8")
        # The key line is live and empty, so pasting after the = is all it
        # takes; every other setting is commented.
        live = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(live, ["ANTHROPIC_API_KEY="])
        self.assertEqual(len(notes), 2)

    def test_an_untouched_env_sets_nothing(self):
        install.prepare()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            envfile.load_env(force=True)
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_second_run_is_silent_and_never_rewrites_the_env(self):
        install.prepare()
        env = self.home / ".env"
        env.write_text("ANTHROPIC_API_KEY=sk-ant-mine\n", encoding="utf-8")
        self.assertEqual(install.prepare(), [])
        self.assertEqual(env.read_text(encoding="utf-8"), "ANTHROPIC_API_KEY=sk-ant-mine\n")


class ExtensionFolderTests(FreshHome):
    def _bundle(self, version="1.0.0"):
        bundle = self.tmp / "pkg" / "extension"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "manifest.json").write_text(f'{{"version": "{version}"}}', encoding="utf-8")
        (bundle / "sidepanel").mkdir(exist_ok=True)
        (bundle / "sidepanel" / "panel.js").write_text("// panel", encoding="utf-8")
        return bundle

    def test_a_source_checkout_uses_its_own_extension_folder(self):
        with mock.patch.object(install, "bundled_extension", return_value=None):
            folder = install.extension_folder()
        self.assertTrue((folder / "manifest.json").is_file())
        self.assertEqual(folder.name, "extension")
        self.assertFalse((self.home / "extension").exists())

    def test_an_installed_package_copies_the_extension_into_the_data_folder(self):
        bundle = self._bundle()
        with mock.patch.object(install, "bundled_extension", return_value=bundle):
            folder = install.extension_folder()
            self.assertEqual(folder, self.home / "extension")
            self.assertEqual((folder / "sidepanel" / "panel.js").read_text(), "// panel")
            # Unchanged bundle: the same folder, not copied again.
            (folder / "sidepanel" / "panel.js").write_text("// edited", encoding="utf-8")
            self.assertEqual(install.extension_folder(), folder)
            self.assertEqual((folder / "sidepanel" / "panel.js").read_text(), "// edited")
            # An upgrade changes the bundle: the copy is replaced in place.
            self._bundle("1.1.0")
            self.assertEqual(install.extension_folder(), folder)
            self.assertIn("1.1.0", (folder / "manifest.json").read_text())
            self.assertEqual((folder / "sidepanel" / "panel.js").read_text(), "// panel")


class VerifyStatusTests(unittest.TestCase):
    def test_without_playwright_the_checks_are_unavailable(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(install.verify_status(), "no-playwright")

    def test_playwright_without_chromium_says_so(self):
        empty = tempfile.mkdtemp()
        with mock.patch("importlib.util.find_spec", return_value=object()), \
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": empty}):
            self.assertEqual(install.verify_status(), "no-chromium")
            Path(empty, "chromium-1234").mkdir()
            self.assertEqual(install.verify_status(), "ready")

    def test_install_browser_explains_the_extra_when_playwright_is_missing(self):
        with mock.patch.object(install, "verify_status", return_value="no-playwright"), \
                mock.patch("builtins.print") as said:
            self.assertEqual(install.install_browser(), 1)
        self.assertIn("captcher[verify]", said.call_args[0][0])

    def test_the_help_gives_the_readmes_install_command(self):
        self.assertIn(install.VERIFY_INSTALL, install.VERIFY_HELP)
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertIn(install.VERIFY_INSTALL, readme)

    def test_install_browser_fetches_only_the_headless_shell(self):
        with mock.patch.object(install, "verify_status", return_value="no-chromium"), \
                mock.patch("subprocess.call", return_value=0) as call:
            self.assertEqual(install.install_browser(), 0)
        self.assertEqual(call.call_args[0][0][-3:], ["install", "--only-shell", "chromium"])

    def test_the_headless_shell_alone_counts_as_ready(self):
        empty = tempfile.mkdtemp()
        with mock.patch("importlib.util.find_spec", return_value=object()), \
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": empty}):
            Path(empty, "chromium_headless_shell-1255").mkdir()
            self.assertEqual(install.verify_status(), "ready")


class CommandLineTests(FreshHome):
    def _serve(self, argv):
        with mock.patch("captcher.server.main") as main:
            self.assertEqual(cli.main(argv), 0)
        return main.call_args.kwargs

    def test_plain_captcher_serves_on_8080_and_opens_a_browser(self):
        self.assertEqual(self._serve([]), {"port": 8080, "open_browser": True})

    def test_ports_given_every_way_that_has_worked(self):
        for argv in (["9000"], ["serve", "9000"], ["serve", "--port", "9000"], ["--port", "9000"]):
            self.assertEqual(self._serve(argv)["port"], 9000, argv)

    def test_no_browser_and_host(self):
        with mock.patch.dict(os.environ, {}):
            kw = self._serve(["--no-browser", "--host", "0.0.0.0"])
            self.assertEqual(os.environ["CAPTCHER_HOST"], "0.0.0.0")
        self.assertFalse(kw["open_browser"])

    def test_unknown_command_is_an_error_with_the_usage(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["frobnicate"]), 2)

    def test_extension_prints_the_folder(self):
        with mock.patch("builtins.print") as said:
            self.assertEqual(cli.main(["extension"]), 0)
        self.assertTrue((Path(str(said.call_args[0][0])) / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()


class EnvFileTests(FreshHome):
    def _load(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".env").write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            envfile.load_env(force=True)
            return os.environ.get("ANTHROPIC_API_KEY")

    def test_a_blank_line_does_not_shadow_a_filled_in_one_below_it(self):
        self.assertEqual(self._load("ANTHROPIC_API_KEY=\nANTHROPIC_API_KEY=sk-ant-x\n"), "sk-ant-x")

    def test_the_blank_line_filled_in(self):
        self.assertEqual(self._load("ANTHROPIC_API_KEY=sk-ant-x\n"), "sk-ant-x")

    def test_a_key_pasted_behind_a_hash_is_ignored_but_found(self):
        self.assertIsNone(self._load("# a comment\n# ANTHROPIC_API_KEY=sk-ant-real\n"))
        self.assertEqual(envfile.commented_out("ANTHROPIC_API_KEY"), 2)
        hint = envfile.missing_key_hint()
        self.assertIn("Line 2 of", hint)
        self.assertIn("Delete the #", hint)
        self.assertNotIn("sk-ant-real", hint)

    def test_the_example_placeholder_is_not_mistaken_for_a_key(self):
        self._load("# ANTHROPIC_API_KEY=sk-ant-...\n# ANTHROPIC_API_KEY=\n")
        self.assertIsNone(envfile.commented_out("ANTHROPIC_API_KEY"))
        self.assertIn("Add ANTHROPIC_API_KEY=", envfile.missing_key_hint())
