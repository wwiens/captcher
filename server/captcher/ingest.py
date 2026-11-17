#!/usr/bin/env python3
"""
ingest.py — convert a recorded capture (page-capture-walkthrough@1 schema)
into a DRAFT walkthrough definition (walkthrough-def@1) for the walkthrough player.

Usage:
    python3 ingest.py suspend-a-member.zip        # prints the draft definition

One player step per recorded action. The draft is deliberately rough:
instructions are placeholders and selectors are guesses from the action log —
an author reviews and edits before publishing. This is the seed of the
capture→walkthrough authoring pipeline for the full learning system.

WHAT THE STORAGE MOVE CHANGED HERE

Two things, and both were forced by the same fact: a walkthrough's id is now
assigned by the database, so it does not exist until the row does.

  - `extract_capture_zip` writes objects under `<uuid>/` in the `captures`
    bucket instead of files under `captures/<slug>/`. It therefore needs the
    id, which means the row has to exist before extraction can start.
  - `convert` no longer writes anything. It takes the manifest already parsed
    out of the ZIP and returns the definition, so the caller can create the row
    from it (`store.create`) and only then extract into the prefix that row's
    id names. It is now a pure function of the manifest — no I/O at all, which
    is also what makes it testable without a database or a bucket.

The caller's order is therefore: read the manifest, convert, create, extract.
If the extraction fails the row has to go with it (`store.delete`) — there is
no `shutil.rmtree` of a half-written capture folder any more, because there is
no folder.

`capture_dir` is gone from the definition entirely. See the note in store.py
for why it was a mistake; the short version is that the prefix is the id now
and `blobs.prefix_for` is the only code that knows it.
"""
import hashlib
import io
import json
import re
import sys
import zipfile

from . import blobs
from . import store


def slugify(s, fallback=""):
    """A url-safe slug, delegating to the one implementation in store.py.

    Kept as a name here because `zip_walkthrough_title` feeds it and the upload
    route has always reached for `ingest.slugify`. The default fallback is ""
    rather than store's "walkthrough": callers here branch on an empty result
    ("no usable title, derive one another way"), and a fallback that invents a
    title would turn that branch off silently.
    """
    return store.slugify(s, fallback)


def frame_path(action):
    """The action's recorded frame route, or []: one verified selector per
    hop from the captured top page down to the document the action happened
    in. Present only on actions recorded inside a same-origin iframe; the
    player's bridge and digest.match descend the same list."""
    path = action.get("framePath")
    if not isinstance(path, list):
        return []
    return [s.strip() for s in path if isinstance(s, str) and s.strip()]


def guess_target(action):
    """Target for a recorded action.

    Newer captures carry `selector` — computed by the recorder at event time
    and verified unique against the live DOM (querySelectorAll length 1), so
    it is used alone: adding a text key could only break a match the recorder
    already proved. Older captures fall back to the tag-plus-label guess,
    which authors (or the authoring pass) refine. An action recorded inside
    an iframe additionally carries the frame route (see frame_path); it is
    attached on both branches — even a label guess should search the document
    the action actually happened in, not the top page."""
    frames = frame_path(action)
    sel = action.get("selector")
    if isinstance(sel, str) and sel.strip():
        target = {"selector": sel.strip()}
    else:
        el = action.get("element") or ""
        label = (action.get("label") or "").strip()
        short = re.split(r"\s{2,}|\n", label)[0][:40].strip()
        sel = {"button": "button", "select": "select", "row": "tr", "a": "a"}.get(el, el or "*")
        target = {"selector": sel}
        if short:
            target["text"] = short
    if frames:
        target["frames"] = frames
    return target


