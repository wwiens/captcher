"""Catalog thumbnails: one downscaled frame per walkthrough.

The catalog's shelf shows the capture each walkthrough was built from. That is
the only imagery this product is allowed to show — a real frame from the
customer's own screen, never generated, stylised or tinted (UI-SYSTEM.md) —
and it is also the fastest way to find something in a library: a returning
author recognises the invoice screen well before they read the word "invoice".

Where a frame comes from, in order:

  1. `screenshot.png` in the first captured step. The extension writes this at
     capture time, at the viewport the author actually had. It is the truest
     answer and it is already in the captures folder.
  2. `render.png` in the same folder. Written by render_check.py when a render
     check has been run. Second-best: it is this server's replay of the
     captured page rather than the original screen, which is exactly the
     difference a render check exists to measure.
  3. The same two files in any later step, for a capture whose first step
     predates screenshots.
  4. Nothing. Some captures are page.html only. The route 404s and the card
     falls back to a plate — see renderThumb() in _catalog-script.html.

There is deliberately no on-demand Playwright render here. Rendering a page
takes seconds, this is a threaded HTTP server, and a shelf of twelve cards
would fire twelve of them at once on first paint. Backfilling renders is what
the render check already does as a background job; this route only ever serves
what is already there.

Pillow is optional. With it, the source is downscaled once and cached in the
capture prefix as `thumb.webp` (or `thumb.png` on a Pillow built without WebP);
without it the full frame is served and the only cost is bytes on the wire.

There are three caches in front of a card image, and they answer different
questions:

  * the **stored** file, which is what stops the downscale running twice;
  * the **process memo**, which is what stops a repeat request touching
    storage at all — the stored cache still cost a LIST and a GET per card,
    which is where a cold twelve-card shelf spent most of its wait;
  * the **browser**, via `Cache-Control` and the `ETag` this module hands the
    route, which is what stops the request being made.

WHAT REPLACED THE MTIME CHECK

The cache used to be validated by comparing mtimes: serve `thumb.png` when it
is at least as new as its source, regenerate otherwise. Storage objects have a
LastModified, but `blobs.list_keys` returns keys and nothing else, so nothing
here can see it — and the obvious substitutes are worse than they look. A
digest comparison would have to read the full-size source to hash it, which is
precisely the transfer the cache exists to avoid; it would make every cache hit
cost more than a miss.

So the cache is validated by presence: a `thumb.webp` in a step folder is the
thumbnail for that folder, full stop. That is safe here because of something
the uuid scheme changed. A capture's prefix is its walkthrough's id, and ids
are never reused, so an upload can no longer land on top of an existing
`screenshot.png` — the re-capture that used to invalidate a thumbnail now
creates a different walkthrough with a different prefix. The one write that
still replaces a source in place is the render check's `render.png`, and it
calls `invalidate()` when it lands.

The trade-off is a real one and worth naming: correctness now depends on the
writers remembering to invalidate, where mtimes made it automatic. A forgotten
call is a permanently stale card with nothing to notice it. The fix, when
`blobs` grows a listing that carries object metadata, is to compare
LastModified on the listing this module already makes — one extra field on a
call that is happening anyway, and the automatic behaviour comes back.
"""
import collections
import hashlib
import io
import re
import threading

from . import blobs
from . import store

# Wide enough for a 300px grid cell on a 2x display, and no wider. A captured
# viewport is typically 1280-1512 CSS px, so this is roughly a halving.
THUMB_WIDTH = 640

# WebP at 82 is the format the cards are actually served in: a 640px frame of a
# UI lands around 20-40KB against 100-250KB for the same frame as PNG, and the
# card paints it at ~300px, where the loss is not visible. The captured frame
# itself is untouched — this is the derived card image, not the capture.
#
# PNG is the fallback for a Pillow built without WebP, and the name is part of
# the cache key rather than a detail: the two live side by side in the captures folder
# and a library made before this change is upgraded on first read (see
# `_rebuild`). THUMB_NAMES is best-first, which is also upgrade order.
WEBP_QUALITY = 82
THUMB_WEBP = "thumb.webp"
THUMB_PNG = "thumb.png"
THUMB_NAMES = (THUMB_WEBP, THUMB_PNG)
CONTENT_TYPES = {THUMB_WEBP: "image/webp", THUMB_PNG: "image/png"}

