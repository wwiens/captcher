"""Reading .env — its own module so the data layer does not import the AI client.

`load_env` used to live in `author.py`, which was fine while `author` was the
only thing that needed configuration. It stopped being fine when `db.py` and
`blobs.py` needed it too: importing `author` pulls in the Anthropic SDK, so the
database layer could not be imported — or tested — without the whole authoring
stack behind it. One function, its own module, no dependencies.

TWO BEHAVIOURS THAT LOOK LIKE BUGS AND ARE NOT

**A real shell variable always wins.** Names already present in the environment
are never overwritten, so `ANTHROPIC_API_KEY=… captcher` beats the file.
That is what makes a deployment's injected configuration authoritative over a
`.env` that happened to get committed to an image.

**The first occurrence of a duplicated key wins**, because the file is read
top-down and each key is only set if absent. A second `ANTHROPIC_API_KEY` further
down the file is silently ignored. This has bitten this project during setup;
it is documented in .env.example for the same reason.

The file is read once, at import. Changes need a restart — the debug
auto-reloader only watches `.py` files.
"""
import os
from pathlib import Path

from . import config

_loaded = False


def env_file() -> Path:
    """The .env this server reads: CAPTCHER_HOME/.env, so ~/.captcher/.env by
    default. CAPTCHER_HOME itself therefore has to come from the real
    environment; setting it inside the file it locates would be circular."""
    return config.home() / ".env"


def load_env(path: Path = None, force: bool = False):
    """Minimal .env loader (stdlib only): KEY=VALUE lines, # comments, optional
    quotes. Never overrides variables already set in the environment.

    Idempotent, and cheap to call from anywhere: the work happens once unless
    `force` is set, so a module that needs configuration can call it at the top
    of every accessor without a file read per call.
    """
    global _loaded
    if _loaded and not force:
        return
    _loaded = True
    try:
        lines = Path(path or env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        # No .env is an ordinary state: a deployment sets real environment
        # variables instead. Callers discover a missing value, not a missing
        # file.
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        # `KEY=` with nothing after it is a blank waiting to be filled in, as
        # the generated file ships ANTHROPIC_API_KEY. It sets nothing, so it
        # cannot shadow a filled-in copy of the same key further down.
        if k and v and k not in os.environ:
            os.environ[k] = v


def commented_out(name: str, path: Path = None):
    """The line number of a `# NAME=value` line that carries a value, or None.

    The easiest mistake with this file: pasting the key after the `# ` of the
    example line. The value is there, the line is a comment, and the server
    says only that the key is missing. Naming the line turns that into a fix.
    """
    try:
        lines = Path(path or env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for n, line in enumerate(lines, 1):
        body = line.strip()
        if not body.startswith("#"):
            continue
        k, eq, v = body.lstrip("#").strip().partition("=")
        v = v.strip().strip("'\"")
        if eq and k.strip() == name and v and "..." not in v and "\u2026" not in v:
            return n
    return None


def missing_key_hint(name: str = "ANTHROPIC_API_KEY") -> str:
    """One sentence for when `name` is unset: where to put it, or which line
    already holds it behind a `#`."""
    where = env_file()
    n = commented_out(name)
    if n:
        return (f"Line {n} of {where} has your {name} but starts with #, so it is "
                f"ignored. Delete the # at the start of that line and restart.")
    return (f"Add {name}=<your key> to {where} and restart.")


load_env()
