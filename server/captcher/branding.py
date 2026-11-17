"""Export branding — colour/font profiles applied to exported walkthroughs.

Owns the brand profile store, the Google Fonts catalogue and URL construction,
and payload validation. Split out of server.py, which owned all of this
alongside the HTTP layer.

`export.py` and `author.py` are the consumers; neither imports server.

WHERE PROFILES LIVE

One row per profile in `brand_profiles`, owned by a member. The active choice is
`members.active_brand_profile_id`, and a null there means the built-in read-only
Captcher profile — which is synthesised in code, never stored, and so
cannot be edited, deleted, or left dangling by a delete.

That last property is why the active profile is a foreign key with `on delete
set null` rather than a stored id string. The predecessor kept `{"active":
"<id>"}` in a JSON file and needed `branding_collection()` to notice when the id
named a profile that no longer existed and silently fall back. The database now
does that, at the moment of deletion, for every reader at once.

Two other things the table fixed. Profiles were keyed by a sha256 of the owner's
lowercased email address, so changing an address orphaned them — see the note in
auth.py. And `save_branding_store` was a read-modify-write over a whole file
with no lock at all, documented as racy: two simultaneous saves lost one. A row
update touches one profile and takes its own lock.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


from . import db

ROOT = Path(__file__).resolve().parent


# ==========================================================================
# Export branding
# ==========================================================================
# White-label settings for exported walkthroughs, so a company can run these
# walkthroughs on their own site or app under their own brand — or their
# client's. A saved profile is applied to an export when chosen in the Download
# dialog (see export.apply_branding).
#
# The defaults mirror Captcher's own player tokens, so the form opens
# pre-filled with the current look as a template. Logos and favicons are URLs,
# never uploaded or stored locally.

# each colour: (key, label, help) — keys map onto the player's CSS tokens
BRANDING_COLORS = [
    ("primary",       "Primary",         "Buttons and the top bar's active state. It carries "
                                         "text, so it has to stay readable on Background."),
    ("primary_hover", "Primary (hover)", "Primary's hover and pressed state. A darker step of "
                                         "the same hue, not a different colour."),
    ("accent",        "Accent",          "The active target, progress, the spotlight — light "
                                         "rather than paint. Reserved — never a text colour."),
    ("background",    "Background",      "The page behind cards and the step rail."),
    ("surface",       "Surface",         "Cards and panels, raised one step above Background."),
    ("text",          "Text",            "Body copy and headings — almost everything a learner "
                                         "reads."),
    ("muted",         "Muted text",      "Secondary text: hints, counts, captions."),
    ("border",        "Border",          "Hairlines around cards, inputs and the captured stage."),
    ("success",       "Success",         "Correct-answer feedback in You-try. Never set this to "
                                         "your accent: the accent means look here, success means "
                                         "you were right."),
    ("error",         "Error",           "Wrong-answer feedback in You-try."),
]

DEFAULT_BRANDING = {
    "brand_name": "Captcher",
    "tagline": "One run-through is the training.",
    # Deliberately blank here, and filled in only by `my_brand_profile()` below.
    # DEFAULT_BRANDING is the base every *customer* profile starts from — "New
    # profile" and "Reset" both clone it — so a logo URL left in it would ship
    # Captcher's lockup inside someone else's white-label export. See
    # SHADOW_CAPTURE_ASSETS.
    "logo_light_url": "",   # logo for light backgrounds (LMS, print, procurement)
    "logo_dark_url": "",    # logo for dark backgrounds
    "favicon_url": "",      # browser tab icon for the exported file
    # Captcher's light player palette (Brand Guide v1). Keep all ten customer
    # slots stable. Indigo is You try and every primary action; Sky is Show
    # me and "look here" (no purple anywhere); success is its own green and never derived from the
    # accent. Slots that carry text use the deep steps of the brand hues so they
    # clear AA on the Mist ground: accent 5.0:1, success 4.8:1, error 5.4:1.
    # Saved customer profiles store their own colours and are not affected.
    "colors": {
        "primary": "#3B49C6",       # Indigo
        "primary_hover": "#2F3BA8",
        "accent": "#0A6FA8",        # Sky, deep step
        "background": "#F2F3F7",    # Mist
        "surface": "#FFFFFF",
        "text": "#14151A",          # Ink
        "muted": "#6B6F7B",
        "border": "#E6E7EC",        # Line
        "success": "#0B7A5A",       # Mint, deep step
        "error": "#B83227",         # Coral, deep step
    },
    "fonts": {
        "heading": "Outfit",              # display + wordmark (a Google Font family)
        "body": "DM Sans",                # UI + body copy (a Google Font family)
        "webfont_url": "",              # derived from the two families above
    },
    "show_attribution": True,           # keep a "Powered by Captcher" line
}

# Curated Google Font families offered in the Heading/Body pickers. All chosen
# to ship a 400 (regular) and 700 (bold) weight, so the derived css2 URL is
# always valid. Users may also type any other Google Font family name.
GOOGLE_FONTS = [
    "Albert Sans", "Archivo", "Barlow", "Be Vietnam Pro", "Bitter",
    "Cormorant Garamond", "DM Sans", "DM Serif Text", "Epilogue", "Figtree",
    "Fraunces", "Hanken Grotesk", "IBM Plex Sans", "IBM Plex Serif",
    "Instrument Sans", "Inter", "Kanit", "Karla", "Lato", "Lexend",
    "Libre Baskerville", "Lora", "Manrope", "Merriweather", "Montserrat",
    "Mulish", "Newsreader", "Nunito", "Nunito Sans", "Onest", "Open Sans",
    "Oswald", "Outfit", "PT Sans", "PT Serif", "Plus Jakarta Sans",
    "Playfair Display", "Poppins", "Prompt", "Raleway", "Red Hat Display",
    "Roboto", "Rubik", "Sora", "Source Sans 3", "Source Serif 4",
    "Space Grotesk", "Spectral", "Urbanist", "Work Sans",
]

# Captcher's own faces are inlined (base64) into every export via
# fonts.css, so no Google URL is ever needed for them. Keep this in step with
# the @font-face block in static/fonts/fonts.css.
#
# Outfit and JetBrains Mono are bundled locally. Sora remains bundled for
# existing profiles: changing a saved font choice is not part of a rebrand.
# All three families are inlined for air-gapped exports.
_INLINED_FONTS = {"dm sans", "outfit", "sora", "jetbrains mono"}

# Google Fonts Developer API key (env/.env: GOOGLE_FONTS_API_KEY). When set,
# the Branding font picker offers the full ~1,800-family catalog with live
# previews; without it we fall back to the curated GOOGLE_FONTS list above.
# Get a free key at https://developers.google.com/fonts/docs/developer_api
GOOGLE_FONTS_API_KEY = os.environ.get("GOOGLE_FONTS_API_KEY", "").strip()

_FONTS_CACHE = {"at": 0.0, "data": None, "source": None}
_FONTS_CACHE_TTL = 24 * 3600            # the catalog changes rarely
_FONTS_LOCK = threading.Lock()
# family (lowercased) -> sorted list of numeric weights it ships. Used to
# build css2 URLs that only request weights that actually exist (a hardcoded
# :wght@400;700 400s for any family without a 700). Warmed by the first fetch.
_FONT_WEIGHTS = {}


def _variants_to_weights(variants):
    """Map Google 'variants' (regular, italic, 700, 300italic, …) to the set
    of numeric font-weights available, ignoring italics."""
    weights = set()
    for v in variants or []:
        v = str(v).lower()
        if v in ("regular", "italic"):
            weights.add(400)
        else:
            m = re.match(r"(\d{3})", v)
            if m:
                weights.add(int(m.group(1)))
    return sorted(weights)


def _curated_font_list():
    """The offline fallback catalog, shaped like the API response."""
    return [{"family": f, "category": "", "variants": ["regular", "700"]}
            for f in GOOGLE_FONTS]


def fetch_google_fonts(force=False):
    """Return (fonts, source): a list of {family, category, variants} sorted by
    popularity, plus 'google' or 'curated'. Cached in-memory for
    _FONTS_CACHE_TTL. Falls back to the curated list when no API key is set or
    the request fails, so the picker always works."""
    now = time.time()
    with _FONTS_LOCK:
        if (not force and _FONTS_CACHE["data"] is not None
                and now - _FONTS_CACHE["at"] < _FONTS_CACHE_TTL):
            return _FONTS_CACHE["data"], _FONTS_CACHE["source"]

        fonts, source = None, "curated"
        if GOOGLE_FONTS_API_KEY:
            url = ("https://www.googleapis.com/webfonts/v1/webfonts?sort=popularity&key="
                   + urllib.parse.quote(GOOGLE_FONTS_API_KEY))
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                fonts = [{"family": it["family"],
                          "category": it.get("category", ""),
                          "variants": it.get("variants", [])}
                         for it in (raw.get("items") or []) if it.get("family")]
                source = "google" if fonts else "curated"
            except Exception as e:   # network / HTTP / JSON — degrade gracefully
                print(f"[fonts] Google Fonts API fetch failed: {e}; using curated list")
                fonts = None

        if not fonts:
            fonts, source = _curated_font_list(), "curated"

        _FONT_WEIGHTS.clear()
        for it in fonts:
            _FONT_WEIGHTS[it["family"].lower()] = _variants_to_weights(it["variants"])

        _FONTS_CACHE.update(at=now, data=fonts, source=source)
        return fonts, source


def _family_css2_part(fam):
    """css2 'family=…' fragment for one family, using its real weights. Prefers
    400, 700 & 800; if the family ships none of them (or its weights are
    unknown) it requests the family with no axis, which loads its default face.

    800 is in the list because the exported player sets page titles at that
    weight (see h1/h2 in static/player.css). A family without it falls back to
    700 — which the browser will then synthesise up, and that is the best
    available answer; asking for a weight the family does not have would not
    help."""
    plus = fam.replace(" ", "+")
    available = _FONT_WEIGHTS.get(fam.lower())
    if not available:
        return "family=" + plus
    want = [w for w in (400, 700, 800) if w in available] or [available[0]]
    if want == [400]:                       # 400 is the default face already
        return "family=" + plus
    return "family=" + plus + ":wght@" + ";".join(str(w) for w in want)


def google_fonts_url(heading, body):
    """Build a valid Google Fonts css2 URL for the two families using each
    family's actual available weights, skipping the fonts already inlined into
    exports. '' when nothing to load."""
    families = []
    for fam in (heading, body):
        fam = (fam or "").replace('"', "").strip()
        if fam and fam.lower() not in _INLINED_FONTS and fam not in families:
            families.append(fam)
    if not families:
        return ""
    if not _FONT_WEIGHTS:                    # warm the weight index (cached)
        fetch_google_fonts()
    parts = "&".join(_family_css2_part(f) for f in families)
    return "https://fonts.googleapis.com/css2?" + parts + "&display=swap"


_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# The read-only "my brand" profile: Captcher's own brand, fixed in code.
# It is always available (first in the list), it is the reset baseline, and it
# is the template new profiles are copied from. It can never be edited or
# deleted.
#
# It is deliberately NOT a row. As a row it would need seeding into every new
# account, protecting from every delete path, and re-seeding whenever the brand
# changed. As a constant it is none of those things, and `active is null` is
# already the correct database representation of "using the built-in one".
#
# The sentinel is not a uuid, so it can never collide with a real profile id.
MYBRAND_ID = "shadow-capture"

# Our own lockup and tab icon, for the built-in profile only.
#
# Absolute URLs on the marketing origin rather than /static/ paths, because the
# three fields are contract-bound to be absolute http(s) URLs: `validate_branding`
# rejects anything else, `export._download_branding_images` fetches them at build
# time, and the applier script assigns them straight to `img.src` inside an
# exported file that has no server behind it. www.captcher.app serves these as
# static assets, so an export built on any host resolves them.
#
# The horizontal lockup (mark + wordmark), not the bare mark: these fields
# REPLACE the wordmark text in the exported chrome — see the `mark()` helper in
# export.py, which hides `.brand-word` whenever a logo URL is present — so a
# mark-only asset would drop the name from the intro card and the top bar.
#
# Light/dark name the SURFACE, not the ink. Old public Python constant name
# retained for compatibility; only the built-in profile uses these new URLs.
SHADOW_CAPTURE_ASSETS = {
    "logo_light_url": "https://www.captcher.app/assets/logo-horizontal-light.svg",
    "logo_dark_url": "https://www.captcher.app/assets/logo-horizontal.svg",
    "favicon_url": "https://www.captcher.app/assets/favicon-32.png",
}


def my_brand_profile():
    """The locked Captcher profile (a fresh copy each call)."""
    p = json.loads(json.dumps(DEFAULT_BRANDING))
    p.update(SHADOW_CAPTURE_ASSETS)
    p["id"] = MYBRAND_ID
    p["read_only"] = True
    return p


def _merge_brand(data):
    """Fill any missing keys of a stored brand from the defaults."""
    merged = json.loads(json.dumps(DEFAULT_BRANDING))
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("colors", "fonts") and isinstance(v, dict):
                merged[k].update({ck: cv for ck, cv in v.items() if ck in merged[k]})
            elif k in merged:
                merged[k] = v
    # webfont_url is always derived from the chosen families — never stored
    # free-text — so stale/invalid URLs (e.g. a specimen page) self-heal.
    merged["fonts"]["webfont_url"] = google_fonts_url(
        merged["fonts"].get("heading"), merged["fonts"].get("body"))
    return merged


MAX_PROFILES = 50


def _row_to_profile(row):
    """One brand_profiles row as the profile dict the client and export expect.

    Run through `_merge_brand` so a row written before a key existed comes back
    complete, and so `fonts.webfont_url` is re-derived rather than trusted.
    """
    profile = _merge_brand({
        "brand_name": row["brand_name"],
        "tagline": row["tagline"],
        "logo_light_url": row["logo_light_url"] or "",
        "logo_dark_url": row["logo_dark_url"] or "",
        "favicon_url": row["favicon_url"] or "",
        "colors": row["colors"] or {},
        "fonts": row["fonts"] or {},
        "show_attribution": row["show_attribution"],
    })
    profile["id"] = row["id"]
    profile["slug"] = row["slug"]
    return profile


_PROFILE_COLUMNS = """
    id, slug, brand_name, tagline,
    logo_light_url, logo_dark_url, favicon_url,
    colors, fonts, show_attribution