# what a capture calls a frame, best first
FRAME_NAMES = ("screenshot.png", "render.png")

STEP_DIR = re.compile(r"^step-\d+$")
IMAGE_SUFFIXES = (".png", ".webp")

# One finished card image, ready to send.
Thumb = collections.namedtuple("Thumb", "data content_type etag")

# ---- the in-process cache -------------------------------------------------
# The bucket cache above removed the downscale from a repeat request; it did
# not remove the network. Serving a cached thumbnail still cost a LIST to see
# it was there and a GET to fetch it — two storage round trips per card, so
# twenty-four of them before a twelve-card shelf painted its first pixel, on
# every cold browser cache.
#
# This holds the finished bytes in the process, so a repeat request costs no
# storage traffic at all and can answer an If-None-Match with a 304 without
# touching the captures folder. It is keyed on the walkthrough id alone, which is safe
# because the id is a uuid and the route resolves ownership from the member's
# own rows *before* it gets here — a member who cannot name the id cannot
# reach the entry.
#
# Bounded both ways: entries and bytes, oldest evicted first. The bound is what
# makes it safe on a long-running server with a large library; without it this
# is a memory leak wearing a cache's clothes.
MEMO_MAX_ENTRIES = 512
MEMO_MAX_BYTES = 32 * 1024 * 1024

_memo = collections.OrderedDict()   # walkthrough_id -> Thumb
_memo_bytes = 0
_memo_lock = threading.Lock()


def _memo_get(walkthrough_id):
    with _memo_lock:
        hit = _memo.get(walkthrough_id)
        if hit is not None:
            _memo.move_to_end(walkthrough_id)
        return hit


def _memo_put(walkthrough_id, thumb):
    global _memo_bytes
    with _memo_lock:
        old = _memo.pop(walkthrough_id, None)
        if old is not None:
            _memo_bytes -= len(old.data)
        _memo[walkthrough_id] = thumb
        _memo_bytes += len(thumb.data)
        while _memo and (len(_memo) > MEMO_MAX_ENTRIES
                         or _memo_bytes > MEMO_MAX_BYTES):
            _, dropped = _memo.popitem(last=False)
            _memo_bytes -= len(dropped.data)


def _memo_drop(walkthrough_id):
    global _memo_bytes
    with _memo_lock:
        gone = _memo.pop(walkthrough_id, None)
        if gone is not None:
            _memo_bytes -= len(gone.data)


def _as_thumb(data, name):
    """Finished bytes plus what the route has to say about them.

    The ETag is a digest of the bytes rather than anything about the object
    behind them, because it has to survive the two things that legitimately
    change those bytes without changing their name: a re-encode at a different
    quality, and the PNG-to-WebP upgrade. A digest is right whatever did it.
    """
    return Thumb(data, CONTENT_TYPES.get(name, "image/png"),
                 '"' + hashlib.sha256(data).hexdigest()[:32] + '"')


def _png_index(walkthrough_id):
    """Every image in the capture, as a set of capture-relative paths.

    One listing serves the whole lookup below — the frame search, the step
    fallback and the cache probe. The predecessor stat'ed each candidate, which
    was free on a local disk and is a round trip per candidate here: a card with
    no frame at all would have paid for two HEADs per step folder before
    answering "none".
    """
    return {rel for rel in store.capture_files(walkthrough_id)
            if rel.lower().endswith(IMAGE_SUFFIXES)}


def _step_folders(d, index):
    """Captured step folders, in the order the walkthrough uses them.

    The definition's own step order is authoritative — a walkthrough may skip
    or reorder captured steps — so it is read first, and the folders in the
    bucket are the fallback for a definition whose steps carry no page.
    """
    seen, out = set(), []
    for step in ((d or {}).get("steps") or []):
        page = str((step or {}).get("page") or "")
        folder = page.split("/", 1)[0]
        if folder and folder not in seen:
            seen.add(folder)
            out.append(folder)
    if not out:
        found = {rel.split("/", 1)[0] for rel in index if "/" in rel}
        out = sorted(f for f in found if STEP_DIR.match(f))
    return out


