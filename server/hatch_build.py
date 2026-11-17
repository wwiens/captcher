"""Build hook: ship the Chrome recorder inside the Python package.

`uv tool install captcher` should give somebody everything they need, and the
recorder is half of Captcher. So the wheel carries the extension at
`captcher/extension/`, and `captcher.install.extension_folder()` copies it to
~/.captcher/extension, a short, stable path to choose in Chrome's
Load unpacked dialog.

Where the extension is found:
  - building from the repository: `../extension` (pyproject.toml is in server/);
  - building a wheel from an sdist: `extension/`, which the sdist build put
    there from `../extension`.

What ships is what `extension/package.sh` ships: no tests, no build scripts.
"""
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SKIP_TOP = {"tests", "scripts", "dist", "upstream", "node_modules",
            "package.sh", ".gitattributes", ".gitignore", "SOURCE-INSTRUCTIONS.txt"}
SKIP_NAMES = {".DS_Store", "__pycache__"}


class ExtensionHook(BuildHookInterface):
    PLUGIN_NAME = "captcher-extension"

    def initialize(self, version, build_data):
        # An editable install imports from the source tree, which already sits
        # beside ../extension (install.extension_folder finds it there).
        if version == "editable":
            return
        root = Path(self.root)
        inside, beside = root / "extension", root.parent / "extension"
        if (inside / "manifest.json").is_file():
            src = inside
        elif (beside / "manifest.json").is_file():
            src = beside
        else:
            raise RuntimeError(
                "The Chrome extension was not found next to server/ or inside the "
                "sdist. Build from a full checkout of the repository.")

        if self.target_name == "wheel":
            dest = "captcher/extension"
        elif src == inside:
            return          # an sdist rebuilt from an sdist already has it
        else:
            dest = "extension"

        for f in sorted(src.rglob("*")):
            rel = f.relative_to(src)
            if (not f.is_file() or rel.parts[0] in SKIP_TOP
                    or SKIP_NAMES.intersection(rel.parts)):
                continue
            build_data["force_include"][str(f)] = f"{dest}/{rel.as_posix()}"
