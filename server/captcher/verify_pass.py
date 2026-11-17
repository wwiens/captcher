#!/usr/bin/env python3
"""
Replay-and-verify: does each step's spotlight land on the right, visible thing?

The authoring pass verifies targets structurally (a ref resolves to a
server-verified selector), and the render check verifies pages visually — but
nothing verifies the two together: a selector can match a perfectly present
element that renders as white space, or the "right" element in the wrong
visual spot for the hint that describes it. This module closes that gap by
replaying the published walkthrough the way the player does:

  1. Headless Chromium loads each step-bearing page through the live
     /capture/ route (bridge injected, frames unlocked — the player's view).
  2. For each step, the bridge's own {cmd:"probe"} spotlights the step's
     target (spotlight ring, scrolled into view) and reports its match
     count — a mechanical signal that costs nothing.
  3. One vision call per page judges the spotlighted screenshots: is the
     ringed element visible, and plausibly the control the step describes?
  4. Failures are handed to the AI edit pass as ONE repair instruction, then
     re-verified once. Still-failing steps surface as flagged entries — like
     every verdict in this pipeline, advisory: nothing blocks a write.

Verdicts land in the walkthrough row's `verify_check` jsonb column, via
store.save_verify_check / store.load_verify_check. They were a
`verify_check.json` sidecar next to walkthrough.json, written through a
mkstemp + os.replace dance to make the update atomic; a column update is
atomic because it is one statement, so the dance is gone with the file.

The probe screenshots this pass takes are NOT stored anywhere. They exist for
the seconds between the headless browser and the vision call, in a
TemporaryDirectory that is deleted on the way out — they are evidence for one
judgment, not an artefact of the walkthrough, and re-taking them is cheaper
than keeping them.

Seams for tests: `drive_pages` (the Playwright driver; lazy import so the
server runs without Playwright), `author.call_claude_vision` (the verdict),
and `author.run_edit` (the repair).
"""
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import author
from . import render_check
from . import store
from .author import AuthorError

SCHEMA = "verify-pass@1"
# One vision call per page carries every step on it; small fixed judgments.
VISION_EFFORT = "low"
VISION_MAX_TOKENS = 8000
# How long the bridge gets to answer a probe before the step is an error.
PROBE_TIMEOUT_MS = 3000
# Smooth scrollIntoView needs a beat to settle before the screenshot.
SCROLL_SETTLE_MS = 700

