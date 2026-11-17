#!/usr/bin/env python3
"""
Export a walkthrough as a single standalone HTML file.

The exported file bundles the player (HTML/CSS/JS), the bridge, the fonts
(base64), the walkthrough definition, and every page snapshot the walkthrough's steps
reference. It plays fully offline (double-click, email, LMS upload) in
"Show me" and "You try" modes; edit mode is server-only and is hidden.

Every export ships the popup player (static/player2.*) — the same playback
surface as /player/<id> and the public examples, where the step card floats
next to the step's target. The classic side-panel player (static/player.*)
survives only as the authoring surface behind /player/<id>?edit=1; nothing a
learner sees is built from it. See EXPORT_PLAYER below.

How it fits together:
  - player2.html is the shell; its /static/ references are inlined.
  - The walkthrough def + preprocessed page HTML are embedded as JSON in a
    <script id="sc-embed" type="application/json"> block ("<" is escaped
    to \\u003c so page content can never terminate the block).
  - player2.js detects the sc-embed block and switches to embedded mode:
    no fetches, pages load into the iframe via srcdoc.
  - Each embedded page gets (a) a freeze shim at the TOP of <head> that
    neuters fetch/XHR/sendBeacon/WebSocket before any product script runs
    (auto/ snapshots keep the product's live JS), and (b) bridge.js inlined
    before </body>, exactly where the server would inject it.

A second format exports the same walkthrough as a folder instead: index.html
plus an assets/ folder holding player2.css, player2.js, fonts, and every brand
logo/favicon — downloaded once at export time and referenced by a local
relative path, so the folder needs no internet access to render correctly.
It can be zipped for download (see build_export_zip / --zip below).

The walkthrough is addressed by its uuid — the definition is a row in
`walkthroughs`, the page snapshots are objects under `<uuid>/` in the captures
bucket — but every *filename* an export produces is built from the row's slug.
See export_basename() for why the two are different strings.

Usage:
  python3 export.py <walkthrough-id> [output.html]     (default: exports/<slug>.html)
  python3 export.py <walkthrough-id> --profile <brand-id>   (apply a saved brand)
  python3 export.py <walkthrough-id> --folder [output-dir]  (folder export: index.html + assets/)
  python3 export.py <walkthrough-id> --zip [output.zip]     (folder export, packed as a .zip)
Or from server.py:  GET /api/walkthroughs/<id>/export[?profile=<brand-id>]
                    GET /api/walkthroughs/<id>/export-zip[?profile=<brand-id>]
"""
import base64
import io
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from . import branding
from . import store
from . import templates

ROOT = Path(__file__).resolve().parent
# static/ is still read off the filesystem, deliberately. The player shell, its
# CSS/JS, the fonts and the logo are application code shipped with this repo and
# versioned with it — they are the same bytes for every account, and they change
# when the code changes, not when a user does anything. Only user data moved to
# Postgres and Storage; putting the player in a bucket would mean a deploy could
# ship a shell whose stylesheet is a release behind, for no benefit.
STATIC_DIR = ROOT / "static"


class ExportError(Exception):
    pass


# The capture engine (SingleFile) stamps every serialized iframe with exactly
# this sandbox prefix — deliberately without allow-same-origin, which gives
# the frame an opaque origin the bridge cannot reach into (contentDocument is
# null), so no frame target could ever resolve. unlock_frames() adds
# allow-same-origin at serve/export time. Scripts inside captured frames are
# stripped at capture time (blockScripts), and the frame's content is part of
# the same capture that already runs same-origin at the top level — so this
# widens reach for the bridge, not for the captured page (see the /capture/
# route note in server.py for that standing gap). The byte-exact prefix means
# an author page's own sandbox attribute is never touched.
FRAME_SANDBOX_STAMP = 'sandbox="allow-popups allow-top-navigation-by-user-activation'
FRAME_SANDBOX_OPEN = 'sandbox="allow-same-origin allow-popups allow-top-navigation-by-user-activation'


def unlock_frames(html: str) -> str:
    """Let the bridge see into captured same-origin iframes (see above)."""
    return html.replace(FRAME_SANDBOX_STAMP, FRAME_SANDBOX_OPEN)


# Runs before any product script: swallow every network primitive the same
# way bridge.js does, but EARLY — auto/ snapshots fire fetch("/api/…") at
# parse time, long before an end-of-body bridge could intervene. Requests
# must hang (never settle), not fail: a rejection would trigger the
# product's own error toasts on an otherwise frozen page.
FREEZE_SHIM = (
    "<script>/* rc-export: freeze the network before product scripts run */"
    "(function(){try{"
    "window.fetch=function(){return new Promise(function(){})};"
    "if(window.XMLHttpRequest){window.XMLHttpRequest.prototype.open=function(){};"
    "window.XMLHttpRequest.prototype.send=function(){}}"
    "if(navigator.sendBeacon)navigator.sendBeacon=function(){return true};"
    "if(window.WebSocket)window.WebSocket=function(){return{send:function(){},"
    "close:function(){}}};"
    "}catch(e){}})();</script>"
)

