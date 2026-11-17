"""Brand inference from a public URL — fetch a company site, infer a profile.

Powers "Import from a URL" on the Branding page. Split out of author.py, which
owned this alongside walkthrough authoring; the two share only the Claude API
client, imported from author.

The SSRF defences here are the reason this is its own module rather than a
loose helper: every hop is re-validated against a public-address allowlist
(_is_public_host), redirects are followed manually rather than by urllib
(_NoRedirect / _BRAND_OPENER), and both byte count and content-type are capped.
Anything calling out to a user-supplied URL should go through this module.
"""
import json
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

from . import langsmith_trace
from .author import (AuthorError, _model_from_env, call_claude, extract_json,
                    strip_data_uris)


# --------------------------------------------------------- brand inference
# "Import from a URL" on the Branding page: fetch a company's website and
# ask the model to infer a starting white-label brand profile (colours,
# fonts, logo/favicon, name, tagline) instead of the user picking each
# field by hand. The result is only ever handed back to the form for
# review — nothing is saved here.

BRAND_FETCH_TIMEOUT = 15        # seconds per hop
BRAND_MAX_REDIRECTS = 5
BRAND_MAX_BYTES = 900_000       # cap what we read off the wire
BRAND_MAX_HTML_CHARS = 45_000   # cap what we send the model


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Blocks urllib's automatic redirect following so each hop can be
    re-validated against the SSRF allowlist before it is fetched.

    Returning the 3xx response itself (rather than None) is what makes the
    manual loops in fetch_public_html / fetch_public_image see it. A handler
    that returns None hands the response to urllib's default error handler,
    which raises HTTPError — so the "if 300 <= status < 400" branches below
    were unreachable and every redirecting URL (http→https, a www. hop, a
    trailing-slash canonicalisation) failed as "Could not fetch (HTTP 301)".
    """
    def http_error_302(self, req, fp, code, msg, headers):
        return fp
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, *args, **kwargs):
        return None


_BRAND_OPENER = urllib.request.build_opener(_NoRedirect)


def _is_public_host(hostname: str) -> bool:
    """False for hostnames that resolve to a private/loopback/link-local/
    reserved address — keeps the brand-fetch feature from being used to
    probe internal services (SSRF) via a redirect or a crafted DNS entry."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        # gaierror is the usual failure, but herror and plain OSError are also
        # possible. Any resolution failure must fail closed, not escape as an
        # unhandled exception (which surfaced as a 500 rather than a clean
        # "could not fetch that URL").
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is judged as the
        # IPv4 address it wraps, which is what the socket will actually reach.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        # is_global is the umbrella test: it also covers the ranges the named
        # flags miss (100.64.0.0/10 shared address space, 192.0.0.0/24, the
        # benchmarking and documentation blocks). The named flags stay for
        # readability and for older Pythons where is_global is narrower.
        if (not ip.is_global or ip.is_private or ip.is_loopback
                or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return False
    return True


def _validate_fetch_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AuthorError("The URL must start with http:// or https://")
    if not parts.hostname:
        raise AuthorError("That doesn't look like a valid URL.")
    if not _is_public_host(parts.hostname):
        raise AuthorError("That URL resolves to a private or internal address, which isn't allowed.")


def fetch_public_html(start_url: str):
    """GET a public web page, following redirects manually so every hop is
    re-validated against the SSRF allowlist. Returns (final_url, html_text).
    Raises AuthorError on anything that should surface to the user."""
    url = start_url
    for _ in range(BRAND_MAX_REDIRECTS + 1):
        _validate_fetch_url(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CaptcherBrandFetch/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        })
        try:
            resp = _BRAND_OPENER.open(req, timeout=BRAND_FETCH_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise AuthorError(f"Could not fetch that URL (HTTP {e.code}).")
        except urllib.error.URLError as e:
            raise AuthorError(f"Could not reach that URL: {e.reason}")
        except OSError as e:
            raise AuthorError(f"Could not reach that URL: {e}")

        status = getattr(resp, "status", 200)
        if 300 <= status < 400:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise AuthorError("That URL redirected without a destination.")
            url = urllib.parse.urljoin(url, location)
            continue
        if status != 200:
            resp.close()
            raise AuthorError(f"Could not fetch that URL (HTTP {status}).")
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            resp.close()
            raise AuthorError("That URL didn't return an HTML page.")
        raw = resp.read(BRAND_MAX_BYTES + 1)[:BRAND_MAX_BYTES]
        final_url = resp.geturl()
        resp.close()
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        try:
            html_text = raw.decode(charset, errors="replace")
        except LookupError:
            html_text = raw.decode("utf-8", errors="replace")
        return final_url, html_text
    raise AuthorError("Too many redirects.")


BRAND_MAX_IMAGE_BYTES = 8_000_000   # cap for a downloaded logo/favicon


def fetch_public_image(start_url: str):
    """GET a public image (a brand logo or favicon URL), following redirects
    manually so every hop is re-validated against the same SSRF allowlist as
    fetch_public_html. Returns (final_url, content_type, data: bytes).
    Raises AuthorError on anything that should surface to the user."""
    url = start_url
    for _ in range(BRAND_MAX_REDIRECTS + 1):
        _validate_fetch_url(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CaptcherBrandFetch/1.0)",
            "Accept": "image/*,*/*;q=0.5",
        })
        try:
            resp = _BRAND_OPENER.open(req, timeout=BRAND_FETCH_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise AuthorError(f"Could not fetch image (HTTP {e.code}): {start_url}")
        except urllib.error.URLError as e:
            raise AuthorError(f"Could not reach image URL: {e.reason} ({start_url})")
        except OSError as e:
            raise AuthorError(f"Could not reach image URL: {e} ({start_url})")

        status = getattr(resp, "status", 200)
        if 300 <= status < 400:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise AuthorError(f"Image URL redirected without a destination: {start_url}")
            url = urllib.parse.urljoin(url, location)
            continue
        if status != 200:
            resp.close()
            raise AuthorError(f"Could not fetch image (HTTP {status}): {start_url}")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        clen = resp.headers.get("Content-Length")
        if clen and clen.isdigit() and int(clen) > BRAND_MAX_IMAGE_BYTES:
            resp.close()
            raise AuthorError(
                f"Image is too large (> {BRAND_MAX_IMAGE_BYTES // 1_000_000}MB): {start_url}")
        data = resp.read(BRAND_MAX_IMAGE_BYTES + 1)
        final_url = resp.geturl()
        resp.close()
        if len(data) > BRAND_MAX_IMAGE_BYTES:
            raise AuthorError(
                f"Image is too large (> {BRAND_MAX_IMAGE_BYTES // 1_000_000}MB): {start_url}")
        if not ctype.startswith("image/"):
            raise AuthorError(
                f"URL did not return an image (got {ctype or 'unknown content type'}): {start_url}")
        return final_url, ctype, data
    raise AuthorError(f"Too many redirects: {start_url}")


BRAND_PROBE_TIMEOUT = 6         # seconds per candidate, and they run together


def _probe_image(url: str, method: str):
    """One probe of `url` with `method`. True if it serves an image, False if
    it definitively does not, None if the method itself was refused and the
    other one is worth a try.

    Redirects are followed by the same manual, re-validated loop used elsewhere
    in this module, so a probe cannot be walked onto a private address.
    """
    here = url
    for _ in range(BRAND_MAX_REDIRECTS + 1):
        try:
            _validate_fetch_url(here)
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; CaptcherBrandFetch/1.0)",
                "Accept": "image/*,*/*;q=0.5",
            }
            if method == "GET":
                headers["Range"] = "bytes=0-0"   # the content type, not the logo
            resp = _BRAND_OPENER.open(
                urllib.request.Request(here, method=method, headers=headers),
                timeout=BRAND_PROBE_TIMEOUT)
        except urllib.error.HTTPError as e:
            # 405 and 501 mean "not this method"; some servers answer 403 to a
            # HEAD they would happily GET. Every other code is a real answer,
            # and the answer is no.
            return None if method == "HEAD" and e.code in (403, 405, 501) else False
        except Exception:
            # timeout, DNS, TLS, a blocked host — treat a HEAD failure as worth
            # one GET, and a GET failure as final.
            return None if method == "HEAD" else False

        status = getattr(resp, "status", 200)
        if 300 <= status < 400:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                return False
            here = urllib.parse.urljoin(here, location)
            continue
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        resp.close()
        return status in (200, 206) and ctype.startswith("image/")
    return False                                    # too many redirects


