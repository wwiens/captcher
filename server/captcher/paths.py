"""Filesystem containment checks.

One function, its own module, because getting this wrong is a path-traversal
bug and the codebase previously had five separate copies — four of which used
str.startswith(), which is not a containment check.
"""
from pathlib import Path


def is_within(path, root) -> bool:
    """True if `path` is inside `root` (or is `root` itself).

    Both sides are resolved first, so `..` segments and symlinks cannot be
    used to escape.

    Why not str(path).startswith(str(root)):

        root = /captures/add-points
        path = /captures/add-points-v2/secret.html    -> startswith() says yes

    A sibling directory whose name merely begins with the root's name passes
    the string test. That is not hypothetical here: walkthrough ids are
    de-duplicated by appending -2/-3 (server.handle_upload), and real capture
    folders on disk are named exactly `add-points` and `add-points-v2`.
    """
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def extension_checkout(app_root, override=""):
    """Resolve the recorder checkout without mistaking an empty folder for one.

    Explicit configuration wins. Otherwise walk up from `app_root` (the
    `captcher` package directory) looking for the monorepo's `extension/`
    folder with a manifest in it. Shared by the cross-component contract tests.
    """
    if override and str(override).strip():
        return Path(str(override).strip()).expanduser()
    root = Path(app_root).resolve()
    for parent in (root, *root.parents):
        candidate = parent / "extension"
        if (candidate / "manifest.json").is_file():
            return candidate
    return root.parent.parent / "extension"


def safe_join(root, rel):
    """Resolve `rel` under `root`, or None if it escapes.

    `rel` is untrusted (it comes off the URL), so the result is checked with
    is_within() rather than assumed.
    """
    root = Path(root)
    try:
        candidate = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    return candidate if is_within(candidate, root) else None
