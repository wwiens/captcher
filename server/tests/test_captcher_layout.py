"""The rebranded app must retain recorder coverage after relocation."""
from pathlib import Path

from captcher import paths


def test_explicit_recorder_checkout_wins(tmp_path):
    chosen = tmp_path / 'explicit'
    assert paths.extension_checkout(tmp_path / 'server' / 'captcher', str(chosen)) == chosen


def test_monorepo_extension_is_found_from_the_package(tmp_path):
    pkg = tmp_path / 'server' / 'captcher'
    pkg.mkdir(parents=True)
    ext = tmp_path / 'extension'
    ext.mkdir()
    (ext / 'manifest.json').write_text('{}')
    assert paths.extension_checkout(pkg) == ext


def test_empty_extension_folder_is_not_a_checkout(tmp_path):
    pkg = tmp_path / 'server' / 'captcher'
    pkg.mkdir(parents=True)
    (tmp_path / 'extension').mkdir()
    assert paths.extension_checkout(pkg) == tmp_path / 'extension'


def test_missing_checkout_returns_expected_location(tmp_path):
    pkg = tmp_path / 'server' / 'captcher'
    assert paths.extension_checkout(pkg) == tmp_path / 'extension'


def test_interface_preloads_use_bundled_brand_faces():
    import re
    root = Path(__file__).resolve().parents[1] / "captcher"
    for page in (root / 'templates').glob('*.html'):
        for font in re.findall(r'rel="preload" href="(/static/fonts/[^"]+)"', page.read_text(encoding='utf-8')):
            assert font in ('/static/fonts/dm-sans-latin.woff2', '/static/fonts/outfit-latin-900.woff2'), (page.name, font)
            assert (root / font.lstrip('/')).is_file()


def test_server_and_cross_repository_tests_use_shared_resolver():
    root = Path(__file__).resolve().parents[1] / "captcher"
    # The server names the extension folder on /extension through
    # install.extension_folder(), which in a source checkout resolves it the
    # same way the cross-component tests do — never with a path of its own.
    server_src = (root / 'server.py').read_text()
    assert 'install.extension_folder()' in server_src
    assert 'paths.extension_checkout(PACKAGE)' in (root / 'install.py').read_text()
    assert 'captcher_ext' not in server_src
    for name in ('test_pairing.py', 'test_pairing_handshake.py',
                 'test_connect_surface.py', 'test_extension_contract.py',
                 'test_recorder_drag.py', 'test_recorder_effects.py',
                 'test_recorder_redaction.py', 'test_recorder_unlock.py'):
        assert 'paths.extension_checkout(' in (root.parent / 'tests' / name).read_text()