VERIFY_VISION_SYSTEM = """\
You check an interactive training walkthrough's step targets. Each image is
one step: the page as the player renders it, scrolled so the step's target is
in view, with the target carrying a bright 2px outline ring and a soft halo
of the same colour.

For each step, judge from its image:
- Is the ringed element actually visible — real rendered content, not a ring
  around blank/white space, a collapsed region, or an unstyled fragment?
- Is it plausibly the control the step's instruction and hint describe (right
  kind of element, right area of the screen)?

Set ok=false with one short problem sentence when the ring sits on empty
space, on the wrong kind of control, in a region that clearly did not render,
or when no ring is visible at all. Ignore image compression, font
antialiasing, and ad or tracker slots rendered as empty boxes.

Report via the required JSON: one entry per step id, in the order given."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "problem": {"type": "string"},
                },
                "required": ["id", "ok", "problem"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["steps"],
    "additionalProperties": False,
}

# The bridge answers {cmd:"probe"} with {type:"probe-result", count} — sent to
# parent, which IS the window when the capture page is loaded top-level, so an
# in-page listener hears it. -1 means the bridge never answered.
_PROBE_JS = """
(target) => new Promise((resolve) => {
  const h = (e) => {
    const m = e.data || {};
    if (m.type === "probe-result") {
      window.removeEventListener("message", h);
      resolve(m.count);
    }
  };
  window.addEventListener("message", h);
  window.postMessage({ cmd: "probe", target }, "*");
  setTimeout(() => { window.removeEventListener("message", h); resolve(-1); },
             __PROBE_TIMEOUT_MS__);
})
""".replace("__PROBE_TIMEOUT_MS__", str(PROBE_TIMEOUT_MS))


class VerifyError(Exception):
    """Anything that should surface to the user as a clean message."""


def load_results(walkthrough):
    """The stored replay verdicts, or None when this walkthrough has never been
    verified.

    Takes either a walkthrough row or an id. `store.load_verify_check` reads a
    column off a row it is handed, and server.py already holds the row from the
    ownership check it just did — making it re-fetch to read one of its own
    columns would be silly. The CLI and the tests have only an id, so this is
    the one place that decides whether a fetch is needed.
    """
    if isinstance(walkthrough, str):
        walkthrough = store.get(walkthrough)
    if not walkthrough:
        return None
    return store.load_verify_check(walkthrough)


def step_jobs(walkthrough: dict):
    """Group target-bearing steps by the page they play on.

    Returns [(page_rel, [(step_id, step_dict), ...])] in first-seen page
    order. Steps with page:null inherit the page in force; observe steps and
    steps with no resolvable page are skipped — there is nothing to ring.
    """
    by_page, order, current = {}, [], None
    for step in walkthrough.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("page") is not None:
            current = step["page"]
        target = (step.get("action") or {}).get("target")
        if current is None or not isinstance(target, dict):
            continue
        if current not in by_page:
            by_page[current] = []
            order.append(current)
        by_page[current].append((str(step.get("id") or "?"), step))
    return [(rel, by_page[rel]) for rel in order]


def drive_pages(base_url: str, cookie_header, walkthrough_id: str, jobs: list,
                out_dir: Path):
    """Probe and screenshot every step. A seam for tests.

    jobs: [(page_rel, [(step_id, step_dict), ...])]. Returns
    {step_id: {"count": int, "shot": str|None}} — count -1 means the bridge
    never answered the probe. Raises VerifyError when Playwright itself is
    unavailable; a page that fails to load marks its steps count -1.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        from .install import VERIFY_HELP
        raise VerifyError(VERIFY_HELP)

    cookies = []
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append({"name": name, "value": value, "url": base_url})

    results = {}
    with sync_playwright() as p:
        try:
            # See render_check.render_pages on the flag.
            browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        except Exception as e:
            raise VerifyError(
                f"Headless Chromium failed to launch ({e}). Run "
                "`captcher install-browser` once.")
        try:
            for page_rel, steps in jobs:
                context = browser.new_context(
                    viewport={"width": render_check.DEFAULT_VIEWPORT["width"],
                              "height": render_check.DEFAULT_VIEWPORT["height"]})
                try:
                    if cookies:
                        context.add_cookies(cookies)
                    pg = context.new_page()
                    url = f"{base_url}/capture/{walkthrough_id}/{page_rel}"
                    try:
                        pg.goto(url, wait_until="networkidle",
                                timeout=render_check.PAGE_LOAD_TIMEOUT_MS)
                        pg.wait_for_timeout(render_check.SETTLE_MS)
                    except Exception:
                        for step_id, _step in steps:
                            results[step_id] = {"count": -1, "shot": None}
                        continue
                    for step_id, step in steps:
                        action = step.get("action") or {}
                        target = action.get("target")
                        drop = (action.get("drop_target")
                                if action.get("type") == "drag" else None)
                        try:
                            extra = {}
                            if isinstance(drop, dict):
                                # the zone first: a probe rings and scrolls to
                                # its match, and the screenshot judged below
                                # must show the TARGET ringed
                                extra["drop_count"] = pg.evaluate(_PROBE_JS, drop)
                            count = pg.evaluate(_PROBE_JS, target)
                            pg.wait_for_timeout(SCROLL_SETTLE_MS)
                            shot = out_dir / f"{step_id}.png"
                            pg.screenshot(path=str(shot))
                            results[step_id] = {"count": count,
                                                "shot": str(shot), **extra}
                        except Exception:
                            results[step_id] = {"count": -1, "shot": None}
                finally:
                    context.close()
        finally:
            browser.close()
    return results


