#!/usr/bin/env python3
"""
Render-accuracy checking: does the player show what the user actually saw?

The capture extension saves a viewport screenshot per step (ground truth,
taken before the SingleFile pass and its deliberate alterations). This module
renders each captured page the way the live player serves it — headless
Chromium pointed at the server's own /capture/ route, bridge injection and
frame unlocking included — screenshots the result, and asks Claude (vision)
to compare the two. Verdicts land in the walkthrough's `render_check` column,
which the review page displays and author.py reads back (`render_notes`): a
page the check judged unusable earns a validation warning, and the AI edit pass
is told not to describe what does not render. Both are advisory — see the note
on author.RENDER_NOTE_SEVERITIES.

Run on demand (POST /api/walkthroughs/<id>/render-check), never automatically:
a run costs a model call per unique page.

WHERE THE VERDICTS AND THE RENDERS WENT

Two different destinations, for two different reasons. The verdicts were a
sidecar file (`walkthroughs/<id>/render_check.json`) written temp-then-replace
so a half-written verdict set could never be read; they are a jsonb column now,
and a column update is atomic without anybody having to arrange it — the
temp-then-replace dance goes away rather than being ported.

The renders are blobs and stay blobs: `render.png` is written into the capture
prefix beside the `screenshot.png` it will be compared against, because the
review page and the catalog card both serve it byte-for-byte.

Two seams for tests: `render_pages` (the Playwright renderer — imported
lazily, so the server runs without Playwright until a check is requested) and
`author.call_claude_vision` (the verdict). Both are patched in
tests/test_render_check.py.
"""
import contextvars
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import author
from . import store
from . import thumbs
from .author import AuthorError

# The reader lives in store.py — author.py consumes the verdicts and this
# module imports author, so the reverse import would be a cycle.
SCHEMA = "render-check@1"
def capture_viewports(row) -> dict:
    """{folder: {width, height, dpr}} as recorded, for the headless render.

    They arrive in the definition's `_source` block, which the authoring pass
    removes from the definition by design. The draft column keeps the
    pre-authoring definition for good, so after authoring the sizes come from
    there; without this an authored walkthrough rendered at DEFAULT_VIEWPORT
    and every check reported a layout shift that was only a window size.
    """
    for col in ("definition", "draft"):
        d = row.get(col) or {}
        found = (d.get("_source") or {}).get("viewports") if isinstance(d, dict) else None
        if found:
            return found
    return {}


# Viewport when a capture predates the viewport metadata.
DEFAULT_VIEWPORT = {"width": 1280, "height": 800, "dpr": 1}
# Per-page ceilings for the headless render.
PAGE_LOAD_TIMEOUT_MS = 30_000
SETTLE_MS = 750

VISION_SYSTEM = """\
You compare two screenshots of the same product screen and judge whether the
second one is a faithful re-render of the first.

Image 1 is ground truth: a viewport screenshot taken in the user's browser at
capture time. Image 2 is how a training player renders a frozen HTML snapshot
of that same page.

Known-benign differences you MUST ignore:
- ad or tracker slots rendered as empty boxes (they are blocked on purpose)
- image recompression artifacts, slightly softer images
- scrollbars, text cursors, mouse cursors, focus rings
- tiny font antialiasing or sub-pixel layout shifts
- live data that legitimately changes (clocks, counters, avatars failing to
  load as broken-image icons in otherwise intact layout is NOT benign)

Report real rendering failures: blank or white regions where content existed,
missing panels or iframes, unstyled text (fonts or CSS not applied), overlapping
or collapsed layout, missing images that hold layout together, error banners or
toasts that were not on the original.

Report your verdict in the required JSON format: `matches` (is the render
faithful), `severity`, and `differences` (one short sentence per real
difference).

"ok": faithful. "minor": noticeable but the screen is still usable for
training. "broken": a learner would see an obviously wrong or unusable screen.
"""

# Structured-output schema for the verdict — the API guarantees the response
# text is JSON matching this, so severity typos and prose wrappers cannot
# happen on the wire. (compare_page still keeps its defensive fallback: tests
# stub the model call with arbitrary strings.)
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["ok", "minor", "broken"]},
        "differences": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["matches", "severity", "differences"],
    "additionalProperties": False,
}

# A verdict is three small fields — deep thinking buys nothing here, and
# max_tokens still covers thinking plus text, so both are kept small.
VISION_EFFORT = "low"
VISION_MAX_TOKENS = 4000
# Verdicts for different pages are independent, so they are judged on a small
# thread pool (the anthropic client is thread-safe). Kept modest to stay
# polite to rate limits. Note the LangSmith spans opened on worker threads
# lose their parent chain span (nesting is threading.local) and appear as
# root runs — accepted; the trace queue itself is thread-safe.
VISION_CONCURRENCY = 4


