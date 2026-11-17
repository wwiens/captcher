#!/usr/bin/env python3
"""
Export a walkthrough as a SCORM content package.
Supports both SCORM 2004 (the API_1484_11 RTE) and SCORM 1.2 (the older API
RTE, still what many on-prem LMSs expect) — pick with `version`.

Reuses export.build_export_folder() for the actual player (index.html +
assets/) and adds what SCORM needs on top:
  - static/scorm-api.js, copied into assets/ and wired in ahead of the
    player script (with a tiny inline <script> setting window.RC_SCORM_VERSION
    first), so it can call the LMS's SCORM RTE and set window.RC_RESUME
    before the player's own boot code runs. scorm-api.js itself picks the
    right API object (window.API vs window.API_1484_11) and CMI data model
    field names based on that flag — see its own comments.
  - imsmanifest.xml, describing the walkthrough as a single SCO, in the
    schema the chosen version expects.
  - the whole folder zipped FLAT at the archive root (imsmanifest.xml must
    sit at the top of the zip — unlike export.build_export_zip, which
    namespaces everything under "<slug>/").

The walkthrough is addressed by uuid; the package FILE is named from the row's
slug (export.export_basename), while the manifest's identifiers are built from
the uuid. An LMS needs those identifiers globally unique and needs the filename
readable, and since the store rewrite those are two different strings.

Usage:
  python3 scorm.py <walkthrough-id> [output.zip]      (default: exports/<slug>-scorm.zip)
  python3 scorm.py <walkthrough-id> --profile <brand-id> [output.zip]
  python3 scorm.py <walkthrough-id> --version 1.2 [output.zip]   (default: 2004)
Or from server.py:  GET /api/walkthroughs/<id>/export-scorm[?profile=<brand-id>&version=1.2]
"""
import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from . import branding
from . import export

ROOT = Path(__file__).resolve().parent
# Application code, not user data — see the note on export.STATIC_DIR.
STATIC_DIR = ROOT / "static"

# Per-version manifest shape. SCORM 1.2 predates the sequencing/navigation
# extensions (imsss/adlseq/adlnav) and uses a different IMS CP namespace and
# a lowercase adlcp:scormtype attribute — a real, commonly-checked detail.
SCORM_VERSIONS = {
    "2004": {
        "schema_version": "2004 4th Edition",
        "scormtype_attr": "adlcp:scormType",
        "namespaces": (
            'xmlns="http://www.imsglobal.org/xsd/imscp_v1p1" '
            'xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3" '
            'xmlns:adlseq="http://www.adlnet.org/xsd/adlseq_v1p3" '
            'xmlns:adlnav="http://www.adlnet.org/xsd/adlnav_v1p3" '
            'xmlns:imsss="http://www.imsglobal.org/xsd/imsss" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://www.imsglobal.org/xsd/imscp_v1p1 imscp_v1p1.xsd '
            'http://www.adlnet.org/xsd/adlcp_v1p3 adlcp_v1p3.xsd '
            'http://www.adlnet.org/xsd/adlseq_v1p3 adlseq_v1p3.xsd '
            'http://www.adlnet.org/xsd/adlnav_v1p3 adlnav_v1p3.xsd '
            'http://www.imsglobal.org/xsd/imsss imsss_v1p0.xsd"'
        ),
    },
    "1.2": {
        "schema_version": "1.2",
        "scormtype_attr": "adlcp:scormtype",
        "namespaces": (
            'xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2" '
            'xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://www.imsproject.org/xsd/imscp_rootv1p1p2 imscp_rootv1p1p2.xsd '
            'http://www.adlnet.org/xsd/adlcp_rootv1p2 adlcp_rootv1p2.xsd"'
        ),
    },
}


def _ncname(s: str) -> str:
    """Sanitize `s` into a valid XML NCName (identifier): letters/digits/-/_/.
    only, and must not start with a digit."""
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", s or "")
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    return s