def _frame_in(index, d):
    """The best frame present in `index`, or None. See source_frame."""
    for folder in _step_folders(d, index):
        for name in FRAME_NAMES:
            rel = f"{folder}/{name}"
            if rel in index:
                return rel
    return None


def source_frame(walkthrough_id, d):
    """The best captured frame for this walkthrough, or None.

    A capture-relative path — `"step-000/screenshot.png"` — not a Path and not
    a key. The first step wins outright: it is the screen the learner opens on,
    and it is what makes a card recognisable. Later steps are searched only when
    the first has no frame at all.

    The containment check the disk version needed is gone with the disk. A
    step's `folder` still comes out of the definition and is still untrusted,
    but it is now only ever matched against a listing of objects that actually
    exist under this walkthrough's prefix, and `blobs.key_for` refuses anything
    unsafe on the read that follows.
    """
    return _frame_in(_png_index(walkthrough_id), d)


_preferred = None


def preferred_name():
    """The cache object name this install writes, probed once.

    Pillow can be built without WebP, and on such a build every re-encode
    would silently fall back to PNG — so "is the cached object the format we
    write?" has to be answerable without attempting an encode. `features.check`
    answers it directly. With no Pillow at all nothing is written to the captures folder
    at all, and PNG is the honest name for the untouched source frame that gets
    served instead.
    """
    global _preferred
    if _preferred is None:
        try:
            from PIL import features
            _preferred = THUMB_WEBP if features.check("webp") else THUMB_PNG
        except ImportError:
            _preferred = THUMB_PNG
    return _preferred