# Bytes twin for the live /capture/ route: server.send_file works on raw bytes
# (deliberately — no decode/encode round-trip on multi-MB snapshots), so it
# cannot reuse preprocess_page's str-domain injection. Same placement rule.
FREEZE_SHIM_BYTES = FREEZE_SHIM.encode("ascii")

# WHERE THE SHIM GOES: after the document's prologue — any BOM, whitespace,
# comments, the doctype, and the <html> and <head> start tags — and before
# anything else, which puts it ahead of every product script.
#
# It used to go after the first <head…> anywhere in the page, else at byte 0.
# Both halves were wrong for SingleFile output, which drops optional tags and
# so usually has no <head> at all:
#
#   * byte 0 is BEFORE `<!DOCTYPE html>`. A doctype that is not the first
#     token is ignored, so every such page rendered in quirks mode, in the
#     player, the render check and every export. Quirks layout is different
#     enough to break real pages: a HubSpot deal record's sidebar cards grew
#     by a screen each and fell out of view (FIELD-NOTES.md 2026-10-06).
#   * "anywhere" reaches into the body: first a <header> a megabyte in (hence
#     the whitespace-or-`>` guard), and an iframe's inlined srcdoc can carry a
#     <head> of its own.
#
# Matching only at \A is what keeps it out of the body. Attributes follow the
# tokenizer: quoted values may contain `>`, unquoted ones end at space or `>`.
_PROLOGUE_SRC = (
    r"\A(?:BOM|\s+|<!--.*?-->|<!doctype[^>]*>"
    r"|<(?:html|head)(?:\s+[^\s\"'>/=]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*\s*/?>)*"
)
_PROLOGUE = re.compile(_PROLOGUE_SRC.replace("BOM", "\ufeff"), re.I | re.S)
_PROLOGUE_BYTES = re.compile(_PROLOGUE_SRC.replace("BOM", r"\xef\xbb\xbf").encode("ascii"),
                             re.I | re.S)


def freeze_page_bytes(body: bytes) -> bytes:
    """FREEZE_SHIM right after the document prologue (see above)."""
    at = _PROLOGUE_BYTES.match(body).end()
    return body[:at] + FREEZE_SHIM_BYTES + body[at:]


def freeze_page(html: str) -> str:
    """The str twin of freeze_page_bytes, for the export path."""
    at = _PROLOGUE.match(html).end()
    return html[:at] + FREEZE_SHIM + html[at:]


def _read_static(name: str) -> str:
    p = STATIC_DIR / name
    if not p.is_file():
        raise ExportError(f"missing player asset: static/{name}")
    return p.read_text(encoding="utf-8")


def _no_close_script(source: str, name: str) -> str:
    """Inlined JS/CSS must not contain '</script>' / '</style>'."""
    if re.search(r"</\s*(script|style)", source, re.I):
        raise ExportError(f"static/{name} contains a closing script/style tag — "
                          f"cannot be inlined safely")
    return source


def _fonts_css_inlined() -> str:
    """fonts.css with each url(/static/fonts/…) replaced by a base64 data URI."""
    css = _read_static("fonts/fonts.css")

    def inline(m):
        rel = m.group(1).lstrip("/")
        p = ROOT / rel
        if not p.is_file():
            raise ExportError(f"font file missing: {rel}")
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"url(data:font/woff2;base64,{b64})"

    return re.sub(r"url\('?(/static/fonts/[^')]+)'?\)", inline, css)


# The <link rel="preload"> block the player shells carry for the live server.
# It exists to beat the flash of fallback type on a cold page load over a real
# network; an export has no such load — standalone inlines its faces as data
# URIs and the folder build ships them next to the HTML — so the tags would
# only leave two dead /static/ requests behind. Stripped by both builders.
_PRELOAD_BLOCK = re.compile(
    r"[ \t]*<!-- font-preload:.*?<!-- /font-preload -->\n?", re.S)


def _strip_font_preload(shell: str) -> str:
    return _PRELOAD_BLOCK.sub("", shell)


# --------------------------------------------------------------------------
# Export branding (white-label): apply a saved brand profile to the chrome.
# --------------------------------------------------------------------------
# Maps a brand profile onto the player's own CSS tokens + rewrites the
# wordmark/logo/tagline. Colours/fonts go in as a :root override; the text,
# logos, and favicon are applied by a tiny injected script (robust to markup
# changes, and it runs in the player document only — never inside the frozen
# iframe pages).