def normalize_window(win):
    """Capture window from the capture manifest -> walkthrough-def form, or None.

    The extension records a drawn rectangle: {rect, viewport, padding, label,
    anchor}. `anchor` (optional) is the element beneath the rectangle —
    {selector, dx, dy} — which the player uses to keep the rectangle glued to
    fixed/centred elements (playback always runs through every captured page,
    even after that element leaves the page). Its selector is nested as a
    target (the same {selector[, text]} shape step actions use). Legacy element-picked windows ({selector, rect,
    …}) map to a top-level target instead.
    """
    if not isinstance(win, dict) or not (win.get("rect") or win.get("selector")):
        return None
    out = {}
    for key in ("rect", "viewport"):
        if isinstance(win.get(key), dict):
            out[key] = win[key]
    anchor = win.get("anchor")
    if isinstance(anchor, dict) and anchor.get("selector"):
        out["anchor"] = {"target": {"selector": anchor["selector"]},
                         "dx": anchor.get("dx", 0), "dy": anchor.get("dy", 0)}
    elif win.get("selector"):  # legacy: window picked as one element
        target = {"selector": win["selector"]}
        if win.get("text"):
            target["text"] = win["text"]
        out["target"] = target
    out["padding"] = win.get("padding", 0)
    if win.get("label"):
        out["label"] = win["label"]
    return out


# ==========================================================================
# Capture ZIP handling
# ==========================================================================
# Unpacking an uploaded capture archive into Storage. Moved here from server.py:
# this is capture ingestion, not HTTP. (The multipart parsing that produces
# the ZIP bytes stays in server.py — that is wire format, not ingestion.)

def zip_entry_name(info: zipfile.ZipInfo) -> str:
    """Recover UTF-8 entry names. zipfile decodes names as cp437 when the
    ZIP's UTF-8 flag isn't set (common for capture ZIPs), which turns an
    em dash into "ΓÇö" — and then page paths in the walkthrough never match."""
    if info.flag_bits & 0x800:  # UTF-8 flag set: name already correct
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


# The capture manifest is named walkthrough.json. Captures recorded before the
# lessons->walkthroughs rename name it lesson.json; accept either on ingest and
# normalise to walkthrough.json in the captures folder so the rest of the app is
# consistent — store.capture_manifest() looks for exactly one name.
# (capture-lab now writes walkthrough.json; the fallback is for older ZIPs.)
CAPTURE_MANIFEST = "walkthrough.json"
_LEGACY_MANIFEST = "lesson.json"
_MANIFEST_NAMES = (CAPTURE_MANIFEST, _LEGACY_MANIFEST)


def _manifest_anchor(name):
    """True if a ZIP entry name is the capture manifest at depth 0 or 1."""
    return name in _MANIFEST_NAMES or (
        name.count("/") == 1 and any(name.endswith("/" + m) for m in _MANIFEST_NAMES))


# --------------------------------------------------------------------------
# Decompression bounds
# --------------------------------------------------------------------------
# The server caps the *compressed* upload (MAX_UPLOAD), but a ZIP's inflation
# ratio is unbounded: a few hundred KB of zeros expands to gigabytes, and
# nothing below reads a size before the bytes are already through the
# decompressor.
#
# These are absolute ceilings rather than a ratio test. A ratio test gets this
# exactly backwards here — a capture step is inlined HTML and legitimately
# compresses 20:1 or better, so the ratio that rejects a bomb also rejects real
# captures, while a bomb built just under the threshold still passes.
#
# For scale: one real step is a few MB of inlined HTML and captures run to tens
# of steps, so both bounds sit far above anything seen in practice and far below
# "fills the disk".
#
# MAX_ENTRY_BYTES now bounds something it did not have to bound before: RAM.
# A Storage PUT signs a digest of the whole body, so an entry cannot be streamed
# out of the decompressor and into the socket the way it was streamed onto disk
# — it has to be a bytes object first. That is the one real cost of the move,
# and this ceiling is what keeps it finite. It is also less alarming than it
# looks: the caller is already holding the entire compressed archive in memory
# (server.py reads the multipart body whole), so the addition is one inflated
# entry at a time, not a new class of exposure. The alternative — S3 multipart
# upload, which would let a large entry go out in signed 5 MB parts — is a
# chunk-tracking protocol of its own, and nothing in a real capture comes close
# to this bound.
MAX_ENTRY_BYTES = 64 * 1024 * 1024        # one file inside the archive
MAX_TOTAL_BYTES = 1024 * 1024 * 1024      # the whole archive, expanded
_COPY_CHUNK = 1 << 20