def image_url_resolves(url: str) -> bool:
    """Does this URL actually serve an image right now? Any doubt is a no.

    HEAD first: nothing here wants the bytes, only the answer.
    """
    verdict = _probe_image(url, "HEAD")
    if verdict is None:
        verdict = _probe_image(url, "GET")
    return bool(verdict)


def prune_unreachable_images(signals: dict) -> dict:
    """Drop every candidate image URL that does not resolve, in place.

    The candidates are what the page SAYS, plus one outright guess: the
    `/favicon.ico` fallback below is appended whether or not anything is served
    there. That guess used to reach a saved profile intact and only fail much
    later, at export time, where a 404 on a tab icon took a whole SCORM package
    with it. Confirm the candidates here instead, where the cost is a handful of
    HEAD requests and the consequence is one empty field.

    Probed concurrently: at most twelve candidates, and a slow host should not
    add its timeout to every other one.
    """
    from concurrent.futures import ThreadPoolExecutor

    urls = list(dict.fromkeys(signals["favicons"] + signals["logo_candidates"]))
    if not urls:
        return signals
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        ok = dict(zip(urls, pool.map(image_url_resolves, urls)))
    signals["favicons"] = [u for u in signals["favicons"] if ok.get(u)]
    signals["logo_candidates"] = [u for u in signals["logo_candidates"] if ok.get(u)]
    return signals


