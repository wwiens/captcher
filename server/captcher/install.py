"""First-run setup: the data folder, its .env, and the recorder extension.

`captcher` calls `prepare()` before it serves. Everything here is idempotent
and quiet once done, so it runs on every start rather than behind a separate
`captcher init` that people would have to know about.

THE .env. Created once, 0600, with every line commented out, so the one thing
a new user has to do — paste an Anthropic API key — has an obvious place to
go. An existing file is never touched.

THE EXTENSION. An installed package carries the recorder at
`captcher/extension/` (see hatch_build.py), inside a virtualenv path nobody
should have to dig for. `extension_folder()` copies it to
CAPTCHER_HOME/extension and returns that: a short path to pick in Chrome's
Load unpacked dialog, and the same path after every upgrade, so Chrome keeps
the extension (and its pairing) across them. The copy is replaced whenever the
bundled files change; it is not a place to edit. A source checkout has no
bundled copy, and its own `extension/` folder is used directly.
"""
import hashlib
import os
import shutil
import sys
from pathlib import Path

from . import config
from . import paths

PACKAGE = Path(__file__).resolve().parent
STAMP = ".bundle-hash"

ENV_TEMPLATE = """\
# Captcher settings. Lines starting with # are ignored; remove the # to use one.
# Restart `captcher` after changing this file.

# Your Anthropic API key, for the AI authoring pass and AI edits. Paste it
# right after the = below (no spaces, no quotes). Without it you can still
# record, browse, play and export.
# Get one at https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=

# Which Claude model writes walkthroughs.
# CAPTCHER_MODEL=claude-sonnet-5-5

# Listening beyond this computer (CAPTCHER_HOST=0.0.0.0, or `captcher --host`)
# requires an access token, entered once per browser at /unlock.
# CAPTCHER_ACCESS_TOKEN=choose-a-long-random-string

# Every other setting is listed in .env.example in the source repository.
"""


def prepare():
    """Create the data folder and its .env if missing. Returns the lines worth
    printing (what was created), which is nothing after the first run."""
    notes = []
    home = config.home()
    if not home.is_dir():
        home.mkdir(mode=0o700, parents=True)
        notes.append(f"Created the data folder {home}")
    env = home / ".env"
    if not env.exists():
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ENV_TEMPLATE)
        notes.append(f"Created {env} for your settings")
    return notes


def bundled_extension():
    """The extension shipped inside this package, or None in a source checkout."""
    folder = PACKAGE / "extension"
    return folder if (folder / "manifest.json").is_file() else None


def _digest(folder):
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(f.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


def extension_folder():
    """The folder to load into Chrome with Load unpacked.

    Installed: CAPTCHER_HOME/extension, refreshed from the package when the
    bundled files differ. Source checkout: the repository's extension/.
    Never raises; if the copy fails it falls back to the bundled folder.
    """
    bundled = bundled_extension()
    if bundled is None:
        return paths.extension_checkout(PACKAGE)
    target = config.home() / "extension"
    try:
        digest = _digest(bundled)
        stamp = target / STAMP
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
            return target
        staging = target.with_name(".extension-new")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(bundled, staging)
        (staging / STAMP).write_text(digest + "\n", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        return target
    except OSError:
        return bundled


# ---- the optional render and replay checks --------------------------------
# They drive a headless Chromium through Playwright, which is a large download
# most people never need, so it is an extra: `captcher[verify]`. One message
# says how to turn them on, wherever the question comes up (the review page,
# a check that fails to start, the pre-authoring render check).

# The install source is spelled out: until Captcher is on PyPI, a bare
# 'captcher[verify]' resolves to nothing. Keep it in step with the README's
# install line (the release checklist lists both).
INSTALL_SOURCE = "git+https://github.com/wwiens/captcher#subdirectory=server"

VERIFY_HELP = (
    "Render and replay checks need Playwright and a headless Chromium. Reinstall "
    "Captcher with its verify extra (uv tool install --force "
    f"\"captcher[verify] @ {INSTALL_SOURCE}\"), then run: captcher install-browser "
    "(a download of about 200 MB)")


def _playwright_browsers_dir():
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def verify_status():
    """"ready", "no-playwright" or "no-chromium". A best guess for the UI: the
    checks themselves still report a precise error if they cannot start."""
    import importlib.util
    try:
        if importlib.util.find_spec("playwright") is None:
            return "no-playwright"
    except (ImportError, ValueError):
        return "no-playwright"
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip() == "0":
        return "ready"      # browsers live inside the package; trust it
    folder = _playwright_browsers_dir()
    try:
        if any(p.name.startswith("chromium") for p in folder.iterdir()):
            return "ready"
    except OSError:
        pass
    return "no-chromium"


def install_browser():
    """`captcher install-browser`: Playwright's Chromium, into the same Python
    that runs Captcher, so a tool install needs no activated virtualenv.

    Only the headless shell: both checks launch Chromium headless, which is
    what Playwright runs the shell for. The full browser would add about
    380 MB that nothing here ever starts."""
    import subprocess
    if verify_status() == "no-playwright":
        print(VERIFY_HELP)
        return 1
    return subprocess.call([sys.executable, "-m", "playwright", "install",
                            "--only-shell", "chromium"])