def _read_bounded(src, entry_name, budget):
    """Inflate one ZIP entry into bytes, refusing to exceed its bounds.

    `ZipInfo.file_size` is written by whoever built the archive, so it is not
    trusted for this — the count is of bytes that actually come out of the
    decompressor, which is why the read stays chunked even though the result is
    accumulated. Reading `file_size` and allocating that would let a lying
    header do the damage the bounds exist to prevent.
    """
    out = bytearray()
    while True:
        chunk = src.read(_COPY_CHUNK)
        if not chunk:
            return bytes(out)
        out += chunk
        if len(out) > MAX_ENTRY_BYTES:
            raise ValueError(
                f"{entry_name} expands past {MAX_ENTRY_BYTES // (1024 * 1024)} MB — "
                "that is not a captured page")
        if len(out) > budget:
            raise ValueError(
                f"archive expands past {MAX_TOTAL_BYTES // (1024 * 1024)} MB — "
                "refusing to extract it")


def zip_manifest(zip_bytes: bytes) -> dict:
    """Read the capture manifest out of a ZIP (root or single wrapper folder)
    without extracting anything.

    The one place the manifest is parsed, because it is now read twice on the
    upload path — once to name and convert the capture, once more only if the
    caller wants the title — and two parsers would be two chances to disagree
    about which entry is the manifest.
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = {zip_entry_name(i): i for i in zf.infolist()}
    anchors = [n for n in names if _manifest_anchor(n)]
    if not anchors:
        raise ValueError("no walkthrough.json at the root of the archive — "
                         "zip the capture folder produced by the capture tool")
    try:
        # bounded: this runs before extraction, so it is the first thing a
        # bomb-shaped manifest would reach (zf.read() would inflate it whole)
        with zf.open(names[anchors[0]]) as f:
            raw = f.read(MAX_ENTRY_BYTES + 1)
        if len(raw) > MAX_ENTRY_BYTES:
            raise ValueError("walkthrough.json in the archive is implausibly large")
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError) as e:
        raise ValueError(f"could not read walkthrough.json from the archive: {e}")
    if not isinstance(data, dict):
        raise ValueError("walkthrough.json in the archive is not an object")
    return data


def zip_walkthrough_title(zip_bytes: bytes) -> str:
    """The capture's own title, for naming a walkthrough before it exists."""
    return str(zip_manifest(zip_bytes).get("title") or "")


