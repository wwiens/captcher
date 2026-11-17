"""Minimal file-backed template loader.

The app's pages used to live as ~2,000 lines of HTML/CSS/JS embedded in
server.py string literals. They now live in templates/ as real .html files, so
editors syntax-highlight them and CSS/JS braces need no escaping.

Substitution is deliberately dumb: `__PLACEHOLDER__` markers replaced by
str.replace. That convention was already in use for the largest blobs
(REVIEW_PAGE, BRANDING_PAGE) precisely because f-string `{{`/`}}` escaping
fights embedded JavaScript. Everything now uses it.

    render("login.html", ERROR="", EMAIL="you@example.com")

Placeholder names are bare in the file (`__ERROR__`) and passed as keyword
arguments without the underscores (`ERROR=...`). Values are inserted verbatim:
callers are responsible for escaping, exactly as before.

In debug mode files are re-read when their mtime changes, so template edits
show up on refresh without a restart.
"""
import json
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def script_json(obj) -> str:
    """JSON safe to interpolate inside a <script> block.

    json.dumps escapes neither `<` nor `/`, so a string value containing
    `</script>` would close the block early and everything after it would be
    parsed as markup. Escaping `<` as \\u003c is enough to prevent that and
    still parses as the same JSON value.

    Reachable, not theoretical: the Branding page's "import from a URL" has
    Claude infer brand_name/tagline from an attacker-controlled page, and
    validate_branding only length-caps those fields.
    """
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")

# name -> (mtime, text)
_CACHE = {}
_DEBUG = True


def set_debug(flag: bool) -> None:
    """When False, templates are read once and cached for the process."""
    global _DEBUG
    _DEBUG = bool(flag)


def load(name: str) -> str:
    """Return the raw text of templates/<name>, cached by mtime."""
    path = TEMPLATE_DIR / name
    cached = _CACHE.get(name)
    if cached is not None and not _DEBUG:
        return cached[1]
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise FileNotFoundError(f"template not found: templates/{name}") from e
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = path.read_text(encoding="utf-8")
    _CACHE[name] = (mtime, text)
    return text


def render(name: str, **subs) -> str:
    """Load templates/<name> and replace __KEY__ for each key=value given."""
    out = load(name)
    for key, value in subs.items():
        out = out.replace(f"__{key}__", "" if value is None else str(value))
    return out


def placeholders(name: str):
    """The __UPPER_CASE__ markers present in a template. Used by tests to keep
    templates and their call sites from drifting apart."""
    import re
    return sorted(set(re.findall(r"__([A-Z][A-Z0-9_]*)__", load(name))))