def _parse_hex(c):
    c = (c or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return None
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _to_hex(rgb):
    r, g, b = (max(0, min(255, round(x))) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _mix(hex_a, hex_b, t):
    """Blend hex_a toward hex_b by t (0..1)."""
    a, b = _parse_hex(hex_a), _parse_hex(hex_b)
    if not a or not b:
        return hex_a
    return _to_hex(tuple(a[i] * (1 - t) + b[i] * t for i in range(3)))


def _soft(c):
    return _mix(c, "#FFFFFF", 0.86)


def _darken(c, t):
    return _mix(c, "#000000", t)


def _relative_luminance(rgb):
    def chan(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0587 * b


def _contrast(hex_a, hex_b):
    a, b = _parse_hex(hex_a), _parse_hex(hex_b)
    if not a or not b:
        return 0.0
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _readable(c, on, target=4.5):
    """Step C away from ON until it clears TARGET contrast: darker on a light
    ground, lighter on a dark one; give up at black or white respectively.

    A fixed darkening factor is not enough for a high-luminance accent: the
    brand's own amber 500 (#E0921F) sits at 2.2:1 on warm paper, and a flat
    32% darkening leaves it around 3:1 — still short of AA at body size, and
    eyebrows are body size. The accent is light, not paint, in any palette
    that follows the rule; the readable step of the ramp is a different colour
    (amber 700, which the stylesheet also uses as --primary)."""
    if _contrast(c, on) >= target:
        return c
    # On a dark ground, darkening can only make it worse: step toward white
    # instead. (A dark accent on a dark customer background used to come back
    # as #000000 here.)
    ground = _parse_hex(on)
    toward, limit = ("#000000", "#000000")
    if ground is not None and _relative_luminance(ground) < 0.179:
        toward, limit = ("#FFFFFF", "#FFFFFF")
    for step in range(1, 21):
        cand = _mix(c, toward, step / 20)
        if _contrast(cand, on) >= target:
            return cand
    return limit


def _logo_on(brand, ground):
    """The logo asset that reads on GROUND, from a profile's two logo fields.

    The two fields name the SURFACE an asset was drawn for, not its ink:
    `logo_light_url` is the version for light grounds (dark ink) and
    `logo_dark_url` is the reversed one. Both places a logo lands in an export —
    the intro card and the top bar — sit on the profile's own surface, and for a
    dark palette that is near-black. Preferring `logo_light_url` unconditionally,
    which is what this did, painted dark ink on a dark ground for every
    dark-palette profile, Captcher's own built-in profile included.

    Falls back to the other field so a profile that filled in only one still gets
    a logo rather than the wordmark.
    """
    light = (brand.get("logo_light_url") or "").strip()
    dark = (brand.get("logo_dark_url") or "").strip()
    rgb = _parse_hex(ground)
    # 0.179 is the usual light/dark split for relative luminance — the point
    # where white and black text swap places as the readable choice.
    if rgb is not None and _relative_luminance(rgb) < 0.179:
        return dark or light
    return light or dark


# The pre-Brand-Guide-v1 platform accent (lime Signal) and the deep step it used
# on its own Paper ground. The built-in profile no longer uses it, but customer
# profiles duplicated from the old built-in one still carry it, and their exports
# must keep the eyebrow colour they had. The current default accent (sky deep,
# #0A6FA8) already clears AA on its ground, so it needs no special case. The
# mirror of this rule lives in templates/branding.html (PLATFORM_ACCENT /
# PLATFORM_ACCENT_TEXT), which recomputes the live preview client-side. Move the
# two together.
PLATFORM_ACCENT = "#C6FF3D"


def _css_font_stack(name, fallback):
    name = (name or "").replace('"', "").strip()
    return f'"{name}", "{fallback}", ui-sans-serif, system-ui, sans-serif' if name \
        else f'"{fallback}", ui-sans-serif, system-ui, sans-serif'


def apply_branding(shell: str, brand: dict) -> str:
    """Inject a brand profile into the exported player document.

    `brand` is a resolved profile — whatever `branding.resolve_branding_profile`
    returned, a `brand_profiles` row or the built-in `branding.my_brand_profile()`
    — never the branding module and never a profile id. The parameter used to be
    called `branding`, which shadowed the imported module inside every function
    that took one; that is exactly the mistake scorm.main() was making (it passed
    the module through to the zip builder), so the name is `brand` throughout now.
    """
    if not brand:
        return shell
    colors = brand.get("colors", {}) or {}
    fonts = brand.get("fonts", {}) or {}

    # brand token -> player.css :root variable. The ten keys here are the
    # white-label contract: they are exactly branding.BRANDING_COLORS, which is
    # what the Branding form renders and what a brand_profiles row's `colors`
    # jsonb holds. Since the re-skin the slot names match the brand keys, so this
    # is an identity map for everything except the two that differ in spelling.
    # Keep it explicit anyway: it is the contract between a saved profile and the
    # stylesheet, and a key added on one side without the other silently exports
    # unbranded.
    token_map = {
        "primary": "--primary", "primary_hover": "--primary-hover",
        "accent": "--accent", "background": "--bg", "surface": "--surface",
        "text": "--text", "muted": "--muted", "border": "--border",
        "success": "--success", "error": "--error",
    }
    root = []
    for key, var in token_map.items():
        if _parse_hex(colors.get(key)):
            root.append(f"{var}:{colors[key]};")
    # derived soft tints for the states that use them
    if _parse_hex(colors.get("accent")):
        root.append(f"--accent-soft:{_soft(colors['accent'])};")
    if _parse_hex(colors.get("success")):
        root.append(f"--success-soft:{_soft(colors['success'])};")
    if _parse_hex(colors.get("error")):
        root.append(f"--error-soft:{_soft(colors['error'])};")
    root.append(f'--font-head:{_css_font_stack(fonts.get("heading"), "Outfit")};')
    root.append(f'--font-body:{_css_font_stack(fonts.get("body"), "DM Sans")};')

    extra = ""
    accent = (colors.get("accent") or "").upper()
    if _parse_hex(accent):
        # Match the live brand preview. Legacy rule: the old lime accent keeps
        # its olive deep step on the old Paper ground only; everything else is
        # darkened until it clears AA on the profile's own background.
        on = colors.get("background") if _parse_hex(colors.get("background")) else "#FFFFFF"
        text_accent = ("#4F6B00" if accent == PLATFORM_ACCENT and on.upper() == "#F4F4EF"
                       else _readable(accent, on))
        extra += f".eyebrow{{color:{text_accent};}}"
    # The top bar used to be a dark filled ground, so the mark was repainted in
    # --bg to survive it. It is flat --surface with one hairline under it now,
    # and the mark takes --text like every other lockup in the product, so
    # there is nothing left to override here.
    extra += (".brand-logo{height:24px;width:auto;max-width:160px;display:inline-block;"
              "vertical-align:middle;}.tb-left .brand-logo{height:20px;}")

    style = f'<style id="sc-brand">:root{{{"".join(root)}}}{extra}</style>'

    webfont = ""
    wf = (fonts.get("webfont_url") or "").strip()
    if wf.lower().startswith(("http://", "https://")):
        safe = wf.replace('"', "%22")
        webfont = f'<link rel="stylesheet" href="{safe}">'

    # brand data for the applier ("<" escaped so page-like values can't break out)
    data = templates.script_json(brand)
    # The chrome the logo sits on is --surface, with --bg as the fallback for a
    # profile that set only one of the two.
    chrome_logo = templates.script_json(
        _logo_on(brand, colors.get("surface") or colors.get("background")))
    applier = (
        '<script id="sc-brand-data" type="application/json">' + data + '</script>'
        '<script>(function(){var b=null;try{b=JSON.parse('
        'document.getElementById("sc-brand-data").textContent);}catch(e){return;}'
        'if(!b)return;var C=b.colors||{};'
        'if(b.brand_name){'
        'Array.prototype.forEach.call(document.querySelectorAll(".brand-word"),'
        'function(el){el.textContent=b.brand_name;});}'
        'function mark(sel,url,stroke,fill){var host=document.querySelector(sel);'
        'if(!host)return;if(url){var svg=host.querySelector("svg");if(svg){'
        'var img=new Image();img.src=url;img.alt=b.brand_name||"";'
        'img.className="brand-logo";svg.parentNode.replaceChild(img,svg);}'
        'var w=host.querySelector(".brand-word");if(w)w.style.display="none";}'
        # With no customer logo there is nothing to swap: the Captcher mark is
        # CSS boxes now, taking its frame from currentColor and its opened corner
        # from --accent, both of which the :root block above has already written.
        # The mark themes itself, so this branch only has to cope with the
        # zero-size .brand-logo-slot <svg> that exists purely as the swap hook.
        'else{var rs=host.querySelectorAll("svg rect");if(rs.length>=2){'
        'if(stroke)rs[0].setAttribute("stroke",stroke);'
        'if(fill)rs[1].setAttribute("fill",fill);'
        'if(stroke)for(var i=2;i<rs.length;i++)rs[i].setAttribute("fill",stroke);}}}'
        # One asset for both slots: they sit on the same surface. Chosen at
        # build time (see _logo_on) rather than in the page, because the ground
        # is a profile colour the server already has.
        f'mark(".intro-brand",{chrome_logo},C.primary,C.accent);'
        f'mark(".tb-left .brand",{chrome_logo},C.text,C.accent);'
        'if(b.favicon_url){var l=document.querySelector(\'link[rel~="icon"]\');'
        'if(!l){l=document.createElement("link");l.rel="icon";document.head.appendChild(l);}'
        'l.href=b.favicon_url;}'
        'var tag=document.querySelector(".tagline");if(tag){'
        'tag.textContent=(b.brand_name||"")+(b.tagline?" \\u00b7 "+b.tagline:"");'
        'if(b.show_attribution!==false){var a=document.createElement("span");'
        'a.textContent="Powered by Captcher";'
        'a.style.cssText="display:block;font-size:11px;margin-top:4px";'
        'tag.appendChild(a);}}})();</script>'
    )

    if "</head>" in shell:
        shell = shell.replace("</head>", style + webfont + "</head>", 1)
    else:
        shell = style + webfont + shell
    if "</body>" in shell:
        shell = shell.replace("</body>", applier + "</body>", 1)
    else:
        shell = shell + applier
    return shell


def preprocess_page(html: str) -> str:
    """Make one captured page snapshot standalone-safe."""
    # capture artifact: canonical link pointing at the recorded product's
    # localhost origin — inert, but meaningless outside the capture session
    html = re.sub(r'<link[^>]*rel=["\']?canonical["\']?[^>]*>', "", html, flags=re.I)

    # captured iframes: same rewrite the live server applies on /capture/
    html = unlock_frames(html)

    # freeze shim after the prologue, before any product script — the same
    # placement rule as the live route (see _PROLOGUE)
    html = freeze_page(html)

    # bridge inline before </body> — same placement as the server's injection
    # (positional splice, not re.sub: bridge source contains backslash
    # sequences that re.sub would misread as replacement escapes)
    bridge = "<script>" + _no_close_script(_read_static("bridge.js"), "bridge.js") + "</script>"
    b = re.search(r"</body>", html, flags=re.I)
    if b:
        html = html[: b.start()] + bridge + html[b.start():]
    else:
        html += bridge
    return html


def _row(walkthrough_id: str) -> dict:
    """One walkthrough row, or an ExportError.

    `store.require` raises NoSuchWalkthrough, but every caller of this module —
    server.py's download routes, scorm.py, the CLI below — catches ExportError
    and nothing else. Translating in one place is cheaper than teaching four
    call sites about a second exception type.

    No owner filter: the export builders are reached only after the route has
    already established that this member may see this walkthrough (or that it
    is a published embed/example, which is deliberately cross-account). Adding
    an owner here would break /embed/ and /examples/ and secure nothing.
    """
    row = store.get(walkthrough_id)
    if row is None:
        raise ExportError(f"unknown walkthrough: {walkthrough_id}")
    return row


def export_basename(row) -> str:
    """The human-readable stem for a file this walkthrough is exported as.

    Filenames come from the slug; the uuid addresses the row and nothing else.
    A download called `3f2a1b4c-7e19-4d0a-….html`, or a SCORM package named
    after one sitting in an LMS's course list, is unidentifiable to the person
    who has to find it again — and the slug column exists precisely so identity
    and name need not be the same string.

    The trade-off is that the name is no longer unique. Slugs are unique within
    one owner (store.unique_slug), so a single account's two exports are
    `log-a-ticket` and `log-a-ticket-2` — but two accounts can each export a
    `log-a-ticket.html`, and a filename no longer identifies a row. That is the
    right way round: the uuid is what addresses the row, and it is still in the
    URL that produced the download.
    """
    row = row or {}
    return row.get("slug") or row.get("id") or "walkthrough"


def _load_walkthrough_and_pages(walkthrough_id: str):
    """Load the walkthrough definition + every page snapshot its steps reference
    (not screenshots, not the recording, not unused auto/ snapshots), each made
    standalone-safe. Returns (walkthrough_dict, {relpath: preprocessed_html}).

    The definition is a jsonb column and the pages are objects in the captures
    bucket, so this is one row read plus one GET per referenced page. Those GETs
    are sequential: an export is already dominated by megabytes of frozen HTML
    moving over the wire, and a walkthrough has tens of pages, not thousands.
    """
    row = _row(walkthrough_id)
    # A copy: the row's dict is the query result, and the stamps below are for
    # the exported artefact only — they must not leak back into anything that
    # later writes this definition.
    walkthrough = dict(row.get("definition") or {})

    # Never embed internal account metadata. Ownership is the `owner_id` column
    # now and never travels inside the definition, so on a row written since the
    # move this pops nothing — it stays because a definition migrated from the
    # file era may still carry the author's email address, and an /embed/ export
    # is served to the public.
    walkthrough.pop("owner", None)

    # The id the player writes its progress under (player2.js: EMBED.walkthrough
    # .id, falling back to the literal "walkthrough"). Stamped from the row
    # rather than trusted from the definition: the definition's own "id" was the
    # old slug-shaped primary key and may be stale or absent, and two exports
    # sharing the fallback key would share one learner's progress in
    # localStorage.
    walkthrough["id"] = row["id"]
    if not walkthrough.get("title"):
        walkthrough["title"] = row.get("title") or ""

    page_refs = []
    for step in walkthrough.get("steps", []):
        p = step.get("page")
        if p and p not in page_refs:
            page_refs.append(p)

    pages, missing = {}, []
    for rel in page_refs:
        # No containment check here. It used to be `paths.is_within(p,
        # capture_root)` against a resolved directory; there is no directory any
        # more, and the rule about what a capture-relative path may look like
        # lives in `blobs.key_for` — which `store.capture_text` goes through, and
        # which returns None for anything with an empty, `.` or `..` segment.
        # Repeating the test here would be a second, drifting copy of the check
        # the store rewrite deliberately collapsed into one place.
        #
        # So `None` covers both "unsafe path" and "no such object", and both are
        # the same answer to an author: that page snapshot is not there.
        text = store.capture_text(walkthrough_id, rel)
        if text is None:
            missing.append(rel)
            continue
        pages[rel] = preprocess_page(text)
    if missing:
        raise ExportError("missing page snapshot(s): " + ", ".join(missing))
    return walkthrough, pages


def _titled(shell: str, walkthrough_id: str) -> str:
    """The shell's <title> becomes the walkthrough's own title.

    The player sets document.title once it renders its intro, but a tab or an
    LMS frame reads the static title first, and the brand applier used to
    overwrite the player's with the brand name, so a folder or SCORM export
    sat in its tab as "Captcher". The brand name goes on the brand word.
    """
    from html import escape
    row = _row(walkthrough_id)
    title = ((row.get("definition") or {}).get("title") or row.get("title") or "").strip()
    if not title:
        return shell
    return re.sub(r"<title>[^<]*</title>", lambda _m: f"<title>{escape(title)}</title>", shell, count=1)


def _embed_script(walkthrough_id: str, skip_intro: bool, chrome: str,
                  extra: dict = None) -> str:
    """The <script id="sc-embed"> block: walkthrough def + preprocessed
    pages, JSON-encoded with '<' escaped so no page content can close it."""
    walkthrough, pages = _load_walkthrough_and_pages(walkthrough_id)
    payload = {"walkthrough": walkthrough, "pages": pages,
               "skip_intro": bool(skip_intro), "chrome": chrome}
    if extra:
        payload.update(extra)
    data = templates.script_json(payload)
    return ('<script id="sc-embed" type="application/json">'
            + data + "</script>")


# The playback surface every export, embed, and SCORM package ships. This is
# the popup player: the step card floats beside the step's target instead of
# living in a fixed side panel. The classic player ("player") still builds
# correctly here and remains the authoring surface at /player/<id>?edit=1, but
# no learner-facing artefact uses it — keep this single constant as the switch
# rather than re-scattering the literal through the call sites.
EXPORT_PLAYER = "player2"


def _build_single_file(walkthrough_id: str, branding_profile: dict,
                       skip_intro: bool, chrome: str, player: str,
                       embed_extra: dict = None) -> str:
    """Build one self-contained player using the selected playback surface."""
    if chrome not in ("full", "viewport"):
        raise ExportError(f"unknown chrome mode: {chrome!r}")
    if player not in ("player", "player2"):
        raise ExportError(f"unknown player: {player!r}")

    skip_intro = bool(skip_intro) or chrome == "viewport"
    embed = _embed_script(walkthrough_id, skip_intro, chrome, embed_extra)
    shell = _strip_font_preload(_read_static(f"{player}.html"))
    player_css = _no_close_script(_read_static(f"{player}.css"), f"{player}.css")
    player_js = _no_close_script(_read_static(f"{player}.js"), f"{player}.js")
    logo_b64 = base64.b64encode((STATIC_DIR / "logo.svg").read_bytes()).decode("ascii")

    replacements = [
        ('<link rel="icon" type="image/svg+xml" href="/static/logo.svg">',
         f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{logo_b64}">'),
        ('<link rel="stylesheet" href="/static/fonts/fonts.css">',
         "<style>" + _fonts_css_inlined() + "</style>"),
        (f'<link rel="stylesheet" href="/static/{player}.css">',
         "<style>" + player_css + "</style>"),
        (f'<script src="/static/{player}.js"></script>',
         embed + "\n<script>" + player_js + "</script>"),
    ]
    for old, new in replacements:
        if old not in shell:
            raise ExportError(f"{player}.html changed — expected tag not found: {old}")
        shell = shell.replace(old, new)
    shell = _titled(shell, walkthrough_id)
    # Owned assets are shipped locally, including URLs in legacy saved profiles.
    # Do not mutate the profile or fetch customer assets in this single-file path.
    if branding_profile:
        branding_profile = dict(branding_profile)
        for field in ("logo_light_url", "logo_dark_url", "favicon_url"):
            own = _own_asset((branding_profile.get(field) or "").strip())
            if own is not None:
                mime = "image/svg+xml" if own.suffix.lower() == ".svg" else "image/png"
                data = base64.b64encode(own.read_bytes()).decode("ascii")
                branding_profile[field] = f"data:{mime};base64,{data}"
    return apply_branding(shell, branding_profile)


def build_export(walkthrough_id: str, brand: dict = None,
                 skip_intro: bool = False, chrome: str = "full") -> str:
    """Assemble the standalone HTML for one walkthrough. Returns the document.

    If `brand` (a resolved brand profile) is given, its colours, fonts, logo,
    wordmark, tagline, and favicon are applied to the exported chrome.

    If `skip_intro` is True the exported file starts straight in the walkthrough,
    omitting the opening description/objectives screen.

    `chrome` is "full" (the normal player: top bar, step panel, intro/complete
    cards — everything) or "viewport" (strip all of that away and show only
    the captured region, bordered, looping the "Show me" demonstration —
    used by bare inline embeds that don't want the full player UI). Viewport
    chrome implies skip_intro, since the intro card is itself chrome.
    """
    return _build_single_file(walkthrough_id, brand, skip_intro, chrome,
                              EXPORT_PLAYER)


def build_example_export(walkthrough_id: str) -> str:
    """Build a public example with the same popup player as /player/<id>.

    Example playback deliberately omits the Settings icon while keeping the
    rest of the production player unchanged.
    """
    return _build_single_file(
        walkthrough_id, None, False, "full", EXPORT_PLAYER,
        {"example": True})


# --------------------------------------------------------------------------
# Folder export: index.html + assets/ (CSS, JS, fonts, and downloaded brand
# images as real files) instead of one inlined file. Zippable for download.
# --------------------------------------------------------------------------

def _ext_for_content_type(ctype: str) -> str:
    """A file extension for a downloaded image's Content-Type, e.g.
    'image/svg+xml' -> '.svg', 'image/jpeg' -> '.jpg'."""
    subtype = ctype.split("/", 1)[-1].split("+")[0].split(";")[0].strip().lower()
    overrides = {"jpeg": "jpg", "x-icon": "ico", "vnd.microsoft.icon": "ico"}
    return "." + overrides.get(subtype, subtype or "bin")


# The built-in profile's three asset URLs (branding.SHADOW_CAPTURE_ASSETS) point
# at the marketing origin, because the fields are contract-bound to be absolute
# http(s) URLs. They are also files in this repo. Copy those off disk rather than
# fetching them, so the profile every default export uses adds no network
# round-trip to a download — and no failed export when the site is unreachable.
_OWN_ASSET_PREFIXES = ("https://shadowcapture.ai/assets/", "https://captcher.app/assets/",
                       "https://www.captcher.app/assets/")


def _own_asset(url: str):
    """The local file behind one of our own asset URLs, or None."""
    prefix = next((p for p in _OWN_ASSET_PREFIXES if url.lower().startswith(p)), None)
    if prefix is None:
        return None
    name = url[len(prefix):]
    # one path segment, no traversal, no query string
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    path = STATIC_DIR / name
    return path if path.is_file() else None


def _download_branding_images(brand: dict, images_dir: Path) -> dict:
    """Download every brand logo/favicon URL in `brand` into images_dir
    and return a copy of `brand` with those fields rewritten to local
    relative paths (assets/images/…), so the exported folder never needs
    internet access to render its branding. Identical URLs (e.g. the same
    logo used for both light and dark) are only downloaded once.

    This is the one place an export reaches the network, and it happens at BUILD
    time on the server, never at play time in the artefact: brand images are
    customer URLs that may be behind a CDN, and a SCORM package has to render in
    an LMS with no egress at all.

    A URL that will not fetch fails the export for a LOGO and is skipped for a
    FAVICON. The two are not the same loss. A logo is the white-label promise:
    dropping it silently ships someone else's training under a wordmark instead
    of their mark, and they find out from a customer. A favicon is the browser
    tab icon, and an export with no tab icon is an export — losing a whole SCORM
    package over a decorative 404 is the worse outcome by a wide margin, and it
    is the one that happened.
    """
    if not brand:
        return brand
    local = dict(brand)
    downloaded = {}
    for field in ("logo_light_url", "logo_dark_url", "favicon_url"):
        url = (brand.get(field) or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in downloaded:
            local[field] = downloaded[url]
            continue
        own = _own_asset(url)
        if own is not None:
            filename = field[: -len("_url")] + own.suffix
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / filename).write_bytes(own.read_bytes())
            rel = f"assets/images/{filename}"
            local[field] = rel
            downloaded[url] = rel
            continue
        # Lazy, and only on this branch: brandfetch pulls in author, which pulls
        # in the Anthropic SDK. A profile whose assets are all local — the
        # built-in one — should not drag that in to build an export.
        from . import brandfetch
        try:
            _final_url, ctype, data = brandfetch.fetch_public_image(url)
        except brandfetch.AuthorError as e:
            if field == "favicon_url":
                # Skipped, not fatal — see the note above. The applier only
                # rewrites the <link rel="icon"> when b.favicon_url is set, so
                # an empty string leaves the export's own icon in place.
                local[field] = ""
                continue
            raise ExportError(
                f"could not download the brand logo for “{brand.get('brand_name') or 'this profile'}” "
                f"({field.replace('_', ' ')}): {e} — fix or clear that URL on the Branding page.")
        filename = field[: -len("_url")] + _ext_for_content_type(ctype)
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / filename).write_bytes(data)
        rel = f"assets/images/{filename}"
        local[field] = rel
        downloaded[url] = rel
    return local


def _write_folder_assets(assets_dir: Path, player: str = EXPORT_PLAYER) -> Path:
    """Copy the player's CSS/JS/fonts/logo into assets/ as real files (no
    inlining, no base64). The copies keep the source filenames, so a folder
    export of the popup player holds assets/player2.css and assets/player2.js.
    Returns the assets/images directory."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in (f"{player}.css", f"{player}.js"):
        (assets_dir / name).write_bytes((STATIC_DIR / name).read_bytes())

    images_dir = assets_dir / "images"
    images_dir.mkdir(exist_ok=True)
    (images_dir / "logo.svg").write_bytes((STATIC_DIR / "logo.svg").read_bytes())

    fonts_dir = assets_dir / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    fonts_css = _read_static("fonts/fonts.css")
    for m in re.finditer(r"url\('?/static/fonts/([^')]+)'?\)", fonts_css):
        name = m.group(1)
        src = STATIC_DIR / "fonts" / name
        if not src.is_file():
            raise ExportError(f"font file missing: static/fonts/{name}")
        (fonts_dir / name).write_bytes(src.read_bytes())
    (fonts_dir / "fonts.css").write_text(
        fonts_css.replace("/static/fonts/", ""), encoding="utf-8")
    return images_dir


def build_export_folder(walkthrough_id: str, out_dir: Path, brand: dict = None,
                        skip_intro: bool = False, chrome: str = "full",
                        player: str = EXPORT_PLAYER) -> Path:
    """Assemble a folder-structured export: out_dir/index.html referencing
    out_dir/assets/ (<player>.css, <player>.js, fonts, and — if `brand`
    sets any logo/favicon URLs — those images downloaded locally). The result
    needs no internet access to render. Wipes out_dir first. Returns out_dir.

    `brand`, `skip_intro`, `chrome` behave exactly as in build_export.
    `player` selects the playback surface, as in _build_single_file.

    out_dir is an ordinary filesystem path and stays one: both callers hand it a
    TemporaryDirectory they are about to zip and delete. It is a build artefact,
    not user data — the only durable copy of an export is the bytes the browser
    downloads.
    """
    if chrome not in ("full", "viewport"):
        raise ExportError(f"unknown chrome mode: {chrome!r}")
    if player not in ("player", "player2"):
        raise ExportError(f"unknown player: {player!r}")
    skip_intro = bool(skip_intro) or chrome == "viewport"
    embed = _embed_script(walkthrough_id, skip_intro, chrome)

    out_dir = Path(out_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    images_dir = _write_folder_assets(out_dir / "assets", player)
    local_brand = _download_branding_images(brand, images_dir)

    shell = _strip_font_preload(_read_static(f"{player}.html"))

    replacements = [
        ('<link rel="icon" type="image/svg+xml" href="/static/logo.svg">',
         '<link rel="icon" type="image/svg+xml" href="assets/images/logo.svg">'),
        ('<link rel="stylesheet" href="/static/fonts/fonts.css">',
         '<link rel="stylesheet" href="assets/fonts/fonts.css">'),
        (f'<link rel="stylesheet" href="/static/{player}.css">',
         f'<link rel="stylesheet" href="assets/{player}.css">'),
        (f'<script src="/static/{player}.js"></script>',
         embed + f'\n<script src="assets/{player}.js"></script>'),
    ]
    for old, new in replacements:
        if old not in shell:
            raise ExportError(f"{player}.html changed — expected tag not found: {old}")
        shell = shell.replace(old, new)
    shell = apply_branding(_titled(shell, walkthrough_id), local_brand)

    (out_dir / "index.html").write_text(shell, encoding="utf-8")
    return out_dir


def build_export_zip(walkthrough_id: str, brand: dict = None,
                     skip_intro: bool = False, chrome: str = "full") -> bytes:
    """Build the folder export (see build_export_folder) and pack it into a
    ZIP archive, namespaced under the walkthrough's slug. Returns the zip bytes.

    The archive root is the slug rather than the id, for the same reason the
    download filename is (see export_basename): unpacking this should leave a
    folder called `log-a-ticket/`, not one named after a uuid that means nothing
    outside the database. The row is read here purely to learn that name.
    """
    stem = export_basename(_row(walkthrough_id))
    with tempfile.TemporaryDirectory(prefix="rc-export-") as tmp:
        folder = build_export_folder(walkthrough_id, Path(tmp) / stem,
                                     brand, skip_intro, chrome)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    arcname = str(Path(stem) / path.relative_to(folder))
                    zf.write(path, arcname=arcname)
        return buf.getvalue()


def main():
    # optional: --profile <id> (or --brand <id>) applies a saved brand profile
    args = sys.argv[1:]
    profile_id = None
    for flag in ("--profile", "--brand"):
        if flag in args:
            i = args.index(flag)
            profile_id = args[i + 1] if i + 1 < len(args) else ""
            del args[i:i + 2]
            break

    # optional: --folder (index.html + assets/) or --zip (the same, zipped)
    fmt = "html"
    for flag, name in (("--folder", "folder"), ("--zip", "zip")):
        if flag in args:
            args.remove(flag)
            fmt = name
            break

    if not args:
        print(__doc__.strip())
        sys.exit(1)
    walkthrough_id = args[0]

    try:
        row = _row(walkthrough_id)
    except ExportError as e:
        print(f"export failed: {e}")
        sys.exit(1)
    stem = export_basename(row)

    brand = None
    if profile_id is not None:
        # The owner comes off the row, not off a session: the CLI has nobody
        # signed in, and `resolve_branding_profile` can only find a saved profile
        # within one account. Called without an owner it returns the built-in
        # profile for every id, so `--profile <a-real-profile-id>` used to
        # silently export unbranded and say "[brand: Captcher]" while doing
        # it. The walkthrough's owner is the right account to look in: a brand
        # profile belongs to whoever owns the walkthrough being exported.
        brand = branding.resolve_branding_profile(profile_id, row["owner_id"])
    brand_note = f" [brand: {brand['brand_name']}]" if brand else ""

    try:
        if fmt == "folder":
            out = Path(args[1]) if len(args) > 1 else ROOT / "exports" / stem
            build_export_folder(walkthrough_id, out, brand)
            print(f"exported {stem} -> {out}/{brand_note}")
        elif fmt == "zip":
            out = Path(args[1]) if len(args) > 1 else ROOT / "exports" / f"{stem}.zip"
            zip_bytes = build_export_zip(walkthrough_id, brand)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zip_bytes)
            print(f"exported {stem} -> {out} "
                  f"({len(zip_bytes) // 1024} KB){brand_note}")
        else:
            out = Path(args[1]) if len(args) > 1 else ROOT / "exports" / f"{stem}.html"
            html = build_export(walkthrough_id, brand)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            print(f"exported {stem} -> {out} "
                  f"({len(html) // 1024} KB){brand_note}")
    except ExportError as e:
        print(f"export failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