def extract_capture_zip(zip_bytes: bytes, walkthrough_id: str) -> int:
    """Safely unpack a capture ZIP into one walkthrough's Storage prefix.

    Accepts walkthrough.json at the ZIP root or inside a single wrapper folder
    (the common result of zipping the folder itself). Skips macOS junk and
    refuses path traversal. Raises ValueError on anything unusable. Returns the
    number of objects written.

    `walkthrough_id` must already name a row: the prefix is the id, so the row
    has to exist before there is anywhere to put the bytes. That inverts the old
    order — extract, then convert, then write the definition — and it moves the
    failure cleanup with it. There is no directory to rmtree; a caller whose
    extraction fails removes the row it just created (`store.delete`, which
    clears the prefix too).

    Three safety properties carry over unchanged in intent:

      - the per-entry and whole-archive expansion ceilings, enforced on bytes
        out of the decompressor (see _read_bounded);
      - traversal rejection, now `blobs.key_for` returning None instead of a
        `relative_to` check on a resolved path. Object keys are literal strings
        with no `..` resolution, so a traversing key would not escape a prefix
        — but it would let two URLs address one object and sail past a prefix
        test written with startswith(), so it is still refused outright;
      - the legacy lesson.json -> walkthrough.json normalisation, which is now
        a destination rename decided before the upload rather than a file
        rename performed after it. Storage has no rename (it is a copy plus a
        delete), and doing it up front means the archive that would not produce
        a manifest is rejected before it costs a single PUT.
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    files = [(info, zip_entry_name(info)) for info in zf.infolist()]
    files = [(info, n) for info, n in files
             if not n.endswith("/")
             and not n.startswith("__MACOSX/")
             and not n.rsplit("/", 1)[-1].startswith(".")]
    if not files:
        raise ValueError("archive contains no files")

    # locate the manifest at depth 0 or 1 → prefix to strip
    anchors = [n for _, n in files if _manifest_anchor(n)]
    if not anchors:
        raise ValueError("no walkthrough.json at the root of the archive — "
                         "zip the capture folder produced by the capture tool")
    anchor = anchors[0]
    prefix = anchor[:anchor.rfind("/") + 1] if "/" in anchor else ""  # "" or "wrapper-folder/"

    inside = [(info, n, n[len(prefix):]) for info, n in files
              if n.startswith(prefix) and n != prefix]
    # An archive carrying both names keeps the modern one and leaves the legacy
    # entry where it fell, exactly as the on-disk rename did — renaming over a
    # real walkthrough.json would silently prefer the older manifest.
    renames = not any(rel == CAPTURE_MANIFEST for _, _, rel in inside)

    expected = blobs.prefix_for(walkthrough_id)
    plan = []
    for info, n, rel in inside:
        if renames and rel == _LEGACY_MANIFEST:
            rel = CAPTURE_MANIFEST
        # Not just "did key_for accept it" but "did it accept it unchanged".
        # key_for normalises — it strips a leading slash, because the /capture/
        # route hands it URL path fragments — so an entry named "/escaped.html"
        # would be accepted, contained, and stored at the same key as a sibling
        # entry named "escaped.html". Contained is not the same as intended, and
        # two archive entries collapsing onto one object is exactly the aliasing
        # key_for exists to prevent. An archive that needs normalising is
        # refused instead.
        if blobs.key_for(walkthrough_id, rel) != expected + rel:
            raise ValueError(f"unsafe path in archive: {n}")
        plan.append((info, n, rel))
    if not any(rel == CAPTURE_MANIFEST for _, _, rel in plan):
        raise ValueError("the archive contains no walkthrough.json to extract")

    total = 0
    for info, n, rel in plan:
        with zf.open(info) as src:
            data = _read_bounded(src, n, MAX_TOTAL_BYTES - total)
        total += len(data)
        store.put_capture(walkthrough_id, rel, data)
    return len(plan)


# --------------------------------------------------------------------------
# Published capture limits
# --------------------------------------------------------------------------
# What an authoring pass costs grows with the recording: every distinct page is
# a digest in the prompt, and its screenshot rides along up to
# author.MAX_INLINE_SCREENSHOTS. Byte-identical snapshots count once, exactly as
# author.unique_pages collapses them, so revisiting a page is free. Checked on
# upload before a row exists; the plans page states both numbers.
MAX_CAPTURE_STEPS = 40
MAX_CAPTURE_PAGES = 25


class CaptureTooLarge(ValueError):
    """A capture over a published limit. A ValueError, so any caller that does
    not tell it apart still refuses the capture; the upload route answers 413
    with the message, which is written for the person who recorded it."""


def _page_paths(manifest):
    """Capture-relative paths of every page snapshot the manifest names."""
    paths = set()
    initial = manifest.get("initialCapture") or {}
    if initial.get("folder"):
        paths.add(f"{initial['folder']}/page.html")
    for cap in manifest.get("steps") or []:
        html_name = (cap.get("files") or {}).get("html")
        if cap.get("folder") and html_name:
            paths.add(f"{cap['folder']}/{html_name}")
    return paths


def check_capture_limits(zip_bytes: bytes, manifest: dict, definition: dict):
    """Raise CaptureTooLarge if the capture is over MAX_CAPTURE_STEPS draft
    steps or MAX_CAPTURE_PAGES distinct pages."""
    # Recorded steps: convert() closes every draft with a synthetic "verify"
    # step that the person recording never took.
    steps = sum(1 for s in definition.get("steps") or [] if s.get("id") != "verify")
    if steps > MAX_CAPTURE_STEPS:
        raise CaptureTooLarge(
            f"This recording has {steps} steps and the limit is "
            f"{MAX_CAPTURE_STEPS}. Record the task in shorter parts.")
    paths = _page_paths(manifest)
    if len(paths) <= MAX_CAPTURE_PAGES:
        return   # cannot be over, however many are duplicates
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = {zip_entry_name(i): i for i in zf.infolist()}
    anchor = next(n for n in names if _manifest_anchor(n))
    prefix = anchor[:anchor.rfind("/") + 1] if "/" in anchor else ""
    digests, total = set(), 0
    for rel in paths:
        info = names.get(prefix + rel)
        if info is None:
            continue
        with zf.open(info) as src:
            data = _read_bounded(src, rel, MAX_TOTAL_BYTES - total)
        total += len(data)
        digests.add(hashlib.sha256(data).digest())
    if len(digests) > MAX_CAPTURE_PAGES:
        raise CaptureTooLarge(
            f"This recording visits {len(digests)} different pages and the limit "
            f"is {MAX_CAPTURE_PAGES}. Record the task in shorter parts.")


# The capture manifest's wire-format identifier. The extension has always
# advertised this, but nothing ever checked it, so a future @2 with different
# semantics would have been silently mis-ingested (PRD.md CAP-017).
#
# The "page-capture" in the name refers to an extension that is no longer part
# of this project; it is kept because captures already recorded carry it.
CAPTURE_SCHEMA = "page-capture-walkthrough@1"

# Older captures predate the schema field entirely; those are still accepted.
_ACCEPTED_SCHEMAS = (CAPTURE_SCHEMA, None, "")


def check_capture_schema(schema):
    """Raise ValueError unless the manifest declares a schema we understand."""
    if schema not in _ACCEPTED_SCHEMAS:
        raise ValueError(
            f"unsupported capture schema {schema!r} — this server understands "
            f"{CAPTURE_SCHEMA!r}. The capture extension is probably newer than "
            f"the server.")


def convert(manifest: dict, walkthrough_id: str = None):
    """A draft walkthrough definition from a capture manifest.

    Takes the manifest already parsed (`zip_manifest`, or
    `store.capture_manifest` for a capture that is already in the captures folder) and
    returns the definition. It writes nothing: the row is created from what
    comes back, by a caller that knows the owner.

    `walkthrough_id` is optional and usually absent, which is the honest
    consequence of the database assigning ids. The definition's `id` is a
    mirror of the row's primary key, kept because the player and the exports
    read it out of the definition they are handed — but at the moment of
    conversion the row does not exist yet, so there is nothing to mirror. Rather
    than invent a value that a later save would have to correct (which is how
    `capture_dir` came to disagree with the thing it pointed at), the key is
    simply omitted and stamped by whoever knows the id. Pass one here only when
    re-converting a capture that already has a row.
    """
    src = manifest if isinstance(manifest, dict) else {}
    check_capture_schema(src.get("schema"))
    # There is no folder name left to fall back on — a capture is a uuid prefix
    # now — so an untitled capture gets a placeholder the author will replace.
    title = str(src.get("title") or "").strip() or "Untitled capture"
    window = normalize_window(src.get("window"))

    steps = []
    # Per-folder capture viewport ({width, height, dpr}) — additive @1 metadata
    # from the extension, alongside the step's screenshot.png. The render check
    # matches its headless viewport to these; the screenshots themselves are
    # discovered in the captures folder as siblings of page.html, not through the
    # manifest.
    viewports = {}
    initial = src.get("initialCapture") or {}
    if initial.get("folder") and isinstance(initial.get("viewport"), dict):
        viewports[initial["folder"]] = initial["viewport"]

    # page shown for an action = the capture taken BEFORE that action.
    # Walk capture-steps; each one's page is the state, its actions happened
    # after the PREVIOUS capture — so attach actions to the previous page.
    prev_page = initial.get("folder")
    prev_page = f"{prev_page}/page.html" if prev_page else None

    for cap in src.get("steps", []):
        # A malformed manifest must produce a clean 400, not a KeyError that
        # surfaces as a 500. The extension always sends both fields; anything
        # hand-assembled or from a future schema version might not.
        folder = cap.get("folder")
        html_name = (cap.get("files") or {}).get("html")
        if not folder or not html_name:
            raise ValueError(
                f"capture step is missing folder or files.html: {cap!r:.120}")
        page = f"{folder}/{html_name}"
        if isinstance(cap.get("viewport"), dict):
            viewports[folder] = cap["viewport"]
        for action in cap.get("actions", []):
            a_type = action.get("type")
            recorded = (action.get("selector") or "").strip() \
                if isinstance(action.get("selector"), str) else ""
            frames = frame_path(action)
            if a_type == "click":
                step_action = {"type": "click", "target": guess_target(action)}
            elif a_type == "select":
                # optionValue is the <option>'s value attribute, recorded since
                # the metadata upgrade; the " — " split is the legacy fallback
                # for label text that concatenated a description
                value = action.get("optionValue")
                if not isinstance(value, str) or not value:
                    value = (action.get("value") or "").split(" — ")[0]
                target = {"selector": recorded or "select"}
                if frames:
                    target["frames"] = frames
                step_action = {"type": "select", "target": target,
                               "value": value}
            elif a_type == "input":
                target = {"selector": recorded or "textarea, input"}
                if frames:
                    target["frames"] = frames
                step_action = {"type": "input", "target": target,
                               "min_length": 3, "demo_value": action.get("value") or ""}
            elif a_type == "drag":
                # Recorded by a recorder that watches drops: the picked-up
                # element and the zone it was released in, both verified
                # unique at event time like a click's selector.
                drop = action.get("dropSelector")
                drop = drop.strip() if isinstance(drop, str) else ""
                if not recorded or not drop:
                    continue
                target, drop_target = {"selector": recorded}, {"selector": drop}
                if frames:
                    target["frames"] = frames
                    drop_target["frames"] = list(frames)
                step_action = {"type": "drag", "target": target,
                               "drop_target": drop_target}
            else:
                continue
            # where the action happened, when the recorder captured it — draft
            # context for the authoring pass, replaced along with the TODO
            where = " / ".join(s for s in (action.get("docTitle"),
                                           action.get("heading")) if s)
            steps.append({
                "id": f"step-{len(steps) + 1}",
                "title": f"TODO: name this step ({a_type})",
                "page": prev_page if not steps or steps[-1].get("_page") != prev_page else None,
                "_page": prev_page,  # stripped below; helps dedupe
                "instruction": f"TODO: instruction. Recorded action: {a_type} on "
                               f"{action.get('element')} \"{(action.get('label') or action.get('value') or '')[:60]}\""
                               + (f" ({where})" if where else ""),
                "showme_caption": "TODO: narration caption for Show-me mode.",
                "hint": "TODO: hint.",
                "action": step_action,
            })
        prev_page = page

    # final observe step on the last captured page
    if prev_page:
        steps.append({
            "id": "verify", "title": "TODO: verify the result", "page": prev_page,
            "instruction": "TODO: tell the learner what to check on the final screen.",
            "showme_caption": "TODO.", "action": {"type": "observe"},
        })

    # dedupe page fields (consecutive steps on the same page → page: null)
    last = None
    for s in steps:
        p = s.pop("_page", s.get("page"))
        s["page"] = p if p != last else None
        if p:
            last = p

    # No `capture_dir`. The capture lives under the walkthrough's own uuid in
    # the captures folder, so there is nothing to point at: `blobs.prefix_for(id)` is the
    # whole answer, and it is derived rather than stored precisely so it cannot
    # drift from the walkthrough that owns it.
    return {
        "schema": "walkthrough-def@1",
        **({"id": walkthrough_id} if walkthrough_id else {}),
        "title": title,
        "track": "TODO",
        "level": "snippet",
        "estimated_minutes": max(2, len(steps)),
        "intro": {"scenario": "TODO: scenario framing.", "objectives": ["TODO"]},
        **({"window": window} if window else {}),
        "steps": steps,
        "completion": {"summary": "TODO", "success_criteria": ["TODO"]},
        "_source": {"capture_schema": src.get("schema"), "recorded_at": src.get("createdAt"),
                    "recording": (src.get("recording") or {}).get("file"),
                    **({"viewports": viewports} if viewports else {})},
    }


if __name__ == "__main__":
    # A dry run, deliberately: creating a walkthrough now means inserting a row
    # owned by a member, and the command line has no member. What it is still
    # good for is seeing what a capture converts to — which steps came out of
    # which recorded actions — without paying for an upload first.
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        definition = convert(zip_manifest(f.read()))
    print(json.dumps(definition, indent=2, ensure_ascii=False))
    print(f"\n{len(definition['steps'])} steps generated — search for TODO "
          "and refine before use.", file=sys.stderr)