def _check_version(version: str) -> str:
    if version not in SCORM_VERSIONS:
        raise export.ExportError(
            f"unknown SCORM version: {version!r} (expected one of {sorted(SCORM_VERSIONS)})")
    return version


def build_manifest(row: dict, files: list, version: str = "2004") -> str:
    """The imsmanifest.xml content for a single-SCO SCORM package, either
    "2004" or "1.2".

    `row` is a walkthrough row (store.get), not the definition inside it. The
    manifest is about identity — an LMS keys its course record off these
    identifiers — and identity now lives in columns: `id` is the uuid, `title`
    is the column the editor keeps in step with the definition.

    The identifiers are built from the uuid rather than the slug on purpose.
    Slugs are unique per owner, so two customers importing `log-a-ticket` into
    the same hosted LMS would collide on manifest identifiers; a uuid cannot.
    The slug is used for the *filename* of the package instead, which is what a
    person actually reads — see export.export_basename.

    `files` is a list of file paths (forward-slash, relative to the package
    root) to declare in the resource — every file actually written to the
    package, so stricter LMS validators can confirm nothing is missing.
    """
    spec = SCORM_VERSIONS[_check_version(version)]
    # A uuid starts with a digit about 62% of the time and an NCName may not, so
    # _ncname's leading underscore is the normal case here, not the exception.
    wt_id = _ncname(row.get("id") or "walkthrough")
    org_id = f"ORG-{wt_id}"
    item_id = f"ITEM-{wt_id}"
    res_id = f"RES-{wt_id}"
    title = xml_escape(row.get("title") or row.get("slug") or "Walkthrough")

    file_tags = "".join(f'<file href="{xml_escape(f)}"/>' for f in files)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<manifest identifier="MANIFEST-{wt_id}" version="1" {spec["namespaces"]}>'
        f'<metadata><schema>ADL SCORM</schema><schemaversion>{spec["schema_version"]}</schemaversion></metadata>'
        f'<organizations default="{org_id}">'
        f'<organization identifier="{org_id}"><title>{title}</title>'
        f'<item identifier="{item_id}" identifierref="{res_id}"><title>{title}</title></item>'
        f'</organization></organizations>'
        f'<resources><resource identifier="{res_id}" type="webcontent" '
        f'{spec["scormtype_attr"]}="sco" href="index.html">{file_tags}</resource></resources>'
        f'</manifest>'
    )


def _wire_in_scorm_api(index_html: str, version: str) -> str:
    # Whichever player build_export_folder shipped — the asset keeps its source
    # filename, so this tracks export.EXPORT_PLAYER rather than hardcoding one.
    player_js = f"assets/{export.EXPORT_PLAYER}.js"
    tag = f'<script src="{player_js}"></script>'
    if tag not in index_html:
        raise export.ExportError(
            f"index.html changed — expected {player_js} tag not found")
    # read by static/scorm-api.js to pick window.API (1.2) vs window.API_1484_11
    # (2004) and the matching CMI field names — must run before that script
    version_flag = f'<script>window.RC_SCORM_VERSION={json.dumps(version)};</script>\n'
    return index_html.replace(
        tag, version_flag + '<script src="assets/scorm-api.js"></script>\n' + tag)