def _downscale(data: bytes):
    """`(bytes, name)` for a width-capped card image, or None.

    None means Pillow is missing or the source is not a readable image; the
    caller serves the original frame instead.

    WebP first. A capture is a screenshot of a UI — flat fills and text — and
    the old reasoning against a lossy format was JPEG's ringing around exactly
    that. WebP does not ring, and at 82 a 640px frame is a fifth to a tenth of
    the PNG for a card that paints at ~300px. PNG stays as the fallback for a
    Pillow built without WebP, so the format is discovered rather than assumed:
    an install without it keeps working and keeps writing thumb.png.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            if im.width > THUMB_WIDTH:
                height = max(1, round(im.height * THUMB_WIDTH / im.width))
                im = im.resize((THUMB_WIDTH, height), Image.LANCZOS)
            # The write-to-temp-then-replace the disk version used is gone: a
            # PUT of the finished bytes either lands whole or does not land, so
            # there is no window in which a half-written thumbnail is readable.
            name = preferred_name()
            out = io.BytesIO()
            if name == THUMB_WEBP:
                im.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
            else:
                im.save(out, "PNG", optimize=True)
            return out.getvalue(), name
    except Exception:
        # A corrupt or truncated capture frame is not worth failing a page
        # load over; the caller serves the original instead.
        return None


def invalidate(walkthrough_id, folder):
    """Drop a step folder's cached thumbnail, in the captures folder and in memory.

    Called by anything that rewrites a frame in place — today only the render
    check, writing `render.png`. Unconditional rather than conditional on which
    frame the thumbnail was actually derived from: getting that wrong in the
    "keep" direction leaves a card wrong forever, and getting it wrong in the
    "drop" direction costs one downscale on the next request.

    Both names go, not just the one this install writes: a captures folder that still
    holds a `thumb.png` from before the WebP change would otherwise keep
    answering with it.

    The in-process entry goes first and always. A file delete that fails is
    a stale object the next request rebuilds over; a memo entry left behind is
    a stale card for as long as the server runs, and it is the one part of
    this that cannot fail.

    Best-effort otherwise. A cache that could not be cleared must not fail the
    render check that cleared it.
    """
    _memo_drop(walkthrough_id)
    for name in THUMB_NAMES:
        key = blobs.key_for(walkthrough_id, f"{folder}/{name}")
        if key is None:
            continue
        try:
            blobs.delete(key)
        except blobs.StorageError:
            pass


def _first_folder(d):
    """The step folder a thumbnail is almost always in, from the definition
    alone — no listing. The first step is the screen the learner opens on, and
    `source_frame` already prefers it outright."""
    folders = _step_folders(d, ())
    return folders[0] if folders else None


def _cached(walkthrough_id, folder):
    """The cached card image already in the captures folder under `folder`, or None.

    One direct read rather than a listing. That is the whole point: the listing
    this replaces was a round trip spent to learn something the read itself
    reports, and it ran on every request including the hits.

    Only the name this install writes is tried. A library still holding a
    legacy `thumb.png` misses here and is answered — and upgraded — by the full
    path below, once; probing for it on every request would put a second round
    trip on the hot path forever to save one on a path taken once.
    """
    return store.capture_bytes(walkthrough_id, f"{folder}/{preferred_name()}")


def _rebuild(walkthrough_id, folder, src_rel):
    """Downscale the source frame, store it, and return a Thumb — or None if
    the source itself has gone.

    A legacy `thumb.png` beside a freshly written `thumb.webp` is deleted
    rather than left: two answers to one question in the captures folder is how a cache
    starts serving whichever one it listed first.
    """
    data = store.capture_bytes(walkthrough_id, src_rel)
    if not data:
        return None
    made = _downscale(data)
    if made is None:
        # No Pillow, or an unreadable frame. Serving the full-size source costs
        # bytes on the wire and nothing else, and it keeps the card populated.
        return _as_thumb(data, THUMB_PNG)
    small, name = made
    try:
        store.put_capture(walkthrough_id, f"{folder}/{name}", small,
                          CONTENT_TYPES[name])
    except (blobs.StorageError, ValueError):
        # The thumbnail is a cache. Failing to persist it means the next
        # request pays for the downscale again, not that this one fails.
        pass
    else:
        for stale in THUMB_NAMES:
            if stale == name:
                continue
            key = blobs.key_for(walkthrough_id, f"{folder}/{stale}")
            if key is None:
                continue
            try:
                blobs.delete(key)
            except blobs.StorageError:
                pass
    return _as_thumb(small, name)


def thumbnail(walkthrough_id, d):
    """A `Thumb` for this walkthrough's catalog card, or None if it has no frame.

    Bytes rather than a path, because the caller can no longer sendfile() its
    way out of the question — the object is in the captures folder and something has to
    carry it. The ETag rides along so the route can answer a revalidation
    without reading anything.

    Three tiers, cheapest first:

      1. the in-process memo — no network at all;
      2. a direct read of the cached image in the first step's folder — one
         round trip, and the common case for every walkthrough whose first
         step has a frame, which is nearly all of them;
      3. the full listing, which finds a frame in a later step folder and is
         also what answers "this capture has no frame at all".

    The downscaled copy is cached beside its source; see the module docstring
    for why presence is the whole staleness rule and what invalidates it.
    """
    hit = _memo_get(walkthrough_id)
    if hit is not None:
        return hit

    # Tier 2. `_first_folder` reads the definition, which the caller already
    # has in hand, so this costs nothing to try and skips the listing outright
    # when it lands.
    folder = _first_folder(d)
    if folder:
        cached = _cached(walkthrough_id, folder)
        if cached:
            thumb = _as_thumb(cached, preferred_name())
            _memo_put(walkthrough_id, thumb)
            return thumb

    # Tier 3.
    index = _png_index(walkthrough_id)
    src_rel = _frame_in(index, d)
    if src_rel is None:
        return None
    folder = src_rel.split("/", 1)[0]

    # Only the preferred name is worth reading. An object under one of the
    # others is a thumbnail this install no longer writes, and fetching it to
    # serve it once would be a round trip spent on an answer `_rebuild` is
    # about to replace anyway.
    cached_rel = f"{folder}/{preferred_name()}"
    if cached_rel in index:
        data = store.capture_bytes(walkthrough_id, cached_rel)
        if data:
            thumb = _as_thumb(data, preferred_name())
            _memo_put(walkthrough_id, thumb)
            return thumb
        # Listed but unreadable: deleted between the listing and the read, or
        # never finished uploading. Fall through and rebuild it.

    thumb = _rebuild(walkthrough_id, folder, src_rel)
    if thumb is not None:
        _memo_put(walkthrough_id, thumb)
    return thumb
