"""Where Captcher keeps its data.

Everything a server writes lives under one folder, CAPTCHER_HOME, which
defaults to ~/.captcher:

    ~/.captcher/
    ├── .env               settings: ANTHROPIC_API_KEY and any CAPTCHER_*
    ├── session_secret     signs the browser's cookies (created on first run)
    ├── captcher.db        SQLite (WAL mode; -wal and -shm files beside it)
    ├── captures/<id>/     every captured page and asset, by walkthrough id
    └── extension/         the recorder, copied here from an installed package

CAPTCHER_HOME is read from the real environment only, never from .env,
because it is what says where .env is.

Read through the environment on every call rather than cached at import, so a
test can point a server at a temporary folder by setting CAPTCHER_HOME and
calling db.reset().
"""
import os
from pathlib import Path


def home() -> Path:
    """The data folder. Not created here; the writers create what they need."""
    raw = (os.environ.get("CAPTCHER_HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".captcher"


def database_path() -> Path:
    """The SQLite file. CAPTCHER_DB overrides it, for a database kept apart
    from the captures."""
    from . import envfile
    envfile.load_env()
    raw = (os.environ.get("CAPTCHER_DB") or "").strip()
    return Path(raw).expanduser() if raw else home() / "captcher.db"


def captures_dir() -> Path:
    """The root every capture key is relative to."""
    return home() / "captures"