def probe_one(base_url: str, cookie_header, walkthrough_id: str,
              page_rel: str, target: dict, out_dir: Path):
    """Probe a single target on a single page: the edit loop's window into
    the player. Returns {"count": int, "shot": str|None} — a thin wrapper
    over drive_pages so the probe tool and the verify sweep can never
    disagree about what a probe means. Raises VerifyError when Playwright is
    unavailable."""
    driven = drive_pages(base_url, cookie_header, walkthrough_id,
                         [(page_rel, [("probe", {"action": {"target": target}})])],
                         out_dir)
    return driven.get("probe") or {"count": -1, "shot": None}


def _judge_page(page_rel: str, steps, driven: dict, model: str) -> dict:
    """One vision verdict for every ringed step on a page.

    Only steps whose probe matched (count >= 1) and screenshotted are judged;
    the caller has already classified the rest mechanically. Returns
    {step_id: {"ok": bool, "problem": str}}.
    """
    judgeable = [(sid, step) for sid, step in steps
                 if (driven.get(sid) or {}).get("count", -1) >= 1
                 and (driven.get(sid) or {}).get("shot")]
    if not judgeable:
        return {}
    content = [{"type": "text",
                "text": f"Page: {page_rel} — {len(judgeable)} step(s), one "
                        f"image each, in order."}]
    for sid, step in judgeable:
        content.append({"type": "text", "text": "\n".join(filter(None, [
            f"Step id: {sid}",
            f"instruction: {step.get('instruction') or ''}".strip(),
            f"hint: {step.get('hint') or ''}".strip(),
        ]))})
        # Read here rather than in _image_block: that takes bytes now, because
        # the frames it is usually handed are objects in the captures folder. These are
        # not — they are the probe screenshots in this sweep's TemporaryDirectory,
        # which is the one place in the pipeline a real file is still the right
        # answer (evidence for one judgment, deleted on the way out).
        content.append(author._image_block(Path(driven[sid]["shot"]).read_bytes()))
    content.append({"type": "text",
                    "text": "Judge each step per your instructions and report "
                            "the verdicts."})
    verdict = author.extract_json(author.call_claude_vision(
        VERIFY_VISION_SYSTEM, content, model, effort=VISION_EFFORT,
        max_tokens=VISION_MAX_TOKENS, output_schema=VERIFY_SCHEMA))
    out = {}
    for entry in verdict.get("steps") or []:
        if isinstance(entry, dict) and entry.get("id"):
            out[str(entry["id"])] = {"ok": bool(entry.get("ok")),
                                     "problem": str(entry.get("problem") or "")}
    return out


def _verify_once(walkthrough_id: str, walkthrough: dict, base_url: str,
                 cookie_header, model: str) -> dict:
    """One replay + judge sweep. Returns {step_id: entry}."""
    jobs = step_jobs(walkthrough)
    if not jobs:
        raise VerifyError(
            f"{walkthrough_id!r} has no target-bearing steps to verify.")
    entries = {}
    with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
        driven = drive_pages(base_url, cookie_header, walkthrough_id, jobs,
                             Path(tmp))
        for page_rel, steps in jobs:
            judge_error = None
            try:
                verdicts = _judge_page(page_rel, steps, driven, model)
            except AuthorError as e:
                verdicts = None
                judge_error = str(e)
            for sid, _step in steps:
                d = driven.get(sid) or {"count": -1, "shot": None}
                entry = {"page": page_rel, "count": d["count"]}
                if "drop_count" in d:
                    entry["drop_count"] = d["drop_count"]
                if d["count"] == 0:
                    entry.update(status="missing",
                                 problem="the target matched nothing on the "
                                         "rendered page")
                elif d["count"] < 0:
                    entry.update(status="error",
                                 problem="the page or bridge did not answer "
                                         "the probe")
                elif d.get("drop_count") == 0:
                    # a drag with nowhere to land is as broken as one with
                    # nothing to pick up; the ring on the target proves nothing
                    entry.update(status="missing",
                                 problem="the drop zone (drop_target) matched "
                                         "nothing on the rendered page")
                elif d.get("drop_count", 1) < 0:
                    entry.update(status="error",
                                 problem="the page or bridge did not answer "
                                         "the drop-zone probe")
                elif verdicts is None:
                    entry.update(status="error", problem=judge_error)
                else:
                    v = verdicts.get(sid)
                    if v is None:
                        entry.update(status="error",
                                     problem="the vision verdict did not "
                                             "cover this step")
                    elif v["ok"]:
                        entry.update(status="ok", problem="")
                    else:
                        entry.update(status="flagged", problem=v["problem"])
                entries[sid] = entry
    return entries