class RenderCheckError(Exception):
    """Anything that should surface to the user as a clean message."""


def load_results(walkthrough_id: str):
    """The stored verdicts, or None if no check has been run.

    Kept for callers holding only an id — the GET route reaches it off a URL.
    Anything that already has the row (the catalog build, the authoring job)
    should call `store.load_render_check(row)` instead and save the query: the
    verdicts are a column on the row it is already holding.
    """
    return store.load_render_check(store.get(walkthrough_id))


def render_pages(base_url: str, cookie_header, walkthrough_id: str, jobs: list):
    """Render each page through the live /capture/ route and screenshot it.

    jobs: [(page_rel, viewport_dict_or_None, out_rel)]. Returns the page_rels
    that produced a render. Raises RenderCheckError if Playwright is
    unavailable; per-page failures raise out of the page loop (the caller
    records them per page). Playwright is a lazy import so the server runs
    without it until a check is requested.

    `out_rel` is a capture-relative path — `"step-000/render.png"` — where the
    old signature took a filesystem Path. Writing the shot is still this
    function's job, as it always was; only the destination changed. It is
    `page.screenshot()` returning bytes rather than `page.screenshot(path=...)`
    writing a file, and one object per page rather than one file, so the bytes
    are never all in this process at once — the judging pass reads each render
    back at its own (bounded) concurrency, exactly as it used to read them back
    off the disk.

    The pages are fetched from the running server itself (ThreadingHTTPServer,
    so serving ourselves mid-request is fine) because that is exactly what the
    player shows: bridge injected, frame sandboxes unlocked, and — like the
    live player, unlike exports — no FREEZE_SHIM.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        from .install import VERIFY_HELP
        raise RenderCheckError(VERIFY_HELP)

    cookies = []
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append({"name": name, "value": value, "url": base_url})

    rendered = []
    with sync_playwright() as p:
        try:
            # --disable-dev-shm-usage: a container's /dev/shm is 64 MB, which a
            # large captured page overflows, and Chromium then crashes the tab
            # mid-render. It spills to /tmp instead; harmless on a dev box.
            browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        except Exception as e:
            raise RenderCheckError(
                f"Headless Chromium failed to launch ({e}). Run "
                "`captcher install-browser` once.")
        try:
            for page_rel, viewport, out_rel in jobs:
                vp = {**DEFAULT_VIEWPORT, **(viewport or {})}
                context = browser.new_context(
                    viewport={"width": int(vp["width"]), "height": int(vp["height"])},
                    device_scale_factor=float(vp.get("dpr") or 1))
                try:
                    if cookies:
                        context.add_cookies(cookies)
                    pg = context.new_page()
                    url = f"{base_url}/capture/{walkthrough_id}/{page_rel}"
                    pg.goto(url, wait_until="networkidle",
                            timeout=PAGE_LOAD_TIMEOUT_MS)
                    pg.wait_for_timeout(SETTLE_MS)
                    store.put_capture(walkthrough_id, out_rel,
                                      pg.screenshot(), "image/png")
                    rendered.append(page_rel)
                finally:
                    context.close()
        finally:
            browser.close()
    return rendered


# One copy again. This was briefly a local function, because author's took a
# Path and there is no path to read: a frame is an object in the captures folder and
# arrives here as bytes. author._image_block takes bytes now, so the double is
# gone — and with it the risk of the two drifting on media_type or encoding.
_image_block = author._image_block


def compare_page(baseline: bytes, render: bytes, page_rel: str, model: str) -> dict:
    """One vision verdict: {"matches": bool, "severity": ..., "differences": [...]}."""
    content = [
        {"type": "text", "text": f"Page: {page_rel}\n\nImage 1 — ground truth "
                                 "(capture-time viewport screenshot):"},
        _image_block(baseline),
        {"type": "text", "text": "Image 2 — the player's render of the saved snapshot:"},
        _image_block(render),
        {"type": "text", "text": "Compare them per your instructions and "
                                 "report your verdict."},
    ]
    verdict = author.extract_json(author.call_claude_vision(
        VISION_SYSTEM, content, model, effort=VISION_EFFORT,
        max_tokens=VISION_MAX_TOKENS, output_schema=VERDICT_SCHEMA))
    severity = verdict.get("severity")
    if severity not in ("ok", "minor", "broken"):
        severity = "ok" if verdict.get("matches") else "broken"
    differences = [str(d) for d in verdict.get("differences") or [] if str(d).strip()]
    return {"matches": bool(verdict.get("matches", severity == "ok")),
            "severity": severity, "differences": differences}


def run_check(walkthrough_id: str, base_url: str, cookie_header=None, model=None,
              progress=None) -> dict:
    """Check every captured page and store the verdicts on the walkthrough.

    Pages without a capture-time screenshot (every pre-screenshot capture)
    report "no-baseline" — the feature degrades cleanly on old captures.
    Byte-identical pages share one render + verdict via unique_pages.

    `progress(phase, message)` reports the two long stretches (headless
    render, vision verdicts) and is the cancellation point — see
    author.call_claude_agentic for why cancellation is cooperative. Raising
    out of a pool worker unwinds the whole check without storing anything,
    which is the right outcome: a half-judged verdict set read back as
    render notes would steer the authoring pass on partial information.
    """
    model = model or author._model_from_env()
    progress = progress or (lambda *_a, **_k: None)
    row = store.get(walkthrough_id)
    if row is None:
        raise RenderCheckError(f"No walkthrough named {walkthrough_id!r}.")
    viewports = capture_viewports(row)

    pages = author.snapshot_pages(walkthrough_id)
    if not pages:
        # This absorbs the old "the capture directory is missing" case as well.
        # There is no directory to be missing now — an empty prefix and a prefix
        # holding no page.html are the same condition, and they were always the
        # same answer to the person who asked for a check.
        raise RenderCheckError(f"No captured pages found for {walkthrough_id!r}.")
    uniques, alias_map = author.unique_pages(pages)

    # One listing instead of an existence probe per candidate: on disk a
    # missing screenshot.png was a stat, here it would be a round trip.
    frames = set(store.capture_files(walkthrough_id, ".png"))

    entries = {}
    jobs = []
    for rel, _html in uniques:
        folder = rel.split("/", 1)[0]
        if f"{folder}/screenshot.png" not in frames:
            entries[rel] = {"status": "no-baseline"}
            continue
        entries[rel] = {"status": "pending",
                        "screenshot": f"{folder}/screenshot.png",
                        "render": f"{folder}/render.png",
                        "viewport": viewports.get(folder) or None}
        jobs.append((rel, viewports.get(folder), f"{folder}/render.png"))

    if jobs:
        progress("render", f"rendering {len(jobs)} page(s) headlessly")
        try:
            rendered = set(render_pages(base_url, cookie_header, walkthrough_id, jobs))
        except RenderCheckError:
            raise
        except Exception as e:
            raise RenderCheckError(f"Headless render failed: {e}")
        judge_jobs = []
        for rel, _viewport, _out_rel in jobs:
            if rel not in rendered:
                entries[rel].update(
                    status="error",
                    error="the page did not produce a render screenshot")
                continue
            # A fresh render.png may be what the catalog card is derived from,
            # and the thumbnail cache is validated by presence alone — so the
            # writer invalidates. See the note in thumbs.py.
            thumbs.invalidate(walkthrough_id, rel.split("/", 1)[0])
            judge_jobs.append(rel)

        def _judge(rel):
            """One page's verdict, with the per-page error isolation the old
            sequential loop had — a failed verdict marks that page only."""
            folder = rel.split("/", 1)[0]
            progress("judging", f"comparing {rel} against its screenshot")
            # Both frames are fetched inside the worker, so the pool's width is
            # also the cap on how many pairs of PNGs are in memory at once.
            baseline = store.capture_bytes(walkthrough_id, f"{folder}/screenshot.png")
            render = store.capture_bytes(walkthrough_id, f"{folder}/render.png")
            if not baseline or not render:
                return rel, {"status": "error",
                             "error": "the render or its baseline could not be "
                                      "read back from storage"}
            try:
                verdict = compare_page(baseline, render, rel, model)
            except AuthorError as e:
                return rel, {"status": "error", "error": str(e)}
            return rel, {"status": verdict["severity"], **verdict}

        if judge_jobs:
            workers = min(VISION_CONCURRENCY, len(judge_jobs))
            # A worker thread starts with an empty context, so each judgement
            # runs in a copy of this one: that is what carries the usage
            # attribution (usage.attribute) to the vision calls. One copy per
            # task, because a context can be entered by one thread at a time.
            contexts = [contextvars.copy_context() for _ in judge_jobs]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for rel, update in ex.map(lambda ctx, job: ctx.run(_judge, job),
                                          contexts, judge_jobs):
                    entries[rel].update(update)

    # Byte-identical duplicates share their canonical page's verdict.
    for alias, canonical in alias_map.items():
        if alias != canonical and canonical in entries:
            entries[alias] = {**entries[canonical], "alias_of": canonical}

    results = {"schema": SCHEMA, "model": model,
               "checked_at": datetime.now(timezone.utc)
                             .isoformat(timespec="seconds").replace("+00:00", "Z"),
               "pages": entries}
    store.save_render_check(walkthrough_id, results)
    return results