def _tag_attr(attrs: str, name: str) -> str:
    m = (re.search(name + r'''\s*=\s*"([^"]*)"''', attrs, re.I)
         or re.search(name + r"""\s*=\s*'([^']*)'""", attrs, re.I))
    return m.group(1) if m else ""


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_TAG_RE = re.compile(r"<meta\s+([^>]+)>", re.I)
_LINK_TAG_RE = re.compile(r"<link\s+([^>]+)>", re.I)
_IMG_TAG_RE = re.compile(r"<img\s+([^>]+)>", re.I)


def _abs_url(base: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.lower().startswith(("javascript:", "data:", "mailto:")):
        return ""
    try:
        return urllib.parse.urljoin(base, href)
    except ValueError:
        return ""


def extract_brand_signals(html: str, base_url: str) -> dict:
    """Pull cheap, reliable brand hints out of raw HTML: title, description,
    theme-color, and candidate favicon/logo image URLs. These candidate URLs
    are the ONLY image URLs the model is allowed to choose from — it may never
    invent one. Note what that does and does not buy: a returned URL is one the
    page named, which is not the same as one that resolves. The `/favicon.ico`
    entry below is not even that — it is a guess. `prune_unreachable_images`
    is what turns this list into URLs that answer."""
    head = html[:80_000]   # title/meta/link tags are always near the top
    signals = {"title": "", "description": "", "site_name": "",
               "theme_color": "", "favicons": [], "logo_candidates": []}

    m = _TITLE_RE.search(head)
    if m:
        signals["title"] = unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:200]

    for m in _META_TAG_RE.finditer(head):
        attrs = m.group(1)
        name = (_tag_attr(attrs, "name") or _tag_attr(attrs, "property")).lower()
        content = _tag_attr(attrs, "content").strip()
        if not content:
            continue
        if name in ("description", "og:description") and not signals["description"]:
            signals["description"] = content[:300]
        elif name == "og:site_name" and not signals["site_name"]:
            signals["site_name"] = content[:120]
        elif name == "theme-color" and not signals["theme_color"]:
            signals["theme_color"] = content[:20]

    for m in _LINK_TAG_RE.finditer(head):
        attrs = m.group(1)
        rel = _tag_attr(attrs, "rel").lower()
        href = _tag_attr(attrs, "href")
        if href and "icon" in rel:
            url = _abs_url(base_url, href)
            if url and url not in signals["favicons"]:
                signals["favicons"].append(url)
    # A guess, not a finding: most sites serve it, plenty do not, and nothing
    # here has checked. prune_unreachable_images settles it.
    fallback_favicon = _abs_url(base_url, "/favicon.ico")
    if fallback_favicon and fallback_favicon not in signals["favicons"]:
        signals["favicons"].append(fallback_favicon)
    signals["favicons"] = signals["favicons"][:6]

    for m in _IMG_TAG_RE.finditer(html[:250_000]):
        if len(signals["logo_candidates"]) >= 6:
            break
        attrs = m.group(1)
        cls = _tag_attr(attrs, "class").lower()
        alt = _tag_attr(attrs, "alt").lower()
        src = _tag_attr(attrs, "src") or _tag_attr(attrs, "data-src")
        if not src:
            continue
        if "logo" in cls or "logo" in alt or "logo" in src.lower():
            url = _abs_url(base_url, src)
            if url and url not in signals["logo_candidates"]:
                signals["logo_candidates"].append(url)

    return signals