def _failures(entries: dict):
    """The step failures worth a repair turn: a target that matched nothing,
    or one the vision judge flagged. Probe/judge errors are states of the
    CHECK — repairing the walkthrough over them would thrash."""
    return {sid: e for sid, e in entries.items()
            if e.get("status") in ("missing", "flagged")}


def _repair_instruction(failures: dict) -> str:
    lines = ["The replay check ran every step in the player and found these "
             "target problems:"]
    for sid, e in sorted(failures.items()):
        lines.append(f"- step {sid!r} (on {e['page']}): {e['problem']}")
    lines.append(
        "Retarget each step above from its page's element inventory — prefer "
        "an element in a part of the page that renders — and adjust that "
        "step's hint if the on-screen location changed. A drag step's "
        "drop_target comes from that page's DROP ZONES list. Change nothing "
        "else.")
    return "\n".join(lines)


def run_verify(walkthrough_id: str, base_url: str, cookie_header=None,
               model=None, repair=True, progress=None) -> dict:
    """Verify every target-bearing step; optionally repair once and re-verify.

    The repair is a single AI edit pass fed the failures as an instruction —
    bounded cost, and its output goes through the same validation as any
    edit. Whatever still fails after that surfaces as flagged entries in the
    `verify_check` column; nothing here ever blocks or rewrites a walkthrough
    directly.

    `progress(phase, message)` names the three stretches an author would
    otherwise wait through blind (first sweep, repair, re-verify) and is the
    cancellation point. A cancel during the repair leaves the first sweep's
    verdicts unwritten, which matches the existing posture that nothing here
    half-applies.
    """
    model = model or author._model_from_env()
    progress = progress or (lambda *_a, **_k: None)
    row = store.get(walkthrough_id)
    if row is None:
        raise VerifyError(f"No walkthrough named {walkthrough_id!r}.")
    # Draft or authored is the `authored_at` column's question now. This used to
    # be a substring scan for "TODO" over the raw walkthrough.json, which cannot
    # tell one of ingest's placeholders from an author legitimately writing the
    # word in learner-facing text — a walkthrough about a ticket queue could not
    # be verified at all. See the note above store.authoring_state.
    if not store.authoring_state(row):
        raise VerifyError("This walkthrough is still a draft — run the "
                          "authoring pass first, then verify the replay.")
    walkthrough = row.get("definition") or {}

    progress("replay", "replaying every step in the player")
    entries = _verify_once(walkthrough_id, walkthrough, base_url,
                           cookie_header, model)
    repaired = False
    repair_summary = None
    repair_error = None
    failures = _failures(entries)
    if failures and repair:
        progress("repair", f"repairing {len(failures)} failing step(s)")
        try:
            edit = author.run_edit(walkthrough_id,
                                   _repair_instruction(failures), model=model,
                                   progress=progress,
                                   replay_ctx=(base_url, cookie_header),
                                   answerable=False)
            repair_summary = edit.get("summary") or ""
            repaired = True
            # Re-read rather than trusting `edit`: run_edit saves through
            # store.save_with_history, and the re-verify below has to run against
            # what was actually committed — not the model's proposal, which the
            # validator may have rejected in part.
            walkthrough = (store.require(walkthrough_id).get("definition") or {})
            progress("replay", "re-checking the repaired steps")
            entries = _verify_once(walkthrough_id, walkthrough, base_url,
                                   cookie_header, model)
        except AuthorError as e:
            # advisory to the end: a failed repair leaves the first sweep's
            # verdicts standing rather than failing the whole check
            repair_error = str(e)

    results = {
        "schema": SCHEMA, "model": model,
        "checked_at": datetime.now(timezone.utc)
                      .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "repaired": repaired,
        "steps": entries,
    }
    if repair_summary:
        results["repair_summary"] = repair_summary
    if repair_error:
        results["repair_error"] = repair_error
    store.save_verify_check(walkthrough_id, results)
    return results