def build_export_scorm_folder(walkthrough_id: str, out_dir: Path, brand: dict = None,
                              skip_intro: bool = False, version: str = "2004") -> Path:
    """Assemble a SCORM content package as a folder: out_dir/imsmanifest.xml,
    out_dir/index.html, out_dir/assets/ (same as export.build_export_folder,
    plus assets/scorm-api.js). Wipes out_dir first. Returns out_dir.

    `brand` is a resolved brand profile, exactly as in export.build_export — the
    parameter is not called `branding` because that shadows the module of the
    same name, which is how main() came to pass the module itself.

    `version` is "2004" (default) or "1.2".
    """
    _check_version(version)
    out_dir = Path(out_dir)
    export.build_export_folder(walkthrough_id, out_dir, brand, skip_intro, chrome="full")

    (out_dir / "assets" / "scorm-api.js").write_bytes(
        (STATIC_DIR / "scorm-api.js").read_bytes())

    index_path = out_dir / "index.html"
    index_path.write_text(_wire_in_scorm_api(index_path.read_text(encoding="utf-8"), version),
                          encoding="utf-8")

    # The row, for the manifest's identifiers and title. This is the second read
    # of it in one build (export._load_walkthrough_and_pages made the first), and
    # it stays a second read rather than a threaded-through argument: a primary
    # key lookup is nothing against the megabytes this function has just written,
    # and export's builders keep their id-in, bytes-out shape.
    row = export._row(walkthrough_id)

    files = sorted(
        str(p.relative_to(out_dir)).replace("\\", "/")
        for p in out_dir.rglob("*") if p.is_file()
    )
    (out_dir / "imsmanifest.xml").write_text(
        build_manifest(row, files, version=version), encoding="utf-8")
    return out_dir


def build_export_scorm_zip(walkthrough_id: str, brand: dict = None,
                           skip_intro: bool = False, version: str = "2004") -> bytes:
    """Build the SCORM folder package (see build_export_scorm_folder) and pack
    it into a ZIP archive with imsmanifest.xml at the archive ROOT (SCORM
    requires this — unlike export.build_export_zip's <slug>/ nesting).
    Returns the zip bytes.

    The staging directory is a TemporaryDirectory on local disk. Nothing about
    it is user data worth keeping: it is assembled, zipped and deleted inside
    one request, and the only copy anybody keeps is the zip the LMS receives.
    """
    with tempfile.TemporaryDirectory(prefix="rc-scorm-") as tmp:
        folder = build_export_scorm_folder(walkthrough_id, Path(tmp) / "package",
                                           brand, skip_intro, version)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(folder)))
        return buf.getvalue()


def main():
    args = sys.argv[1:]
    profile_id = None
    for flag in ("--profile", "--brand"):
        if flag in args:
            i = args.index(flag)
            profile_id = args[i + 1] if i + 1 < len(args) else ""
            del args[i:i + 2]
            break

    version = "2004"
    if "--version" in args:
        i = args.index("--version")
        version = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]

    if not args:
        print(__doc__.strip())
        sys.exit(1)
    walkthrough_id = args[0]

    try:
        row = export._row(walkthrough_id)
    except export.ExportError as e:
        print(f"export failed: {e}")
        sys.exit(1)
    stem = export.export_basename(row)

    brand = None
    if profile_id is not None:
        # Owner from the row, not from a session — see the same call in
        # export.main() for why the CLI has to supply one.
        brand = branding.resolve_branding_profile(profile_id, row["owner_id"])
    brand_note = f" [brand: {brand['brand_name']}]" if brand else ""

    try:
        out = Path(args[1]) if len(args) > 1 else ROOT / "exports" / f"{stem}-scorm.zip"
        # `brand` — the profile resolved four lines up — not `branding`, the
        # imported module. This passed the module, on every run and not only
        # when --profile was given, so `python3 scorm.py <id>` died in
        # export._download_branding_images on `dict(brand)` with "TypeError:
        # 'module' object is not iterable". A module is truthy, so neither that
        # function's `if not brand` guard nor apply_branding's caught it; the
        # resolved profile went nowhere but the brand_note printed above.
        # Renaming the parameter `brand` is what stops it recurring: the bug was
        # a local variable and a module competing for one name.
        zip_bytes = build_export_scorm_zip(walkthrough_id, brand, version=version)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zip_bytes)
        print(f"exported {stem} -> {out} "
              f"({len(zip_bytes) // 1024} KB) [SCORM {version}]{brand_note}")
    except export.ExportError as e:
        print(f"export failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
