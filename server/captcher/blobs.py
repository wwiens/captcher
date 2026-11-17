#!/usr/bin/env python3
"""Captured screens, as files under `config.captures_dir()`.

A capture is frozen SingleFile HTML plus the PNGs the recording produced. It is
the bulk of a walkthrough by size, it is rewritten in place by the AI edit path
and by the render check, and it is served byte-for-byte to a browser. So it
lives on disk while the walkthrough definition lives in SQLite.

KEY SCHEME — the first segment is always the walkthrough's uuid, and a key is
a path relative to the captures folder:

    <uuid>/step-000/page.html
    <uuid>/step-000/screenshot.png      recorded baseline
    <uuid>/step-000/render.png          written by the render check
    <uuid>/step-000/thumb.webp          derived catalog card (thumb.png
                                        without WebP in Pillow)
    <uuid>/auto/<name>.html             pages the authoring pass synthesised
    <uuid>/walkthrough.json             the capture manifest, as recorded
    <uuid>/.history/<stamp>/<rel>       pre-edit page copies behind a save

The prefix is the id, and `prefix_for()` is the only place that knows it.

SAFETY. Keys reach the filesystem, and `rel` comes off the URL on the
/capture/ route, so `key_for` rejects any `..`, empty or backslash segment and
every path is resolved through `paths.safe_join` against the captures folder
before it is touched. Writes go to a temporary file in the same folder and are
moved into place with `os.replace`, so a reader never sees half a page.

The API is a small object-store one (put, get, get_text, exists, delete,
list_keys, delete_prefix, copy, copy_prefix), so callers never see paths.
`content_type` arguments are accepted and ignored: a file's type
comes from its extension, which is what the routes serve it by.
"""
import os
import shutil
import tempfile
from pathlib import Path

from . import config
from . import paths

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ico": "image/vnd.microsoft.icon",
}


class StorageError(RuntimeError):
    """A capture file could not be read or written.

    Carries a status so callers can tell a missing object (404, often
    expected) from anything else without parsing a message.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NotFound(StorageError):
    """No object at that key."""

    def __init__(self, message):
        super().__init__(message, status=404)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def prefix_for(walkthrough_id):
    """The key prefix holding one walkthrough's capture.

    The single place that knows the layout. Everything else composes keys from
    this.
    """
    return f"{walkthrough_id}/"


def key_for(walkthrough_id, rel):
    """The key for one file inside a capture, or None if `rel` escapes it.

    `rel` is untrusted — it arrives off the URL on the /capture/ route — so this
    is a containment check, not a join. A `..`, `.` or empty segment, a
    backslash or a NUL is rejected outright: besides escaping, it would let two
    different URLs address the same file.
    """
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    segments = rel.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None
    if "\\" in rel or "\x00" in rel:
        return None
    return prefix_for(walkthrough_id) + rel


def content_type_for(key):
    """The Content-Type for a key, from its extension."""
    dot = key.rfind(".")
    if dot == -1:
        return "application/octet-stream"
    return CONTENT_TYPES.get(key[dot:].lower(), "application/octet-stream")


def root() -> Path:
    return config.captures_dir()


def _path(key):
    """The file behind a key, or StorageError when the key would escape."""
    key = (key or "").lstrip("/")
    p = paths.safe_join(root(), key) if key else None
    if p is None or p == root().resolve():
        raise StorageError(f"unsafe capture key: {key!r}", status=400)
    return p


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def put(key, data, content_type=None):
    """Write one object, replacing whatever was there. Returns its size."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    target = _path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data)


def get(key):
    """Read one object's bytes. Raises NotFound when there is none."""
    p = _path(key)
    try:
        return p.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise NotFound(f"no capture object at {key!r}") from None


def get_text(key, encoding="utf-8"):
    """Read one object as text. Captured HTML is not always clean UTF-8, so
    decoding replaces rather than raising — a page with one bad byte should
    still play."""
    return get(key).decode(encoding, "replace")


def exists(key):
    """True when an object is present."""
    try:
        return _path(key).is_file()
    except StorageError:
        return False


def delete(key):
    """Remove one object. Deleting a key that is not there is not an error,
    which keeps cleanup paths idempotent."""
    try:
        _path(key).unlink()
    except FileNotFoundError:
        pass


def list_keys(prefix):
    """Every key under a prefix, sorted.

    Two callers (the export walk and the manifest build) depend on a stable
    order. Temporary files from an interrupted write are skipped.
    """
    base = _path(prefix.rstrip("/")) if prefix.strip("/") else root().resolve()
    if not base.is_dir():
        return []
    top = root().resolve()
    keys = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.startswith(".tmp-"):
                continue
            rel = Path(dirpath, name).relative_to(top).as_posix()
            if rel.startswith(prefix):
                keys.append(rel)
    return sorted(keys)


def delete_prefix(prefix):
    """Remove every object under a prefix. Returns how many."""
    keys = list_keys(prefix)
    for key in keys:
        delete(key)
    # Then the emptied folders, so a deleted walkthrough leaves nothing behind.
    if prefix.strip("/"):
        base = _path(prefix.rstrip("/"))
        if base.is_dir():
            shutil.rmtree(base, ignore_errors=True)
    return len(keys)


def copy(src_key, dest_key):
    """Copy one object. Raises NotFound when the source is missing."""
    src = _path(src_key)
    if not src.is_file():
        raise NotFound(f"no capture object at {src_key!r}")
    put(dest_key, src.read_bytes())


def copy_prefix(src_prefix, dest_prefix):
    """Copy every object under one prefix to another. Returns how many."""
    keys = list_keys(src_prefix)
    for key in keys:
        copy(key, dest_prefix + key[len(src_prefix):])
    return len(keys)


def health():
    """(ok, detail) for the startup banner. Never raises."""
    try:
        root().mkdir(parents=True, exist_ok=True)
        probe = root() / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True, str(root())
    except OSError as exc:
        return False, f"{root()}: {exc}"


if __name__ == "__main__":
    ok, detail = health()
    print(("ok   " if ok else "FAIL ") + detail)
    raise SystemExit(0 if ok else 1)