def _trim_html_for_branding(html: str) -> str:
    """Like trim_html, but keeps <style> blocks and inline style attributes
    — colour and font-family declarations live there and are the main
    visual signal the model has to work with."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # Critical here, not just a cost saving: <style> blocks are kept, and
    # SingleFile inlines every image and font as base64 inside them. One
    # embedded font can exceed BRAND_MAX_HTML_CHARS on its own, truncating the
    # colour and font-family declarations we actually came for.
    html = strip_data_uris(html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html.strip()[:BRAND_MAX_HTML_CHARS]


BRAND_SYSTEM_PROMPT = """You infer a white-label brand profile for the Captcher walkthrough \
exporter from a company's real website, so a user doesn't have to pick colours and fonts by \
hand. You are given the page's trimmed HTML (structure, inline styles, and <style> blocks; \
scripts and comments removed) plus metadata extracted separately: title, description, \
theme-color, and candidate favicon/logo image URLs found on the page.

Infer:
- brand_name: the company's real name (from the title, site name, or logo alt text), \
title-cased, short.
- tagline: a short (under 12 words) tagline. Prefer the company's real tagline if it is \
visible on the page; otherwise write a concise one from the description.
- colors: a full 10-colour hex palette for these roles — primary, primary_hover, accent, \
background, surface, text, muted, border, success, error. Base "primary" on the company's \
dominant/most distinctive brand colour (buttons, header, logo colour) as seen in inline \
styles, CSS custom properties, or meta theme-color. primary_hover is a visibly darker shade \
of primary. accent is a secondary/highlight colour that contrasts with primary. \
background/surface/text/muted/border must form a coherent, accessible light UI theme (surface \
is usually near-white, text near-black, background a very light neutral — not necessarily the \
site's own literal page background if that wouldn't work as a UI backdrop). success is a green, \
error is a red, each tuned to feel consistent with the rest of the palette. Every value must be \
a 6-digit hex like #1E3B2F. text-on-surface and text-on-background must both read as high \
contrast (WCAG AA-ish).
- fonts.heading / fonts.body: real Google Fonts family names (exact spelling) that best match \
the site's typographic character, inferred from any font-family declarations visible in the \
HTML/CSS, or — if none are visible — from the overall visual style implied by the content and \
colours. Prefer a distinct heading font unless the site clearly uses one font throughout.
- logo_light_url: the best logo image URL for a light background, chosen ONLY from the given \
logo candidates — or "" if none look like a real logo.
- logo_dark_url: the best logo image URL for a dark background (e.g. a reversed/white variant), \
chosen ONLY from the given candidates — it is fine to reuse the same URL as logo_light_url, or \
return "" if you can't tell them apart.
- favicon_url: chosen ONLY from the given favicon candidates, preferring a real found \
<link rel="icon"> over the /favicon.ico guess when both are given.

Never invent an image URL: logo_light_url, logo_dark_url, and favicon_url must each be copied \
exactly from the provided candidate lists, or be "".