"""


def _unique_profile_slug(owner_id, name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:80] or "brand"
    taken = {
        r["slug"] for r in db.query(
            "select slug from brand_profiles where owner_id = %s and slug like %s",
            (owner_id, base + "%"),
        )
    }
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def list_profiles(owner_id):
    """Every editable profile for one account, oldest first."""
    return [
        _row_to_profile(r) for r in db.query(
            f"select {_PROFILE_COLUMNS} from brand_profiles "
            "where owner_id = %s order by created_at",
            (owner_id,),
        )
    ]


def active_profile_id(owner_id):
    """The member's active profile id, or MYBRAND_ID when it is the built-in.

    Never returns a dangling id: the column is a foreign key with `on delete set
    null`, so deleting the active profile resets the member to the built-in one
    as part of the same statement.
    """
    row = db.one(
        "select active_brand_profile_id as id from members where id = %s",
        (owner_id,),
    )
    return (row or {}).get("id") or MYBRAND_ID


def branding_collection(owner_id=None):
    """What the client sees: read-only my-brand first, then editable profiles."""
    if owner_id is None:
        return {"active": MYBRAND_ID, "profiles": [my_brand_profile()]}
    return {
        "active": active_profile_id(owner_id),
        "profiles": [my_brand_profile()] + list_profiles(owner_id),
    }


def get_profile(owner_id, profile_id):
    """One editable profile, or None. The built-in is not returned here — it is
    not a row, and callers that want it ask for it by name."""
    if not profile_id or profile_id == MYBRAND_ID:
        return None
    try:
        row = db.one(
            f"select {_PROFILE_COLUMNS} from brand_profiles "
            "where id = %s and owner_id = %s",
            (profile_id, owner_id),
        )
    except db.Error:
        return None
    return _row_to_profile(row) if row else None


def create_profile(owner_id, clean):
    """Insert a validated profile. Returns it, or None when the cap is reached."""
    count = db.value(
        "select count(*) from brand_profiles where owner_id = %s", (owner_id,), default=0
    )
    if count >= MAX_PROFILES:
        return None
    row = db.one(
        f"""insert into brand_profiles
                (owner_id, slug, brand_name, tagline, logo_light_url,
                 logo_dark_url, favicon_url, colors, fonts, show_attribution)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning {_PROFILE_COLUMNS}""",
        (owner_id, _unique_profile_slug(owner_id, clean["brand_name"]),
         clean["brand_name"], clean["tagline"], clean["logo_light_url"],
         clean["logo_dark_url"], clean["favicon_url"],
         db.Json(clean["colors"]), db.Json(clean["fonts"]), clean["show_attribution"]),
    )
    return _row_to_profile(row)


def update_profile(owner_id, profile_id, clean):
    """Overwrite one profile. Returns it, or None when there is no such profile.

    The owner filter is in the UPDATE rather than a SELECT beforehand, so there
    is no window between the ownership check and the write.
    """
    if not profile_id or profile_id == MYBRAND_ID:
        return None
    try:
        row = db.one(
            f"""update brand_profiles
                   set brand_name = %s, tagline = %s, logo_light_url = %s,
                       logo_dark_url = %s, favicon_url = %s, colors = %s,
                       fonts = %s, show_attribution = %s
                 where id = %s and owner_id = %s
             returning {_PROFILE_COLUMNS}""",
            (clean["brand_name"], clean["tagline"], clean["logo_light_url"],
             clean["logo_dark_url"], clean["favicon_url"],
             db.Json(clean["colors"]), db.Json(clean["fonts"]),
             clean["show_attribution"], profile_id, owner_id),
        )
    except db.Error:
        return None
    return _row_to_profile(row) if row else None


def delete_profile(owner_id, profile_id):
    """Remove one profile. The built-in cannot be deleted; it is not a row.

    A member whose active profile this was falls back to the built-in, by the
    foreign key's `on delete set null` — not by any code here.
    """
    if not profile_id or profile_id == MYBRAND_ID:
        return False
    try:
        return db.execute(
            "delete from brand_profiles where id = %s and owner_id = %s",
            (profile_id, owner_id),
        ) > 0
    except db.Error:
        return False


def set_active_profile(owner_id, profile_id):
    """Choose the active profile. MYBRAND_ID (or None) selects the built-in.

    A profile id belonging to somebody else resolves to None by the same
    `get_profile` ownership filter, so it selects the built-in rather than
    another account's brand.
    """
    if not profile_id or profile_id == MYBRAND_ID or not get_profile(owner_id, profile_id):
        db.execute(
            "update members set active_brand_profile_id = null where id = %s", (owner_id,)
        )
        return MYBRAND_ID
    db.execute(
        "update members set active_brand_profile_id = %s where id = %s",
        (profile_id, owner_id),
    )
    return profile_id


def resolve_branding_profile(profile_id=None, owner_id=None):
    """Pick a brand profile for an export: the requested id, else the account's
    active profile, else the read-only built-in. Always returns a full profile.

    The built-in is an answer, not the absence of one. Asking for it BY NAME
    used to fall past the request and land on `active_profile_id`, because the
    only test here was `profile_id != MYBRAND_ID` — the id was read as "nothing
    was requested". So picking "Captcher — my brand" in the Download
    dialog exported the account's active custom profile instead, and read as
    the dialog being ignored. It is the one id that never needs a lookup, so it
    is answered first.

    Falling through is for a request that CANNOT be honoured, not one that
    merely disagrees with the active profile: `/embed/` froze its profile id at
    publish time and that profile may since have been deleted — hence returning
    something rather than failing.
    """
    if profile_id == MYBRAND_ID:
        return my_brand_profile()
    if owner_id is None:
        return my_brand_profile()
    if profile_id:
        found = get_profile(owner_id, profile_id)
        if found:
            return found
    active = active_profile_id(owner_id)
    if active != MYBRAND_ID:
        found = get_profile(owner_id, active)
        if found:
            return found
    return my_brand_profile()


def validate_branding(payload):
    """Coerce + validate an incoming branding payload.
    Returns (clean_dict, errors[]). Unknown keys are dropped."""
    errors = []
    clean = json.loads(json.dumps(DEFAULT_BRANDING))  # deep copy as the base

    def _str(v, cap=200):
        return ("" if v is None else str(v)).strip()[:cap]

    if isinstance(payload, dict):
        clean["brand_name"] = _str(payload.get("brand_name"), 80) or DEFAULT_BRANDING["brand_name"]
        clean["tagline"] = _str(payload.get("tagline"), 160)
        for key in ("logo_light_url", "logo_dark_url", "favicon_url"):
            url = _str(payload.get(key), 2048)
            if url and not re.match(r"^https?://", url, re.I):
                errors.append(f"{key.replace('_', ' ')} must start with http:// or https://")
                url = ""
            clean[key] = url

        colors = payload.get("colors") or {}
        for key, label, _help in BRANDING_COLORS:
            val = _str(colors.get(key), 7)
            if val and not _HEX_RE.match(val):
                errors.append(f"{label} colour must be a hex value like #8A4B12")
            elif val:
                clean["colors"][key] = val.upper() if val.startswith("#") else val

        fonts = payload.get("fonts") or {}
        clean["fonts"]["heading"] = _str(fonts.get("heading"), 120) or DEFAULT_BRANDING["fonts"]["heading"]
        clean["fonts"]["body"] = _str(fonts.get("body"), 120) or DEFAULT_BRANDING["fonts"]["body"]
        # webfont URL is derived from the two families (Google Fonts), never
        # taken from the client — so it is always a valid css2 stylesheet URL.
        clean["fonts"]["webfont_url"] = google_fonts_url(
            clean["fonts"]["heading"], clean["fonts"]["body"])

        clean["show_attribution"] = bool(payload.get("show_attribution", True))
    else:
        errors.append("Expected a JSON object")
    return clean, errors