Return the brand profile in the required JSON format:
{"brand_name": "...", "tagline": "...", "logo_light_url": "...", "logo_dark_url": "...", \
"favicon_url": "...", "colors": {"primary": "#......", "primary_hover": "#......", \
"accent": "#......", "background": "#......", "surface": "#......", "text": "#......", \
"muted": "#......", "border": "#......", "success": "#......", "error": "#......"}, \
"fonts": {"heading": "...", "body": "..."}}"""

_COLOR_ROLES = ("primary", "primary_hover", "accent", "background", "surface",
                "text", "muted", "border", "success", "error")

# Structured-output schema mirroring the shape above. The hex format and the
# candidate-list rule stay in prose (JSON-schema `pattern` is not supported by
# structured outputs) — and the post-hoc URL allowlisting below stays
# regardless: that is security, not formatting.
BRAND_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_name": {"type": "string"},
        "tagline": {"type": "string"},
        "logo_light_url": {"type": "string"},
        "logo_dark_url": {"type": "string"},
        "favicon_url": {"type": "string"},
        "colors": {
            "type": "object",
            "properties": {role: {"type": "string"} for role in _COLOR_ROLES},
            "required": list(_COLOR_ROLES),
            "additionalProperties": False,
        },
        "fonts": {
            "type": "object",
            "properties": {"heading": {"type": "string"},
                           "body": {"type": "string"}},
            "required": ["heading", "body"],
            "additionalProperties": False,
        },
    },
    "required": ["brand_name", "tagline", "logo_light_url", "logo_dark_url",
                 "favicon_url", "colors", "fonts"],
    "additionalProperties": False,
}

# Brand inference reads a lot of HTML but writes a small fixed object; low
# effort with a small budget is the right shape (max_tokens covers thinking
# plus the response).
BRAND_EFFORT = "low"
BRAND_MAX_TOKENS = 8000


def _build_brand_user_prompt(requested_url, final_url, signals, trimmed_html):
    parts = [
        f"Requested URL: {requested_url}",
        f"Final URL after redirects: {final_url}",
        f"Page title: {signals['title'] or '(none)'}",
        f"Site name (og:site_name): {signals['site_name'] or '(none)'}",
        f"Meta description: {signals['description'] or '(none)'}",
        f"Meta theme-color: {signals['theme_color'] or '(none)'}",
        'Favicon candidates (favicon_url must be exactly one of these, or ""): '
        + json.dumps(signals["favicons"]),
        'Logo image candidates (logo_light_url / logo_dark_url must each be exactly one of '
        'these, or ""): ' + json.dumps(signals["logo_candidates"]),
        "--- PAGE HTML (scripts/comments stripped; inline styles and <style> blocks kept) ---",
        trimmed_html,
        "Now return the inferred brand profile JSON.",
    ]
    return "\n\n".join(parts)


def infer_branding_from_url(url: str, model: str = None) -> dict:
    """Fetch `url` and ask the model to infer a brand profile from it.
    Returns a dict shaped like a branding payload (brand_name, tagline,
    logo_light_url, logo_dark_url, favicon_url, colors, fonts) plus
    source_url. Raises AuthorError on any failure that should reach the user."""
    model = model or _model_from_env()
    with langsmith_trace.span(
        "brand inference", inputs={"url": url}, metadata={"model": model},
        tags=["branding"],
    ) as sp:
        final_url, html = fetch_public_html(url)
        signals = extract_brand_signals(html, final_url)
        # Offer the model only images that actually resolve, so a candidate it
        # picks is one an export can later download.
        prune_unreachable_images(signals)
        trimmed = _trim_html_for_branding(html)
        user = _build_brand_user_prompt(url, final_url, signals, trimmed)
        text = call_claude(BRAND_SYSTEM_PROMPT, user, model,
                           effort=BRAND_EFFORT, max_tokens=BRAND_MAX_TOKENS,
                           output_schema=BRAND_SCHEMA)
        data = extract_json(text)
        if not isinstance(data, dict):
            raise AuthorError("Model output was not a JSON object.")

        # Defense in depth: only ever accept image URLs we actually offered as
        # candidates — never trust the model not to hand back an invented one.
        allowed_favicons = set(signals["favicons"])
        allowed_logos = set(signals["logo_candidates"])
        if data.get("favicon_url") not in allowed_favicons:
            data["favicon_url"] = ""
        if data.get("logo_light_url") not in allowed_logos:
            data["logo_light_url"] = ""
        if data.get("logo_dark_url") not in allowed_logos:
            data["logo_dark_url"] = ""

        data["source_url"] = final_url
        sp.set_outputs(data)
        return data
