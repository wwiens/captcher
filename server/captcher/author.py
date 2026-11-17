#!/usr/bin/env python3
"""
author.py — Stage 3 AI authoring pass (AUTHORING.md).

Turns a TODO draft walkthrough (produced by ingest.py) into a publishable walkthrough
by sending the draft, a digest of every captured page snapshot (see digest.py),
and a scenario intent to the Anthropic Messages API, then validating the result
before writing it. The model targets elements by digest ref; resolve_refs()
substitutes the server-verified selectors before validation.

Usage:
    python3 author.py <walkthrough-id> ["optional scenario intent"]
    python3 author.py edit <walkthrough-id> "edit instruction"

Without a scenario intent the model derives one from the captured pages
and recorded actions. `edit` applies a natural-language change to a
published walkthrough, keeping the previous version in `walkthrough_history`.

Requires ANTHROPIC_API_KEY in the environment. Model can be overridden with
CAPTCHER_MODEL (default: claude-sonnet-5-5).

On success:
  - the pre-authoring draft is preserved in the row's `draft` column (write-once,
    so a retry keeps the original ingest draft)
  - the row's `definition` is replaced with the authored walkthrough, through
    store.save_with_history — so the draft is recoverable as a history entry too

WHAT THE STORAGE MOVE CHANGED HERE

This module used to resolve a walkthrough itself: `ROOT / "walkthroughs" / id`
for the definition, then the definition's own `capture_dir` to find the capture
beside it. Both are gone. A walkthrough is a row read with `store.get`, and its
captured pages are objects addressed by a capture-relative path STRING —
"step-000/page.html" — through store.capture_text / capture_bytes /
capture_files. Nothing here composes a capture key; `blobs.key_for` is the only
code that does, which is the point: an independent second resolution is exactly
how a walkthrough and the capture folder it pointed at came to drift apart.

`ROOT` survives for repo-bundled material only (prompts/example-walkthrough.json,
which ships with the code). It must never be used to reach a walkthrough again.

The cost of the move, named honestly: page text arrives over the network now
rather than off a local disk, and both passes are tool loops that re-read pages
on every turn. The reads that repeat are marked where they happen, and the two
that mattered most — validation with no pending capture edits, and the
screenshots that are listed but never inlined — no longer fetch anything.
"""
import base64
import copy
import itertools
import json
import os
import re
import sys
import tempfile
from html import unescape
from pathlib import Path

import anthropic

from . import digest
from . import envfile
from . import langsmith_trace
from . import store
from . import usage as ai_usage  # not `usage`: the call paths use that as a local
from .digest import is_generated_class  # noqa: F401 — moved there; re-exported
                                       # because prune_class_attrs and
                                       # tests/test_author_prune.py use it here

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "claude-sonnet-5-5"

# Models that accept the server-side refusal fallback (`fallbacks`, beta
# server-side-fallback-2026-07-01). Listed explicitly rather than matched by a
# loose prefix: "claude-opus-5" used to cover "claude-opus-5-5" only by
# coincidence of spelling, and a model that does not support the parameter
# answers it with a 400 that fails the whole pass.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5",
                   "claude-fable-5")


def supports_fallbacks(model: str) -> bool:
    """True when `model` is (a snapshot of) a model listed in FALLBACK_MODELS."""
    model = model or ""
    return any(model == m or model.startswith(m + "-") for m in FALLBACK_MODELS)


def _model_from_env():
    """Model override from the environment, else DEFAULT_MODEL. Empty counts
    as unset, so exporting the name blank does not ask for a blank model.
    Shared with brandfetch.py."""
    return (os.environ.get("CAPTCHER_MODEL") or "").strip() or DEFAULT_MODEL


# max_tokens caps THINKING PLUS RESPONSE TEXT, not just the response. The pass
# omitted `thinking` for a long time, which on the current models means adaptive
# thinking runs at the default "high" effort — so a large share of the budget
# was spent before the first character of JSON. At 16000 that truncated a
# 16-step QuickBooks walkthrough mid-emission.
#
# For scale: real authored output runs ~950 chars/step (approve-goodwill 6 steps
# → 5,882 chars; test-after-phase-8 8 steps → 7,437), so even a 20-step
# walkthrough is only ~7K tokens of JSON. Everything above that is thinking.
MAX_TOKENS = 32000
# Thinking depth. Explicit because the API default is "high" and omitting the
# field does not mean "off" — it means the default applies invisibly. This is
# the module default (the AI edit pass uses it: a bounded transformation, not
# open-ended reasoning). Call sites with different shapes override per call:
# the authoring pass runs at AUTHOR_EFFORT (below), the render-check verdict
# and brand inference at "low" with small max_tokens of their own.
EFFORT = "medium"
# The tool loop's per-turn budget, separate from MAX_TOKENS. A truncated turn in
# the loop cannot be resumed (a half-written tool call is not a tool call), so
# it throws away every earlier turn; the cap only costs what is actually used,
# and Sonnet 5.5 / Opus 5.5 allow up to 128K. This is headroom against the
# rare long turn, NOT the fix for routine truncation — if that ever becomes
# common, lower the effort first (see the truncation error text and its test).
AGENTIC_MAX_TOKENS = 64000
# Input budget for one pass, checked by preflight_prompt_size() before the
# request goes out. Held below the model's real 1M context so the reply and a
# pessimistic estimate both have room.
INPUT_TOKEN_BUDGET = 900_000
# Per-request deadline for thinking plus generation on a prompt that can
# approach the input budget. The request itself streams (so the connection
# never sits idle), but the caller still blocks until the final message.
TIMEOUT = 600
RETRIES = 3            # SDK-managed: connection errors, 408/409/429/5xx
# One-shot quality reference for the authoring prompt. This is repo-bundled
# first-party prompt material, not a walkthrough: it deliberately does NOT live
# under walkthroughs/, which is git-ignored (so it would vanish on a fresh
# clone) and is globbed into the catalog (so it would show up as a broken card).
EXAMPLE_WALKTHROUGH_FILE = "prompts/example-walkthrough.json"


class AuthorError(Exception):
    """Anything that should surface to the user as a clean message."""


# Re-exported so the many call sites that say `author.load_env()` keep working.
# The implementation moved to envfile.py so that db.py and blobs.py can read
# configuration without importing the Anthropic SDK behind it.
load_env = envfile.load_env

load_env()  # server.py imports this module, so the server picks up .env too

# Thinking depth for the Stage 3 authoring pass specifically — the quality-
# critical call in the product. "high" is a deliberate cost increase over the
# module default; the env knob follows the _model_from_env pattern so it can
# be dialled back without a code change. (Defined after load_env() so a .env
# value is honoured.)
AUTHOR_EFFORT = (os.environ.get("CAPTCHER_AUTHOR_EFFORT") or "").strip() or "high"
# Thinking depth for the AI edit pass (and so for verify_pass's repair turn,
# which is an edit). Same env-knob pattern as AUTHOR_EFFORT; defaults to the
# module EFFORT. An author waits on this call interactively, so it trades a
# little depth for latency — raise it if edits start missing multi-part asks.
EDIT_EFFORT = (os.environ.get("CAPTCHER_EDIT_EFFORT") or "").strip() or EFFORT


# ---------------------------------------------------------------- HTML prep

# Inline data: URI in an attribute value. SingleFile leaves attribute values
# unquoted whenever they contain no spaces, so a bare `src=data:image/png;base64,…`
# is the common case in real captures — matching only quoted values lets whole
# base64 images through. srcset (comma-separated, spaced) is covered by the
# quoted branches; an unquoted value cannot contain a space by definition.
_DATA_URI_ATTR_RE = re.compile(
    r"""(\w[-\w:]*)\s*=\s*(?:"data:[^"]{40,}"|'data:[^']{40,}'|data:[^\s>]{40,})""",
    re.I,
)
# Inline data: URI in a CSS url(). The bare branch excludes quotes and parens,
# so a raw unencoded SVG data URI simply fails to match rather than being cut
# mid-payload — CSS forbids unescaped parens in an unquoted url() anyway.
_DATA_URI_CSS_RE = re.compile(
    r"""url\(\s*(?:"data:[^"]{40,}"|'data:[^']{40,}'|data:[^)"']{40,})\s*\)""",
    re.I,
)


def strip_data_uris(html: str) -> str:
    """Replace inline data: URI payloads with a placeholder, keeping the
    attribute or declaration that carried them. The model needs to know an
    image is there and what it is — alt/title/aria-label and the surrounding
    markup survive — never the megabytes of base64."""
    html = _DATA_URI_ATTR_RE.sub(r'\1="data:…"', html)
    return _DATA_URI_CSS_RE.sub("url(data:…)", html)


SCRIPT_BLOCK_CHARS = 4_000      # per <script> body kept by keep_scripts


def trim_html(html: str, keep_scripts: bool = False) -> str:
    """Shrink a captured page for the prompt: drop scripts/styles/comments
    and inline data URIs, collapse whitespace. Structure, attributes and
    visible text — everything selector hardening needs — survive.

    keep_scripts keeps <script> blocks (each body capped at
    SCRIPT_BLOCK_CHARS with a visible truncation marker) instead of dropping
    them. Used by the read_page_html tool: a page's behaviour often lives in a
    script tag, and the tool used to strip them silently — the model could
    read the whole page and still have no way to learn it was reactive.
    Inlined prompt pages (digest fallback) keep the drop: a SingleFile bundle
    is megabytes of JS the prompt budget cannot carry."""
    if keep_scripts:
        def clip_script(m):
            body = m.group(2)
            if len(body) > SCRIPT_BLOCK_CHARS:
                body = (body[:SCRIPT_BLOCK_CHARS]
                        + "\n/* …script truncated by read_page_html — "
                          "the file has the rest… */\n")
            return m.group(1) + body + m.group(3)
        html = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", clip_script,
                      html, flags=re.S | re.I)
    else:
        html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = strip_data_uris(html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html.strip()


# ------------------------------------------------- heavy-page prompt pruning
#
# trim_html is enough for an ordinary product page. Some apps are not ordinary:
# a QuickBooks Online snapshot trims to ~200K chars, and dense markup tokenises
# at roughly TWO chars per token (not the ~4 you would assume for prose), so a
# 13-page capture reached 1.3M tokens and the request was rejected outright.
#
# The extra pruning below only runs on pages that actually blow past
# PRUNE_ABOVE_CHARS. Every existing capture in this project trims to under 32K
# per page, so none of them see any of it and their authoring passes are
# unchanged. Only genuinely heavy pages degrade — and they degrade toward
# *better* targeting handles, not worse ones.
#
# What this does NOT buy you: there is no safety net underneath it. validate()
# has no HTML parser and no CSS engine — it checks that a selector string is
# non-empty, and warns if the target's text is missing from the page's flattened
# text. Nothing resolves the selector, so nothing can tell you a pruned handle
# left the model with an ambiguous one. At playback, bridge.js's findTarget
# silently takes the first of N matches.
#
# So the guard here is that the pruned tokens are build-generated hashes the
# model must not target anyway (see is_generated_class), not that a mistake
# would be caught downstream. It would not be.

PRUNE_ABOVE_CHARS = 60_000

# Build-generated class tokens are recognised by digest.is_generated_class
# (moved there with its regexes — the digest's selector ladder needs the same
# judgement). It is re-exported above because prune_class_attrs below is what
# it originally guarded, and tests/test_author_prune.py pins both directions
# through this module.

_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)
_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.S | re.I)
_SVG_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)
_CLASS_ATTR_RE = re.compile(r"""(\sclass=)("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
# Framework plumbing with no authoring value. data-testid / data-id are the
# opposite — they are the most stable handles a captured page offers — so they
# are deliberately absent from this list.
_NOISE_ATTR_RE = re.compile(
    r"""\s(?:data-morpheus-[\w-]+|data-reactid|data-uiobject|data-cy)"""
    r"""=(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)


def strip_svg_geometry(html: str) -> str:
    """Collapse inline <svg> bodies to the opening tag plus any <title>.

    Path/g/circle geometry is a third of a heavy page and means nothing to the
    authoring pass. The <svg> tag itself survives with its attributes, so
    aria-label and role — the parts an icon button is actually targeted by —
    are kept.
    """
    def repl(m):
        chunk = m.group(0)
        open_tag = _SVG_OPEN_RE.match(chunk)
        title = "".join(_SVG_TITLE_RE.findall(chunk))
        return ((open_tag.group(0) if open_tag else "<svg>")
                + (f"<title>{title}</title>" if title.strip() else "")
                + "</svg>")
    return _SVG_RE.sub(repl, html)


def prune_class_attrs(html: str) -> str:
    """Drop build-generated tokens from every class attribute, and drop the
    attribute entirely when nothing authored is left."""
    def repl(m):
        raw = m.group(2)
        quote = raw[0] if raw[:1] in ("\"", "'") else ""
        kept = [t for t in raw.strip("\"'").split() if t and not is_generated_class(t)]
        if not kept:
            return ""
        joined = " ".join(kept)
        return f'{m.group(1)}{quote}{joined}{quote}' if quote else f'{m.group(1)}{joined}'
    return _CLASS_ATTR_RE.sub(repl, html)


def prepare_page(html: str, keep_scripts: bool = False) -> str:
    """Trim a captured page for the prompt, pruning harder if it is heavy.

    The single entry point for both the authoring and the edit pass — they must
    show the model the same view of a page or an edit will target something
    authoring never saw. keep_scripts is the read_page_html tool's variant:
    script bodies stay (capped), so page behaviour is inspectable on demand.
    """
    trimmed = trim_html(html, keep_scripts=keep_scripts)
    if len(trimmed) <= PRUNE_ABOVE_CHARS:
        return trimmed
    pruned = strip_svg_geometry(trimmed)
    pruned = _NOISE_ATTR_RE.sub("", pruned)
    return prune_class_attrs(pruned)


def visible_text(html: str) -> str:
    """Page text the way the player's bridge sees it: tags contribute
    NOTHING (element.textContent concatenates adjacent cells with no
    separator), whitespace collapses. Must stay faithful to bridge.js's
    norm(textContent) or validation warnings drift from player reality."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", unescape(text))


def snapshot_pages(walkthrough_id: str):
    """All top-level snapshot pages in the capture, in recorded order.
    Returns [(capture-relative path, html text)]. Excludes auto/ snapshots.

    Takes the walkthrough's id, where it took a capture root: there is no root
    to hand it any more. `store.capture_files` returns sorted paths and the
    folders are zero-padded (step-000, step-001 …), so recorded order comes out
    of the sort exactly as it did out of `sorted(iterdir())`.

    Reading every page whole is what `unique_pages` needs to collapse the
    extension's duplicate captures, and it is now the expensive part of both
    passes, since the bytes come over the network. The cheap version — comparing
    the S3 ETag, which is the object's MD5 — needs a listing that carries object
    metadata, which `blobs` does not yet expose.
    """
    pages = []
    for rel in store.capture_files(walkthrough_id, ".html"):
        parts = rel.split("/")
        if len(parts) != 2 or parts[0] == "auto" or parts[1] != "page.html":
            continue
        html = store.capture_text(walkthrough_id, rel)
        if html is not None:
            pages.append((rel, html))
    return pages


def unique_pages(pages):
    """Collapse byte-identical snapshots: the extension's change detector
    re-captures on large DOM mutations, so a real flow ships duplicates (the
    Salesforce capture: 13 files, 8 distinct). Returns (uniques, alias_map)
    where uniques keeps each first occurrence and alias_map maps every
    duplicate rel path to its canonical one. Every original path stays a valid
    `page` value — the digest lists aliases so the model can still assign the
    path a step was recorded against."""
    uniques, alias_map, first_by_hash = [], {}, {}
    for rel, html in pages:
        h = hash(html)  # within one capture, collision-checked by equality below
        canon = first_by_hash.get(h)
        if canon is not None and canon[1] == html:
            alias_map[rel] = canon[0]
        else:
            first_by_hash[h] = (rel, html)
            uniques.append((rel, html))
    return uniques, alias_map


# A page the digest builder cannot handle falls back to the old prepared-HTML
# representation, hard-capped: one pathological page must not reintroduce the
# unbounded prompt this module exists to end.
FALLBACK_PAGE_CHARS = 150_000


def build_page_digests(pages):
    """Digest every unique page for the prompt.

    Returns (blocks, ref_table, alias_map, trees):
      blocks     — one prompt text block per unique page (digest, or capped
                   prepared HTML under a "raw HTML fallback" header)
      ref_table  — ref -> {"page": canonical rel, "target": verified target}
      alias_map  — duplicate rel path -> canonical rel path
      trees      — canonical rel -> digest.Tree (only pages that parsed)
    """
    uniques, alias_map = unique_pages(pages)
    aliases_of = {}
    for dup, canon in alias_map.items():
        aliases_of.setdefault(canon, []).append(dup)

    blocks, ref_table, trees = [], {}, {}
    counter = itertools.count(1)
    for rel, html in uniques:
        try:
            tree = digest.parse_page(html)
            block, entries = digest.page_digest(rel, tree,
                                                aliases=aliases_of.get(rel, ()),
                                                ref_counter=counter)
            # display-only entries don't make a page digest-viable: steps need
            # interactive targets, so such a page still falls back to raw HTML
            if not any(not e.get("display") for e in entries):
                raise digest.DigestError("no interactive elements found")
        except digest.DigestError as e:
            print(f"[author] digest fallback for {rel}: {e}")
            body = prepare_page(html)
            if len(body) > FALLBACK_PAGE_CHARS:
                body = body[:FALLBACK_PAGE_CHARS] + "\n…(truncated)"
            alias_note = ("\nsame page as (interchangeable `page` values): "
                          + ", ".join(aliases_of[rel])) if rel in aliases_of else ""
            blocks.append(f"=== PAGE {rel} (raw HTML fallback — write selectors "
                          f"directly for targets on this page) ==={alias_note}\n{body}")
            continue
        trees[rel] = tree
        blocks.append(block)
        for e in entries:
            ref_table[e["ref"]] = {"page": e["page"], "target": e["target"],
                                   "node": e["node"],
                                   "display": bool(e.get("display")),
                                   "dropzone": bool(e.get("dropzone"))}
    return blocks, ref_table, alias_map, trees


EFFECTS_BLOCK_MAX_LINES = 40


def observed_effects_block(walkthrough_id: str, ref_table: dict,
                           alias_map: dict, trees: dict) -> str:
    """What the LIVE page did when the author acted, as the recorder saw it.

    The extension's recorder watches a short MutationObserver window around
    every recorded action and reports which named elements' text changed
    (`effects` on the action, additive on capture schema @1). This is the one
    signal that survives serialization loss: a framework page whose handlers
    are attached with addEventListener captures as inert markup, and no
    amount of digest inspection can recover that typing an amount recomputes
    a total. Here those observations are resolved to digest refs and put in
    front of the model.

    Placement note: an action recorded on capture-step N happened on the
    PREVIOUS page (its pre-action state) — the same walk ingest.convert()
    does when it drafts steps."""
    # The manifest is an object in the capture prefix like any other page, and
    # `store.capture_manifest` owns both its name and its parse. A capture
    # without one (or with an unparseable one) simply has no observations to
    # report, which is the same silence the old OSError/JSONDecodeError branch
    # produced — the difference is that store returns {} instead of raising, so
    # there is nothing to catch here.
    src = store.capture_manifest(walkthrough_id)
    if not src:
        return ""
    node_ref = {id(e["node"]): r for r, e in ref_table.items()
                if e.get("node") is not None}

    def name_of(page_rel, selector):
        """A digest ref for the selector when it resolves, else the selector."""
        canon = (alias_map or {}).get(page_rel, page_rel)
        tree = trees.get(canon)
        if tree is not None and selector:
            hits = digest.match(tree, {"selector": selector})
            if hits is not None and len(hits) == 1:
                ref = node_ref.get(id(hits[0]))
                if ref:
                    return ref
        return selector or "(unnamed)"

    def _c(s):
        s = str(s if s is not None else "")
        return s if len(s) <= 60 else s[:59] + "…"

    initial = (src.get("initialCapture") or {}).get("folder")
    prev = f"{initial}/page.html" if initial else None
    per_page = {}
    for cap in src.get("steps") or []:
        for action in cap.get("actions") or []:
            if prev and action.get("effects") and action.get("selector"):
                per_page.setdefault(prev, []).append(action)
        folder, html = cap.get("folder"), (cap.get("files") or {}).get("html")
        if folder and html:
            prev = f"{folder}/{html}"
    if not per_page:
        return ""

    lines = ["--- OBSERVED BEHAVIOUR (recorded from the live product as the "
             "author acted). The frozen snapshot replays an effect only where "
             "the wiring survives in the markup (a `reactive:` element); an "
             "effect listed here with NO reactive source element means the "
             "live app did it in script the capture lost — playback will NOT "
             "reproduce it, so when a step depends on the after-state, bake "
             "it into the page with capture_edits. ---"]
    for page_rel, actions in per_page.items():
        lines.append(f"{page_rel}:")
        for action in actions:
            cause = name_of(page_rel, action.get("selector"))
            for e in action.get("effects") or []:
                if len(lines) >= EFFECTS_BLOCK_MAX_LINES:
                    lines.append("  … (more observations elided)")
                    return "\n".join(lines)
                lines.append(
                    f'  {action.get("type")} on {cause} → '
                    f'{name_of(page_rel, e.get("selector"))} changed '
                    f'"{_c(e.get("before"))}" → "{_c(e.get("after"))}"')
    return "\n".join(lines)


def resolve_refs(walkthrough: dict, ref_table: dict, alias_map: dict):
    """Substitute model-picked element refs with their verified targets.

    Mutates the walkthrough in place; returns a list of errors. A target
    {"ref": "e17"} becomes the server-generated target that ref stands for —
    any selector/text the model wrote alongside it is discarded, because the
    stored target is already verified unique. A ref that lives on a different
    page than the step plays on is an error: the digests are per-page, so a
    cross-page ref means the model mis-assigned `page` (the check the old
    "search the HTML" instruction could never enforce)."""
    errors = []
    current_page = None
    for i, step in enumerate(walkthrough.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        page = step.get("page")
        if page is not None:
            current_page = page
        action = step.get("action") or {}
        label = f"step {i} ({step.get('id', '?')})"
        for key in ("target", "drop_target"):
            target = action.get(key)
            if not isinstance(target, dict) or "ref" not in target:
                continue
            ref = target["ref"]
            entry = ref_table.get(ref)
            if entry is None:
                errors.append(f"{label}: {key} ref {ref!r} is not in the page digests")
                continue
            if entry.get("display"):
                errors.append(
                    f"{label}: {ref} is a DISPLAY element — it shows a value and "
                    f"cannot be clicked or typed into. Target an element from the "
                    f"INTERACTIVE list; use display refs only as capture_edits "
                    f"anchors")
                continue
            if entry.get("dropzone") and key == "target":
                errors.append(
                    f"{label}: {ref} is a DROP ZONE — it is where a drag ends, not "
                    f"something to click, type into or pick up. Use it as a drag "
                    f"step's drop_target, and pick the element from the "
                    f"INTERACTIVE list as target")
                continue
            effective = alias_map.get(current_page, current_page)
            if effective != entry["page"]:
                errors.append(
                    f"{label}: {key} is {ref}, which exists on {entry['page']}, but "
                    f"the step plays on {current_page or '(no page set)'} — fix the "
                    f"step's `page` or pick a ref from that page's inventory")
                continue
            action[key] = dict(entry["target"])
    return errors


# --------------------------------------------------------- render accuracy
# What render_check.py knows and the digests cannot: whether a page still LOOKS
# right once the player renders the snapshot. The digests are parsed from HTML,
# so an element can be perfectly present in the tree and render as white space
# because a stylesheet or a background image did not survive the capture.
#
# Everything below is advisory, and deliberately so: a verdict is per-PAGE
# prose from a vision comparison with no element-level localisation, so nothing
# can enforce it the way resolve_refs enforces a ref. It steers wording and
# earns a warning; it never blocks a write.

# Severities worth telling the model about. "ok" is silence; "no-baseline",
# "pending" and "error" describe the state of the CHECK, not of the page, and
# a note about the checker helps nobody write a step.
RENDER_NOTE_SEVERITIES = ("minor", "broken")
# Severities worth a warning on the author's review list — only a page a
# learner could not use. "minor" is by definition still usable for training,
# and one line per imperfect page would bury the target and selector warnings
# it sits next to.
RENDER_WARN_SEVERITIES = ("broken",)


def render_notes(results, severities=RENDER_NOTE_SEVERITIES):
    """{page rel -> {"severity", "differences"}} from a render_check sidecar.

    `results` is store.load_render_check()'s value, or None when no check has
    been run — which is the common case, since the check is on demand and
    costs a vision call per unique page. Pages judged faithful, and pages the
    check could not judge at all, are left out.

    Byte-identical duplicate snapshots appear under every one of their paths:
    run_check copies a canonical page's verdict onto its aliases, and a step's
    `page` may legitimately be any of them.
    """
    notes = {}
    for rel, entry in ((results or {}).get("pages") or {}).items():
        if not isinstance(entry, dict) or entry.get("severity") not in severities:
            continue
        notes[rel] = {
            "severity": entry["severity"],
            "differences": [str(d) for d in entry.get("differences") or [] if str(d).strip()],
        }
    return notes


def render_notes_block(notes, checked_at=None):
    """Prompt text for `notes`, or None when there is nothing to say."""
    if not notes:
        return None
    when = f", checked {checked_at}" if checked_at else ""
    lines = [
        f"--- RENDER ACCURACY (how the player actually renders these snapshots, "
        f"compared against screenshots taken during capture{when}) ---",
        "The pages below did not render faithfully. The digests are parsed from page "
        "HTML, so an element listed in one can still be invisible, unstyled or "
        "misplaced on screen. Do NOT write instructions, captions or hints that lean "
        "on anything reported below — a colour, an image, a chart, a logo, or a "
        "position relative to one — and where the instruction leaves you a choice of "
        "target, prefer an element in a part of the page that renders. This describes "
        "the rendering, not the task: never add, drop or reorder steps because of it.",
    ]
    for rel in sorted(notes):
        note = notes[rel]
        detail = "; ".join(note["differences"]) or "differs from the capture screenshot"
        lines.append(f"{rel} — {note['severity']}: {detail}")
    return "\n".join(lines)


# ------------------------------------------------------------------ prompt

# Both passes are tool loops. There used to be a second, single-shot shape
# behind an AGENTIC env knob — the model returned bare JSON and any validation
# failure aborted the run, so the author re-paid for a re-run the model never
# learned from. It was the rollback while the loop bedded in; the loop is the
# only path now and git is the rollback. What that removes, beyond the branch
# itself: two closers per prompt, two `_check` call sites judged by the same
# rules "either way", and a config flag whose unset state silently bought the
# worse pass.
PASS_CLOSER = """Work with your tools, then submit:
- `submit_walkthrough` runs the full validation itself, so when you believe the \
walkthrough is finished, submit it directly. `validate_walkthrough` runs the same checks \
without submitting — use it only for a draft you are unsure of, not as a routine step \
before every submit.
- `read_page_html` returns raw snapshot HTML around a search string, for the rare case \
where a digest excerpt is not enough. The digests are usually sufficient — reach for \
this only when they are not.
- When the walkthrough is finished, call `submit_walkthrough` with the complete \
walkthrough.json. If the result reports errors, fix exactly those errors and resubmit — \
do not start over and do not return the walkthrough as plain text. If it reports only \
warnings, fix the ones that point at a real problem (an empty field, a target text that \
is not on the page, a target that matches several elements) and resubmit; a warning you \
judge harmless can stay."""

EDIT_CLOSER = """Work with your tools, then submit:
- `submit_edit` runs the full validation itself, so when you believe the edit is \
finished, submit it directly. `validate_walkthrough` runs the same walkthrough checks \
without submitting (and without your capture_edits) — use it only for a draft you are \
unsure of.
- `read_page_html` returns raw snapshot HTML around a search string when a digest \
excerpt is not enough.
- `verify_capture_edit` reports exactly how many times a proposed `find` string occurs \
in the captured HTML files, with surrounding context — check any capture edit you are \
not certain of before submitting, instead of guessing at markup you cannot see.
- `probe_target` (when offered) renders a page in the real player and returns a \
target's live match count plus a screenshot with the match ringed — the ground truth \
for whether a retargeted or new step will land. Use it before submitting any target \
you are not sure of; it sees the pages as saved, without your pending capture_edits.
- When done, call `submit_edit` with {"walkthrough": <the complete updated \
walkthrough.json>, "capture_edits": [{"find": "<exact string>", "replace": \
"<replacement>", "page": "<optional: one snapshot path — omit to apply everywhere>"}, \
...], "summary": "<one or two sentences, past tense, telling the author exactly what \
changed>"}. If the result reports errors, fix exactly those errors \
and resubmit — do not start over. If it reports only warnings, fix the ones that point \
at a real problem and resubmit; a warning you judge harmless can stay.
- When the author asked a question instead, call `answer_author` with {"answer": \
"<plain prose, direct answer first>", "suggested_change": "<optional: one imperative \
edit instruction>"}. It saves nothing."""

# ---- shared prompt blocks -------------------------------------------------
#
# Both system prompts end with a schema reference, and the step/action half of
# it was duplicated verbatim. That duplication has one specific failure mode:
# a schema change (a new step field, a new action) lands in `validate()` and in
# whichever prompt the author happened to be editing, and the other pass keeps
# describing the old shape — with nothing failing, because the prompts are
# prose and nothing parses them.
#
# Only the genuinely identical text is shared. The *prose rules* — frozen
# pages, target picking, the three voices, merged runs — are deliberately NOT
# merged even though they cover the same ground in both prompts: one is
# telling the model how to build a walkthrough and the other how to preserve
# one while changing a part of it, and the wordings differ in ways that read
# as intentional. Collapsing them would mean rewriting a live prompt to suit a
# refactor, which is the wrong way round. `test_prompt_shared_blocks.py` pins
# what is shared so it cannot drift back apart.

STEP_SCHEMA = """each step: `id`, `title`, `page` (snapshot path or null), \
`instruction`, `showme_caption`, `hint` (optional on observe), `action`, and optionally \
`continues` (true = plays straight on from the previous step with no pause; never on the \
first step, never together with a `page`). Actions: \
`click` {target}; `select` {target, value, accept_any?, wrong_value_feedback?}; `input` \
{target, min_length, demo_value}; `drag` {target, drop_target}; `observe` {}."""

SELECT_SEMANTICS = """A select's \
`value` is what Show-me demonstrates; `accept_any: true` makes You-try accept any choice \
(then `wrong_value_feedback` never shows)."""

DRAG_SEMANTICS = """A drag's `target` is the element the learner picks up (a `draggable` \
inventory line) and `drop_target` is where it is released (a DROP ZONES line on the same \
page). Show-me carries the element there and the player moves it into the zone in the \
frozen page, so a drag needs no button or menu to exist for the move; in You-try the \
learner drags it for real, and releasing it anywhere else is a wrong answer."""


def schema_reference(target_rule, top_level=""):
    """The closing schema block. `target_rule` is the one part that genuinely
    differs between the passes: the authoring pass writes every target from
    scratch, the edit pass leaves untouched ones alone.

    The step schema is stored lowercase because the edit prompt runs it
    straight on from the em dash; after a top-level sentence it starts one, so
    it is capitalised here rather than kept as two near-identical constants.
    """
    steps = STEP_SCHEMA
    head = "Schema reference (walkthrough-def@1) — "
    if top_level:
        head += top_level
        steps = steps[0].upper() + steps[1:]
    return f"{head}{steps} {target_rule} {SELECT_SEMANTICS} {DRAG_SEMANTICS}"


TOP_LEVEL_SCHEMA = """top level: `schema`, `id`, `title`, `level`, \
`track`, `estimated_minutes`, `intro` {scenario, objectives: [3-5 \
strings]}, `steps`, `completion` {summary, success_criteria: [strings]}, and optionally \
`cursor`. """

SYSTEM_PROMPT = """You are the Stage 3 authoring pass of the Captcher walkthrough pipeline. \
You convert a DRAFT interactive walkthrough (walkthrough-def@1 schema, generated mechanically from a \
screen capture, every human-facing field stamped TODO) into a publishable walkthrough. You are \
given the draft walkthrough.json, a DIGEST of every captured page snapshot, and optionally a \
scenario intent (who the learner is and why they perform this task). Each page digest carries \
the page's visible text (what the learner sees) and an inventory of its interactive elements. \
Every inventory line is one element: a ref id (e.g. e17), its kind, its visible label, and a \
`sel:` selector the server has already VERIFIED to match exactly that element on that page — \
you pick elements, the server supplies the selectors. A digest headed "same page as" lists \
snapshot paths that are byte-identical duplicates; they share one inventory and any of those \
paths is a valid `page` value for steps on that page. A digest may be followed by that \
page's capture-time screenshot — what the learner actually saw on screen; trust it for \
anything visual (position, colour, grouping, what is above the fold). If no scenario intent \
is provided, derive one yourself: read the page digests and recorded actions, use the fictional \
people and data visible on the pages (names, statuses, reason codes, notes), and invent a \
realistic work situation that explains who the learner is and why they do this task.

Apply these transformations, in order:

1. CONSOLIDATE STEPS — the action log over-records:
   - Merge a `click` on a <select> followed by a `select` action into a single select step.
   - Merge successive `input` actions on the same field (partial typing like "kow" then \
"kowalski") into ONE input step carrying the final, cleaned-up value.
   - Delete stray/incidental actions: clicks on non-interactive containers (e.g. the dialog \
itself), duplicate clicks, focus clicks before typing.
   - Consolidation removes NOISE (the items above), never the lesson. An action that visibly \
changed the page — it has a snapshot of its own after it — is something the author chose \
to show: keep it as a step even when a later step reverses it (a filter applied then \
cleared, a view switched and switched back). Do not drop deliberate actions to shorten \
the walkthrough; a recording of 8-12 noisy raw actions often becomes 5-7 teaching steps, \
but a longer, deliberate recording stays longer.
   - KEEP DEMONSTRATED REFUSALS — this overrides the merging, the duplicate-click rule and \
the step-count guide above. When the author submits (a save/create/continue/confirm/send \
click) and the snapshot that follows shows the product REFUSING it — an inline error, an \
error toast, the dialog still open with a message — and the author then corrects the input \
and submits again, they showed the rule on purpose. Teach it: keep the rejected value as \
its own input step (its `demo_value` is exactly the rejected value), the refused submit as \
its own step, then the corrected value and the successful submit. The refused submit's \
instruction and caption say what the product enforces and quote the message it shows; the \
step after it plays on the snapshot showing that message. When what was refused is an \
EMPTY field there is no value to type: skip the input step and say in the refused \
submit's instruction that the field is left (or cleared) empty. Merge two edits of one \
field only when no submit happened between them.
   - RECOVER DRAGS the recorder could not see. It logs clicks, typing and selects only, \
so a drag leaves no action — just its result. When a `draggable` element sits in one DROP \
ZONE on a step's page and in a different zone on the next snapshot, and no recorded \
action explains the move, the author DRAGGED it: write a `drag` step (target: the \
draggable, drop_target: the zone it ends up in). Do not substitute a click on a button \
that happens to move it too — teach the gesture the author actually made.
   - MERGE RUNS — set `"continues": true` on a step that should play straight on from the \
one before it, with no pause between them. Use it for a stretch of fields filled in one \
sitting (a name/email/phone form, an address block): filling a form is ONE thing the \
learner is doing, and a pause plus a Continue button between every field breaks it into \
busywork. Each field stays its own step with its own `hint`, `demo_value` and `min_length` \
— only the pacing merges. Rules: never on the first step of the walkthrough, and never on \
a step that sets `page` (a run cannot span two snapshots, so the whole run is one \
snapshot's worth of work). Do NOT merge across a meaningful beat change — a form's fields \
merge, but the button that submits it starts a new step, because deciding to submit is a \
separate thing to teach. TITLES MUST WORK IN BOTH PLAYERS: the classic player heads the \
whole run with the FIRST step's `title` and `instruction` while the captions advance \
field by field; the popup player shows each step's own title next to the field being \
worked on. So title the first step for the run ("Enter the contact details", not "Full \
name") and give its `instruction` the whole run's job — and still give every later step \
in the run a real per-field title of its own ("Email address"); their instructions and \
captions stay per-field.

2. FIX PAGES — each step's `page` must show the state the learner acts ON (the pre-action \
state). The ingest heuristic is sometimes off by one; the first step often belongs on the \
FIRST snapshot in the provided list. Only the provided snapshot paths (including the \
"same page as" duplicates) are valid `page` values — copy them exactly as given, never \
invent one and never reuse a path from an example.
   CRITICAL: the snapshots are FROZEN pages — clicking never opens a dialog, switches a \
tab, or navigates. (Their own scripts still run — a wired input may recompute a label \
as the player types — but nothing new ever appears.) Every step's target element must ALREADY EXIST on the page that is \
loaded for that step. So the first step that acts inside a dialog (or on a newly revealed \
tab/panel) must load the snapshot where that dialog is already open — never `page: null` \
on the previous page. Use `page: null` only for second and later steps on the SAME \
already-loaded snapshot. Verify by finding the target in that page's element inventory: \
if it is not there, pick the page whose inventory has it.

3. PICK TARGETS — for every click/select/input step, choose the element from the \
inventory of that step's effective page and write its ref as the target: \
`"target": {"ref": "e17"}`. The server substitutes the verified selector for the ref.
   - Never invent a selector and never copy a `sel:` string by hand — refs only.
   - A `drag` step names two refs on the SAME page: `target` from the INTERACTIVE list \
(a `draggable` line) and `drop_target` from that page's DROP ZONES list. A drop-zone ref \
is never a `target` — nothing clicks, types into or picks up a column.
   - A ref is only valid on its own page (or that page's listed duplicates): picking a \
ref from a different page than the step plays on is an ERROR, so getting step 2 (pages) \
right matters here.
   - Only on a page marked "raw HTML fallback" (no inventory) do you write a CSS \
selector yourself, as {"selector": css, "text": substring} or {"selector": css, \
"text_exact": exact} — scope it to a container, prefer meaningful classes, and verify it \
matches exactly once in that page's HTML.

4. WRITE THE TEACHING CONTENT — replace every TODO using three distinct voices:
   - `instruction` (You-try): second person, the task plus why it matters in the scenario. \
When the learner has to compose text (a title, a note, a reply), say what it must cover, \
not the words to type: You-try is practice, and dictating the answer turns it into \
copying. `demo_value` carries the model answer and Show-me types it. Naming something \
to find (a ticket key, a member) is fine.
   - `showme_caption` (Show-me): narrator "we", explains what is happening and the reasoning.
   - `hint`: concrete on-screen location of the control — when the page's screenshot is \
provided, describe the location from what is actually visible in it (position, grouping, \
colour), never from DOM order.
   Also:
   - Rename step ids and titles semantically (step-3 → choose-reason).
   - Write intro.scenario as a realistic work situation using the named fictional people \
from the scenario intent, and 3-5 objectives.
   - Add `wrong_value_feedback` to select steps — explain why a wrong choice does not fit \
the scenario, never just "wrong".
   - `demo_value` on input steps: when the recorded value is a real, complete entry, KEEP \
IT AS TYPED — the author typed the answer they want taught; fix only obvious typos and \
the partial-typing merges from step 1. Replace a value only when it is a throwaway or \
placeholder ("test", "asdf", a lone letter) or clearly incomplete, and then model good \
practice (e.g. a case note that references the flag and the requester) using ONLY facts \
visible on the captured pages — never invent a date, amount, commitment, name or detail \
the pages do not show. Set a sensible `min_length`.
   - Make the final observe step an explicit verification checklist of what the last page \
shows.
   - Write completion.summary and success_criteria as observable end states.
   - Fix metadata: proper title casing, a sensible `track` slug, realistic \
`estimated_minutes` (about 30 seconds per step, minimum 2), counting a merged run as one step.
   - Remove the `_source` block. Keep `schema`, `id`, and `level` unchanged.

""" + schema_reference(
    'A target is {"ref": "eNN"} naming an '
    "inventory element — the server replaces the ref with its verified selector, which is why "
    'a SAVED walkthrough (like the example below) shows {"selector": css, "text"/"text_exact": '
    "…} instead; write that saved form yourself only for a raw-HTML-fallback page.",
    top_level=TOP_LEVEL_SCHEMA) + """

""" + PASS_CLOSER

EDIT_SYSTEM_PROMPT = """You are the walkthrough editor of the Captcher walkthrough pipeline. You \
receive, in this order: a DIGEST of every captured page snapshot, then the current \
published walkthrough (walkthrough-def@1 schema), and the author's edit instruction last. \
Each page digest carries the page's visible text and an inventory of its interactive \
elements — one line per element: a ref id (e.g. e17), its kind, its visible label, a \
`sel:` selector the server has VERIFIED to match exactly that element, and a `src:` \
excerpt from the snapshot file. In a `src:` excerpt, class/style attributes and long \
inline-handler bodies are elided, each elision marked with `…` — elided content is not \
shown but IS in the file. An element carrying inline event handlers also lists them as \
`reactive:` (e.g. `reactive: oninput,onchange`): the page recomputes something when \
that element is used. A digest may also carry a DISPLAY ELEMENTS section — named \
read-only elements (totals, statuses, messages) with `d`-prefixed refs. They are what \
instructions about "the value shown" usually mean: use their `src:` excerpts as \
capture_edits anchors. A step can never target a display ref. A digest headed "same page as" lists \
byte-identical duplicate snapshots sharing that inventory. You may also receive a RENDER \
ACCURACY block, listing pages the player does not render faithfully — a digest describes \
the markup, that block describes what a learner actually sees. And you may receive an \
OBSERVED BEHAVIOUR block: cause→effect pairs the recorder watched on the LIVE product \
as the author acted (typing here changed that total). Treat it as ground truth about \
the real app — and read it against the digests: an effect whose cause is a `reactive:` \
element replays in the frozen snapshot; one with no reactive source was script the \
capture lost, so playback will NOT reproduce it, and a step that depends on the \
after-state needs it baked in with capture_edits.

Apply exactly what the instruction asks for — and nothing else. Preserve all untouched \
steps and fields verbatim. If the instruction is ambiguous, make the most reasonable \
minimal interpretation.

QUESTIONS: the author's message is not always an edit. They also ask things ("is \
this field required?", "do we have the dropdown's options from the capture?", "why \
doesn't this open?"). When the message asks rather than instructs, do not edit: check \
the captured pages with your tools — read_page_html, view_screenshot, probe_target — \
and call `answer_author` with a direct answer grounded in what you found. A problem \
report phrased as a question ("this list doesn't open — do we have the options?") is a \
question: answer what is and is not in the capture, and when a change would fix it, \
offer that change as `suggested_change` rather than making it. Only a message that \
plainly asks for a change ("…and add them", "fix it") is an edit.

Everything you change must keep meeting the walkthrough standards:

- FROZEN PAGES: the snapshots are frozen NAVIGATION, not dead documents. Nothing \
fetches and nothing navigates — clicking cannot open a dialog, switch a tab, or load a \
page — but the page's OWN SCRIPTS STILL RUN, and the player dispatches real input and \
change events as it types and selects. An inline handler that recomputes a total or \
relabels a button WILL fire during playback. So before editing behaviour into a page, \
check whether the page already implements it (a `reactive:` marker in the inventory, or \
read_page_html around the element); if it already does what the instruction asks, \
change nothing there and say so in `summary`. Every step's target element must already \
exist on the page loaded for that step (`page: null` means "same page as the previous \
step"). The first step acting inside a dialog must load the snapshot where that dialog \
is already open. Only the provided snapshot paths are valid `page` values.
- TARGETS: when an edit changes which element a step targets (or adds a step), pick the \
element from the inventory of that step's page and write `"target": {"ref": "e17"}` — \
the server substitutes its verified selector. Never invent a selector and never copy a \
`sel:` string by hand. A ref is only valid on its own page (or its listed duplicates). A \
`drag` step carries two refs from the same page: `target` (the `draggable` picked up) and \
`drop_target` (a DROP ZONES line — never usable as a `target`). \
Steps the instruction does not touch keep their existing {"selector": …} targets \
verbatim; write a selector yourself only for a page marked "raw HTML fallback", scoped \
to a container, with `text_exact` when a substring would collide.
- VOICES: `instruction` — second person, task plus why; `showme_caption` — narrator "we", \
explains the reasoning; `hint` — concrete on-screen location. Selects carry \
`wrong_value_feedback` (moot once `accept_any` is set); inputs carry a realistic \
`demo_value` and sensible `min_length`.
- MERGED RUNS: `"continues": true` makes a step play straight on from the one before it \
with no pause — used for a stretch of form fields that are one thing to do. Honour an \
instruction to merge or unmerge steps by setting or removing it. Never on the first step, \
never on a step that sets `page`, and never across a real beat change (a form's fields \
merge; the button that submits it does not). The classic player shows the run's FIRST \
step title and instruction for the whole run; the popup player shows each step's own \
title. So when you merge steps, retitle the first step for the run ("Enter the contact \
details") while keeping real per-field titles on the later steps, and when you unmerge \
one give it back a standalone title.
- FUNCTIONAL EDITS: an instruction may change behaviour, not just wording — the action a \
step expects (its `type`, `value`, `accept_any`, `min_length`), the element it targets, \
or the page markup itself. Honour such instructions literally; never reduce a functional \
request to a text rewrite. In You-try mode the player enforces the action mechanically — \
a select is judged against `value`, an input against `min_length` — so rewording an \
instruction or deleting `wrong_value_feedback` loosens NOTHING. To stop enforcing one \
particular dropdown choice, you must set `accept_any: true` on the select action (keep \
`value` — Show-me still demos it). To teach the drag an author actually performed where \
the walkthrough shows a click instead, replace the action with {"type": "drag", \
"target": {"ref": "eNN"}, "drop_target": {"ref": "eMM"}} and rewrite the step's three \
voices for a drag. Show-me draws a select's option list open on its own \
when it demos a `select` step — the cursor travels to the chosen option before the value \
changes — so "the dropdown does not expand" is never fixed in the page: do not add `size` \
to a select, swap it for radios, or otherwise change a control's kind to make its options \
visible. Leave the markup alone and say so in `summary`.
- Keep `schema`, `id`, and `level` unchanged; step ids stay semantic; no \
`_source` block; `estimated_minutes` about 30 seconds per step (minimum 2), counting a \
merged run as one.

You can also change what the captured pages themselves show. Alongside the updated \
walkthrough, you may return find/replace operations that are applied literally (exact, \
case-sensitive) to the captured snapshot HTML files. An operation without a `page` \
applies to EVERY snapshot; with `"page": "<snapshot path>"` it applies to that snapshot \
(and its byte-identical duplicates) only. Scope an op to its page whenever the change \
belongs to one state of the app — an unscoped find that also occurs on other pages will \
rewrite them all, and the validator will tell you so:

- Use them whenever the instruction changes what a page shows OR how it behaves: member \
names, emails, ids, point balances, labels, statuses, dialog text — and just as much the \
markup itself, like a select's option list, a `required`/`disabled`/`maxlength` \
attribute, or an element the instruction wants altered or gone.
- A `find` string must be copied byte-exactly from the digest: either visible text from \
the VISIBLE TEXT OUTLINE (stay inside one line — outline lines never span an element \
boundary, and a string that crosses one will not exist in the file) or markup from an \
inventory line's `src:` excerpt — a CONTIGUOUS run only, never spanning an `…` elision \
mark, because the elided content is in the file even though the excerpt hides it. When \
in doubt, verify_capture_edit settles it. Make it \
specific enough not to hit unintended matches, and cover every variant that appears on \
the pages (full name, email address, member id, possessives, etc.) with separate \
operations. Anything not visible in a digest cannot be safely edited this way — leave \
it alone and say so in `summary` instead of guessing at markup you cannot see.
- Keep the walkthrough's targets, instructions, captions, demo values and scenario consistent \
with the pages AFTER these operations are applied.
- PREFER THE STRUCTURED FORMS when the digest names the element — they are compiled \
against the element's exact bytes in its own page and refused when the anchor is absent \
or ambiguous, so they cannot over-edit:
  {"op": "set_text", "ref": "d294", "value": "$2,500.00"} — replace a plain-text \
element's content;
  {"op": "set_attribute", "ref": "e169", "attr": "maxlength", "value": "6"} — set or \
add an attribute; {"value": ""} writes a bare attribute (required, disabled), \
{"remove": true} deletes it.
Fall back to literal find/replace for anything they refuse.
- If the pages need no changes, return an empty list.

""" + schema_reference(
    'A target you change or add is {"ref": "eNN"}; an untouched target stays the '
    '{"selector": css, "text"/"text_exact": …} it already is.') + """

""" + EDIT_CLOSER


def _scenario_intent(scenario):
    return (f"Scenario intent: {scenario}" if scenario else
            "Scenario intent: none provided — derive it from the captured pages and "
            "recorded actions, using the fictional people and data visible on the pages.")


def _prompt_head(walkthrough_id, scenario, draft):
    return "\n\n".join([
        f"Walkthrough id: {walkthrough_id}",
        _scenario_intent(scenario),
        "--- DRAFT walkthrough.json ---",
        json.dumps(draft, indent=2, ensure_ascii=False),
        "--- CAPTURED PAGE DIGESTS (their paths are the only valid `page` values) ---",
    ])


def build_user_prompt(walkthrough_id, scenario, draft, page_blocks,
                      closer="Now return the complete authored walkthrough.json."):
    parts = [_prompt_head(walkthrough_id, scenario, draft)]
    parts.extend(page_blocks)
    parts.append(closer)
    return "\n\n".join(parts)


def build_user_content(walkthrough_id, scenario, draft, page_blocks, page_rels,
                       closer, note_overflow=False):
    """Content-block form of build_user_prompt: each digest block is followed
    by its page's capture-time screenshot (up to MAX_INLINE_SCREENSHOTS), so
    hints and instructions are written from what is actually on screen.
    Returns a plain string (same as build_user_prompt) when no page has a
    screenshot — old captures predate them.

    Which pages HAVE a screenshot comes from one listing; only the ones that
    are actually inlined are fetched. On disk that distinction did not matter —
    `is_file()` was a stat and the Path was read later or not at all — but a
    fetch is a transfer of a full-size PNG, and a thirteen-page capture would
    have paid for thirteen of them to inline eight.
    """
    frames = set(store.capture_files(walkthrough_id, ".png"))
    has_shot = [f"{rel.split('/', 1)[0]}/screenshot.png" in frames
                for rel in page_rels]
    if not any(has_shot):
        return build_user_prompt(walkthrough_id, scenario, draft, page_blocks,
                                 closer=closer)
    content = [{"type": "text",
                "text": _prompt_head(walkthrough_id, scenario, draft)}]
    inlined = 0
    for rel, block, present in zip(page_rels, page_blocks, has_shot):
        content.append({"type": "text", "text": block})
        if not present or inlined >= MAX_INLINE_SCREENSHOTS:
            continue
        shot = _screenshot_for(walkthrough_id, rel)
        if shot is None:      # listed a moment ago, gone now — not worth failing over
            continue
        content.append({"type": "text",
                        "text": f"Capture-time screenshot of {rel} "
                                f"(what the learner saw):"})
        content.append(_image_block(shot))
        inlined += 1
    skipped = sum(has_shot) - inlined
    if skipped > 0 and note_overflow:
        closer = (f"({skipped} more page screenshot(s) were not inlined — use "
                  f"the view_screenshot tool to see them.)\n\n{closer}")
    content.append({"type": "text", "text": closer})
    return content


def example_walkthrough_block(owner=None):
    """Published walkthrough from the same product as a one-shot quality reference.

    Reads the repo-bundled fixture, so there is no per-account content here and
    nothing to leak between owners; `owner` is accepted for call-site symmetry.
    """
    p = ROOT / EXAMPLE_WALKTHROUGH_FILE
    if not p.is_file():
        return ""
    try:
        example = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if "TODO" in json.dumps(example):
        return ""  # not actually published; skip
    return ("\n\nHere is a published walkthrough from a DIFFERENT product (a loyalty-"
            "program admin console), as a reference for quality, tone, and selector "
            "style ONLY. It describes a DIFFERENT task on "
            "DIFFERENT captured pages: never copy its step `id`s, its `page` paths, or its "
            "wording into your output — use the snapshot paths and actions you were "
            "actually given.\n" +
            json.dumps(example, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- API call

def estimate_tokens(text: str) -> int:
    """Rough input-token count for a prompt that is mostly captured markup.

    Deliberately pessimistic. Dense HTML — long attribute values, hashed class
    names, few word boundaries — measured at ~1.95 chars/token on a real
    QuickBooks capture, against the ~4 chars/token a prose-shaped prompt gets.
    Dividing by 2 tracks the markup case and overestimates prose, which is the
    right way round for a guard: it can refuse slightly early, never too late.
    """
    return len(text) // 2


def _count_tokens(system: str, user: str, model: str):
    """Exact input-token count from the count_tokens endpoint, or None.

    None on any failure — the counter is a free endpoint but still a network
    call, and a broken counter must never block authoring (same philosophy as
    langsmith_trace: observability degrades, the product call proceeds).
    """
    try:
        resp = _client().messages.count_tokens(
            model=model, system=system,
            messages=[{"role": "user", "content": user}])
        return resp.input_tokens
    except Exception:
        return None


def preflight_prompt_size(system: str, user: str, model: str = None):
    """Refuse an over-budget prompt with something the author can act on.

    Without this the request goes out and comes back as a bare 'API error 400:
    prompt is too long', which reads like a bug in the pipeline rather than an
    over-large capture.

    Two-stage: the len//2 estimate is the fast path (pessimistic — it can only
    refuse early, never too late), and only when it trips does the exact
    count_tokens endpoint get a say. Prose-heavy prompts that the pessimistic
    estimate would have refused (~4 chars/token, not 2) are rescued by the
    real count; a genuinely over-budget prompt is still refused.
    """
    est = estimate_tokens(system) + estimate_tokens(user)
    if est <= INPUT_TOKEN_BUDGET:
        return
    real = (_count_tokens(system, user, model or _model_from_env())
            if os.environ.get("ANTHROPIC_API_KEY") else None)
    if real is not None and real <= INPUT_TOKEN_BUDGET:
        return
    shown = real if real is not None else est
    raise AuthorError(
        f"This capture is too large for one authoring pass: about {shown:,} input "
        f"tokens against a {INPUT_TOKEN_BUDGET:,} budget. The pages have already "
        f"been trimmed and pruned. Re-record the flow with fewer capture steps "
        f"(the authoring pass consolidates actions anyway, so 5-7 steps is "
        f"usually plenty), or split it into two walkthroughs.")


_CLIENT = None
_CLIENT_KEY = None


def _client() -> anthropic.Anthropic:
    """Lazy singleton, rebuilt if the key or workspace changes (tests patch
    the env).

    A key that is not scoped to a workspace is refused by the API unless the
    request names one in the anthropic-workspace-id header. Set
    ANTHROPIC_WORKSPACE_ID to send it on every call; a key that is already
    scoped to a workspace does not need it and the variable can stay unset.
    """
    global _CLIENT, _CLIENT_KEY
    key = os.environ.get("ANTHROPIC_API_KEY")
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    ident = (key, workspace)
    if _CLIENT is None or ident != _CLIENT_KEY:
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        _CLIENT = anthropic.Anthropic(api_key=key, timeout=TIMEOUT,
                                      max_retries=RETRIES,
                                      default_headers=headers)
        _CLIENT_KEY = ident
    return _CLIENT


def _accumulate_stream(events) -> dict:
    """Fold raw Messages API stream events into the final message dict.

    Deliberately reads events as plain JSON (`to_dict()`) and never touches a
    typed attribute. The SDK's own accumulator does the opposite, and that is
    what made it fragile: when an event fails strict validation the SDK falls
    back to "first union variant that deserialises", which is
    `BetaRawMessageStartEvent` — a model with no `delta` field. The loose
    constructor does not enforce the `type` literal, so you get an object
    that reports `type == "message_delta"` while `delta` is still a raw dict,
    and the accumulator's `event.delta.container` dies with

        'dict' object has no attribute 'container'

    on a response that is perfectly well-formed. That happens whenever the
    API ships a field ahead of the installed SDK — routine on the beta
    surface this module runs on (`fallbacks`, `output_config`). Folding the
    JSON ourselves means an unknown field is carried through instead of
    crashing the pass, which is the behaviour every caller here already
    assumes: they all read `data` with `.get`.

    Unknown event types are ignored rather than raising — a new event kind is
    additive by construction, and the finished message does not depend on it.
    """
    message = None
    for raw in events:
        event = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        kind = event.get("type")

        if kind == "message_start":
            message = dict(event.get("message") or {})
            message["content"] = list(message.get("content") or [])
            continue
        if message is None:
            # Everything below folds into a message that must already exist.
            continue

        if kind == "content_block_start":
            index = event.get("index")
            block = dict(event.get("content_block") or {})
            content = message["content"]
            if not isinstance(index, int):
                index = len(content)
            while len(content) <= index:
                content.append({})
            content[index] = block
            # The fallback hop names the model that actually served the
            # response; the non-streaming message relabels itself the same way.
            if block.get("type") == "fallback" and isinstance(block.get("to"), dict):
                message["model"] = block["to"].get("model", message.get("model"))

        elif kind == "content_block_delta":
            block = _stream_block(message, event.get("index"))
            if block is None:
                continue
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + (delta.get("text") or "")
            elif dtype == "thinking_delta":
                block["thinking"] = (block.get("thinking", "")
                                     + (delta.get("thinking") or ""))
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature") or ""
            elif dtype == "input_json_delta":
                # Buffered raw and parsed once at content_block_stop: partial
                # JSON is not JSON, and only the whole of it has to parse.
                block[_JSON_BUF] = (block.get(_JSON_BUF, "")
                                    + (delta.get("partial_json") or ""))
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
            elif dtype == "compaction_delta":
                block["content"] = delta.get("content")
                block["encrypted_content"] = delta.get("encrypted_content")

        elif kind == "content_block_stop":
            block = _stream_block(message, event.get("index"))
            if block is not None and _JSON_BUF in block:
                buffered = block.pop(_JSON_BUF).strip()
                try:
                    block["input"] = json.loads(buffered) if buffered else {}
                except ValueError as e:
                    raise AuthorError(
                        f"The model's tool call did not parse as JSON ({e}). "
                        f"Re-run the pass.")

        elif kind == "message_delta":
            delta = event.get("delta") or {}
            for field in ("stop_reason", "stop_sequence", "stop_details",
                          "container"):
                if field in delta:
                    message[field] = delta[field]
            usage = message.setdefault("usage", {})
            for field, value in (event.get("usage") or {}).items():
                # The delta reports only what changed; a null is "no news",
                # not a reset to zero.
                if value is not None:
                    usage[field] = value
            if event.get("context_management") is not None:
                message["context_management"] = event["context_management"]

    if message is None:
        raise AuthorError("The API stream ended without a message — no "
                          "message_start event arrived. Re-run the pass.")
    return message


# Where a tool_use block's partial JSON is buffered mid-stream. Popped at
# content_block_stop, so it never reaches a caller.
_JSON_BUF = "_partial_json"


def _stream_block(message: dict, index) -> dict:
    """The content block a delta addresses, or None if the index is bogus."""
    content = message.get("content") or []
    if isinstance(index, int) and 0 <= index < len(content):
        return content[index]
    return None


def _send_request(params: dict) -> dict:
    """One Messages API call, returned as a plain dict.

    A seam: tests patch this to assert the exact request params without
    touching the network. Streams under the hood — a request can think for
    minutes and the SDK refuses long non-streaming calls — but callers still
    block for the final message. Goes through the beta surface because the
    server-side refusal fallback (`fallbacks`) lives there; everything else
    is identical to the non-beta path.

    Accumulates the stream itself rather than calling the SDK's
    `.stream(...).get_final_message()`: see `_accumulate_stream` for why that
    helper cannot be trusted on a beta surface that moves faster than the
    installed SDK.
    """
    with _client().beta.messages.create(**params, stream=True) as stream:
        return _accumulate_stream(stream)


def _request(params: dict) -> dict:
    """_send_request with SDK errors mapped onto AuthorError, and the
    response's token usage recorded (usage.py) before anyone inspects it — a
    truncated or refused reply was still billed."""
    try:
        data = _send_request(params)
        ai_usage.record(data, params.get("model"))
        return data
    except anthropic.APIStatusError as e:
        try:
            body = e.response.text[:300]
        except Exception:
            body = str(e)[:300]
        raise AuthorError(f"API error {e.status_code}: {body}")
    except anthropic.APIConnectionError as e:  # includes APITimeoutError
        raise AuthorError(f"Network error reaching the Anthropic API: {e}")


def _content_text(user) -> str:
    """The text portion of a user prompt (a plain string, or the text blocks
    of a Messages API content-block list)."""
    if isinstance(user, str):
        return user
    return "\n".join(b.get("text", "") for b in user
                     if isinstance(b, dict) and b.get("type") == "text")


def _served_by_fallback(data: dict) -> bool:
    """True when a policy decline on the primary was re-served by the
    server-side fallback model (a `fallback_message` usage iteration)."""
    return any(isinstance(it, dict) and it.get("type") == "fallback_message"
               for it in (data.get("usage") or {}).get("iterations") or [])


def call_claude(system: str, user, model: str, *, effort: str = EFFORT,
                max_tokens: int = MAX_TOKENS, output_schema: dict = None,
                refusal_hint: str = None) -> str:
    """One model call. `user` is a plain string or a Messages API content-block
    list (text + image blocks) sent as a single user message.

    The keyword knobs exist because the call sites have very different shapes:
    the authoring pass is the quality-critical call and thinks hard, while the
    render-check verdict is three fields and should not. `output_schema`
    switches on structured outputs — the response text is then guaranteed to
    be JSON matching the schema, so callers can json.loads it directly.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthorError("ANTHROPIC_API_KEY is not set, and AI authoring and edits "
                          "call the Anthropic API. " + envfile.missing_key_hint())
    preflight_prompt_size(system, _content_text(user), model)
    output_config = {"effort": effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    params = {
        "model": model,
        "max_tokens": max_tokens,
        # Both sent explicitly. Omitting them does not mean "off" — it means the
        # API's defaults (adaptive thinking, "high" effort) apply invisibly, and
        # they share the max_tokens budget with the response.
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
        # The system prompt is a stable prefix (module constants plus the
        # deterministic exemplar), so it is marked cacheable: repeat calls
        # within the TTL read it at ~0.1x input price instead of re-paying it.
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    # Server-side refusal fallback: captured customer pages occasionally trip
    # a safety classifier on the primary model; "default" lets the API re-run
    # the declined request on Anthropic's recommended fallback for the refusal
    # category instead of failing the pass. Only attached on models that
    # support the parameter — on others it would be a 400.
    fallbacks_attached = supports_fallbacks(model)
    if fallbacks_attached:
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"

    # Trace the text parts and an image count, never the base64 — the span
    # clipper would truncate it anyway and it is noise in LangSmith.
    n_images = 0 if isinstance(user, str) else sum(
        1 for b in user if isinstance(b, dict) and b.get("type") == "image")
    traced_user = (user if isinstance(user, str)
                   else f"[{n_images} image(s)]\n{_content_text(user)}")
    metadata = {"ls_provider": "anthropic", "ls_model_name": model,
                "max_tokens": max_tokens, "effort": effort}
    if n_images:
        metadata["images"] = n_images

    # ls_model_name is what LangSmith matches to price a custom LLM run; without
    # it the token counts still arrive but the cost column stays empty.
    with langsmith_trace.span(
        "anthropic.messages", run_type="llm",
        inputs=langsmith_trace.chat_inputs(system, traced_user),
        metadata=metadata,
    ) as sp:
        data = _request(params)
        usage = data.get("usage") or {}
        # The API reports exactly what every sizing decision here needs.
        # Discarding it is why the token numbers in this module had to be
        # reconstructed by measuring files on disk. cache_read at ~0.1x price
        # is the caching working; a persistent 0 across repeat calls means a
        # silent prefix invalidator crept in.
        print(f"[author] {model} {'vision ' if n_images else ''}effort={effort} "
              f"in={usage.get('input_tokens', '?')} "
              f"out={usage.get('output_tokens', '?')}/{max_tokens} "
              f"cache_read={usage.get('cache_read_input_tokens') or 0} "
              f"cache_write={usage.get('cache_creation_input_tokens') or 0}")
        sp.add_metadata({
            "stop_reason": data.get("stop_reason"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
            "cache_creation_input_tokens":
                usage.get("cache_creation_input_tokens") or 0,
        })
        # A fallback_message iteration means a policy decline was re-served by
        # the fallback model — worth a visible line, because the response's
        # `model` is then not the one that was asked for.
        fallback_ran = _served_by_fallback(data)
        if fallback_ran:
            print(f"[author] served by fallback model {data.get('model')} "
                  f"(primary declined)")
            sp.add_metadata({"served_by_fallback": data.get("model")})
        if data.get("stop_reason") == "max_tokens":
            raise AuthorError(
                f"Model response was truncated: it hit max_tokens "
                f"({usage.get('output_tokens', '?')}/{max_tokens}). Note that "
                f"budget covers thinking AND the response, so the walkthrough "
                f"itself is usually far smaller than the number above — try "
                f"lowering the effort for this call (see EFFORT / "
                f"AUTHOR_EFFORT, this call ran {effort!r}) before raising "
                f"max_tokens.")
        # A safety classifier declining the request is a 200 with an
        # empty or partial `content`, not an HTTP error. Without this it
        # surfaces as "empty response", which sends the author looking
        # for a bug in the prompt assembly instead of at the capture.
        if data.get("stop_reason") == "refusal":
            detail = (data.get("stop_details") or {}).get("category")
            raise AuthorError(
                "The model declined this request"
                + (f" ({detail})" if detail else "")
                + ". " + (refusal_hint or
                          "The prompt carries the captured pages verbatim, so "
                          "something in the capture tripped a safety classifier "
                          "— recapture without it, or set CAPTCHER_MODEL to "
                          "another model.")
                + (" (A fallback model was also tried and declined too.)"
                   if fallbacks_attached else ""))
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        # Recorded before the empty-response check so a blank reply is
        # visible in the trace as a blank reply, not just an error.
        sp.set_outputs(langsmith_trace.chat_outputs(text, usage))
        if not text.strip():
            raise AuthorError("Model returned an empty response.")
        return text


def call_claude_vision(system: str, content: list, model: str, *,
                       effort: str = EFFORT, max_tokens: int = MAX_TOKENS,
                       output_schema: dict = None) -> str:
    """Multimodal wrapper over call_claude, kept as a named seam — render_check
    and its tests patch this symbol. `content` is a content-block list (image
    blocks + text blocks) sent as a single user message."""
    return call_claude(
        system, content, model, effort=effort, max_tokens=max_tokens,
        output_schema=output_schema,
        refusal_hint="The images are captured customer pages, so something in "
                     "them tripped a safety classifier.")


def extract_json(text: str) -> dict:
    """Parse the model output; tolerate stray fences/prose around the JSON."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AuthorError("Model output contained no JSON object.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AuthorError(f"Model output was not valid JSON: {e}")


# ------------------------------------------------------------- agentic loop
# Both passes give the model the validator (and a few read tools) and end only
# when a submit tool validates clean, so the model repairs its own errors
# inside the same run rather than the author re-paying for a re-run the model
# never learns from.

MAX_LOOP_ITERATIONS = 8


class LoopExhausted(AuthorError):
    """The loop ran out of turns without an accepted submission.

    An AuthorError subclass so an uncaught one still reaches the author as a
    422 with a readable message. Both passes do catch it, because by the time
    the turns run out the run has usually produced *something* that validated
    clean — see `_salvage_or_raise`. `errors` is the last validation result
    seen, which is what makes the failure message specific instead of "the
    loop did not finish".
    """

    def __init__(self, message, errors=None, tools_called=None):
        super().__init__(message)
        self.errors = list(errors or [])
        # {tool name: calls} over the whole loop. What tells "every candidate
        # failed validation" apart from "the model never offered one".
        self.tools_called = dict(tools_called or {})


class LoopTruncated(LoopExhausted):
    """A loop turn hit max_tokens.

    A LoopExhausted so both passes' salvage handlers see it: a truncated turn
    cannot be continued (its tool call is cut off mid-JSON), but an earlier
    turn may already have validated clean, and throwing that away with the
    truncated turn is the same waste `_salvage_or_raise` exists to stop. With
    nothing to salvage it is re-raised unchanged, so the author still gets the
    truncation message and its advice about effort.
    """


def _salvage_or_raise(salvage, exhausted, what):
    """Recover a clean candidate from an exhausted loop, or fail usefully.

    Eight turns of Opus with inlined screenshots is the most expensive thing
    this server does, and the old behaviour threw all of it away the moment
    the model failed to call submit — including runs where an earlier
    `validate_walkthrough` had already come back with zero errors and the
    model then spent its remaining turns polishing wording.

    Salvaging is safe precisely because the candidate went through `_check`:
    the same resolve → invariants → validate chain a submit runs, and the same
    one the editor's save runs. This is not "write it anyway" — an unvalidated
    candidate is still refused. It is "the model already proved this one".
    """
    if salvage.get("clean") is not None:
        print(f"[author] {what}: loop exhausted — recovered the last "
              f"validated candidate")
        return salvage["clean"]
    if isinstance(exhausted, LoopTruncated):
        raise exhausted
    called = getattr(exhausted, "tools_called", None) or {}
    if not salvage.get("errors") and not any(called.get(t) for t in CHECK_TOOLS):
        # Not a validation failure at all: every turn went on reading, so
        # "nothing validated" would send the author looking for errors that
        # were never produced.
        raise LoopExhausted(
            f"The {what} used all {MAX_LOOP_ITERATIONS} turns examining the "
            f"captured pages ({_describe_tool_counts(called)}) and never "
            f"submitted anything to check, so nothing was written. The request "
            f"may need a change it could not work out how to make from the "
            f"captured pages — try describing it more specifically.",
            tools_called=called) from exhausted
    detail = ("; ".join(salvage.get("errors") or []) or
              "it never produced a candidate that validated")
    raise LoopExhausted(
        f"The {what} did not finish in {MAX_LOOP_ITERATIONS} turns and nothing "
        f"it produced validated cleanly, so nothing was written. Last "
        f"validation result: {detail}",
        errors=salvage.get("errors"), tools_called=called) from exhausted


# The tools that run a candidate through `_check`. A loop that never called
# one has no validation result to report.
CHECK_TOOLS = ("validate_walkthrough", "submit_walkthrough", "submit_edit")

# Plural nouns for the exhaustion message; an unknown tool falls back to its name.
_TOOL_COUNT_NOUNS = {
    "read_page_html": ("page read", "page reads"),
    "view_screenshot": ("screenshot", "screenshots"),
    "verify_capture_edit": ("match count", "match counts"),
    "probe_target": ("player probe", "player probes"),
    "answer_author": ("empty answer", "empty answers"),
}


def _describe_tool_counts(called):
    parts = []
    for name, n in sorted(called.items(), key=lambda kv: -kv[1]):
        one, many = _TOOL_COUNT_NOUNS.get(name, (name, name))
        parts.append(f"{n} {one if n == 1 else many}")
    return ", ".join(parts) or "no tool calls"


# A submission that validates with no errors but with warnings is handed back
# ONCE before it is accepted. Warnings used to be accepted silently on a direct
# submit, and the ones validate() raises are mostly real defects the learner
# would hit — a hint left empty, target text that is not on the page, a target
# that matches several elements so the player may ring the wrong one. The model
# only saw them if it happened to call validate_walkthrough first. One review
# round costs one turn and only when there is something to review; the second
# submission is accepted whatever it still carries, so a warning the model
# judges harmless can never trap the loop.
WARNINGS_REVIEW_NOTE = (
    "No errors, but the warnings below were found. Fix any that point at a real "
    "problem for the learner, then submit again. If you judge a warning "
    "harmless, resubmit unchanged — the next submission is accepted.")


def _reviewable_warnings(warnings):
    """Warnings worth one review round. Render-check warnings are excluded:
    they describe how a captured page renders, which the model cannot change
    from the walkthrough, and they appear on every submit for that page."""
    return [w for w in warnings or [] if "the render check found" not in w]


_WALKTHROUGH_INPUT = {
    "type": "object",
    "properties": {"walkthrough": {
        "type": "object",
        "description": "The complete walkthrough.json object.",
    }},
    "required": ["walkthrough"],
}

VALIDATE_TOOL = {
    "name": "validate_walkthrough",
    "description": ("Check a work-in-progress walkthrough. Runs the same ref "
                    "resolution and validation the final save runs and returns "
                    "{errors, warnings}. Use it for a draft you are not yet "
                    "ready to submit — a submit runs the same checks, so "
                    "validating a finished walkthrough and then submitting it "
                    "only writes the whole thing out twice."),
    "input_schema": _WALKTHROUGH_INPUT,
}

SUBMIT_WALKTHROUGH_TOOL = {
    "name": "submit_walkthrough",
    "description": ("Submit the finished walkthrough.json. Validates like "
                    "validate_walkthrough; if errors come back, fix exactly "
                    "those errors and submit again. A submission with no "
                    "errors but with warnings is returned once so you can "
                    "fix them; resubmit (changed or unchanged) and it is "
                    "accepted. On success the run is complete."),
    "input_schema": _WALKTHROUGH_INPUT,
}

READ_PAGE_TOOL = {
    "name": "read_page_html",
    "description": ("Read raw (trimmed) snapshot HTML for one captured page. "
                    "With `query`, returns up to 5 excerpt windows around "
                    "case-insensitive matches; without it, the head of the "
                    "page. Use only when a digest excerpt is not enough."),
    "input_schema": {
        "type": "object",
        "properties": {
            "page": {"type": "string",
                     "description": "A snapshot path exactly as listed in the digests, e.g. step-002/page.html"},
            "query": {"type": "string",
                      "description": "Text to locate in the page HTML (visible text or markup)"},
        },
        "required": ["page"],
    },
}

VERIFY_CAPTURE_EDIT_TOOL = {
    "name": "verify_capture_edit",
    "description": ("Count how many times a proposed capture-edit `find` "
                    "string occurs in the captured HTML files, per file, "
                    "with the surrounding file text for the first matches. "
                    "Use it before submitting any capture edit you are not "
                    "certain of — a find that matches 0 times will do "
                    "nothing, one that matches more than intended will "
                    "over-edit, and the context shows how the replacement "
                    "will read in place."),
    "input_schema": {
        "type": "object",
        "properties": {"find": {"type": "string",
                                "description": "The exact, case-sensitive string to count."}},
        "required": ["find"],
    },
}

PROBE_TOOL = {
    "name": "probe_target",
    "description": ("Render a captured page in the REAL player (headless "
                    "browser, bridge injected) and probe a target: returns "
                    "the live match count and a screenshot with the first "
                    "match ringed by the spotlight, scrolled into view. The "
                    "one tool that shows what a learner will actually see. "
                    "Use it to confirm a retargeted or newly added step "
                    "before submitting, or when a digest verdict and the "
                    "instruction disagree. Probes the pages as saved on disk "
                    "— pending capture_edits are NOT applied."),
    "input_schema": {
        "type": "object",
        "properties": {
            "page": {"type": "string",
                     "description": "A snapshot path exactly as listed in the digests"},
            "ref": {"type": "string",
                    "description": "An interactive element ref (eNN) from that page's inventory"},
            "selector": {"type": "string",
                         "description": "A CSS selector, when no ref fits"},
            "text": {"type": "string",
                     "description": "Optional substring filter alongside `selector`"},
            "text_exact": {"type": "string",
                           "description": "Optional exact-text filter alongside `selector`"},
        },
        "required": ["page"],
    },
}

MAX_PROBES_PER_RUN = 6          # headless launches are seconds each — bound them


def _probe_tool_handler(replay_ctx, walkthrough_id, ref_table, alias_map,
                        valid_pages):
    """probe_target: the edit loop's eyes. replay_ctx is (base_url,
    cookie_header) — the same self-addressing the verify pass uses. Lazy
    import: verify_pass imports author, so the top level would be circular."""
    from . import verify_pass as _vp
    calls = {"n": 0}

    def handler(inp):
        page = str(inp.get("page") or "")
        canon = (alias_map or {}).get(page, page)
        if canon not in valid_pages:
            return (f"Unknown page {page!r}. Valid pages: "
                    + ", ".join(sorted(valid_pages))), None
        if calls["n"] >= MAX_PROBES_PER_RUN:
            return (f"Probe budget exhausted ({MAX_PROBES_PER_RUN} per edit) — "
                    f"validate and submit with what you have verified."), None
        ref = str(inp.get("ref") or "")
        if ref:
            entry = ref_table.get(ref)
            if entry is None:
                return f"{ref!r} is not in the page digests.", None
            if entry.get("display"):
                return (f"{ref} is a display element — it has no verified "
                        f"step target to probe. Probe by `selector` if you "
                        f"need to see it."), None
            if entry["page"] != canon:
                return f"{ref} lives on {entry['page']}, not {page}.", None
            target = dict(entry["target"])
        elif inp.get("selector"):
            target = {"selector": str(inp["selector"])}
            if inp.get("text_exact"):
                target["text_exact"] = str(inp["text_exact"])
            elif inp.get("text"):
                target["text"] = str(inp["text"])
        else:
            return "Give either a `ref` or a `selector` to probe.", None
        calls["n"] += 1
        base_url, cookie_header = replay_ctx
        try:
            with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
                d = _vp.probe_one(base_url, cookie_header, walkthrough_id,
                                  canon, target, Path(tmp))
                count, shot = d.get("count", -1), d.get("shot")
                if count < 0:
                    return (f"The page or bridge did not answer the probe on "
                            f"{canon} — treat the target as unverified."), None
                head = (f"Live probe on {canon}: the target matches {count} "
                        f"element(s)."
                        + (" The first match is ringed in this player "
                           "screenshot:" if count and shot else ""))
                if count and shot and Path(shot).is_file():
                    # A probe shot is a real file in a TemporaryDirectory that
                    # is about to be deleted — it is evidence for one turn, not
                    # an artefact of the walkthrough, so it is never stored and
                    # the bytes are read here rather than by _image_block.
                    return [{"type": "text", "text": head},
                            _image_block(Path(shot).read_bytes())], None
                return head, None
        except _vp.VerifyError as e:
            return f"Probe unavailable: {e}", None
    return handler


# ---- structured capture ops ------------------------------------------------
# Literal find/replace is the escape hatch; these are the safe path for the
# common cases. Each is compiled server-side against the element's exact
# bytes in its OWN page and refused when the anchor is absent or ambiguous —
# so a structured op cannot over-edit, and its failure mode is an error
# message, not a silent rewrite of three pages.

def _rewrite_start_tag(src: str, attr: str, value, remove: bool):
    """A start tag with one attribute set, replaced, or removed."""
    attr_re = re.compile(
        r"""\s+""" + re.escape(attr) + r"""(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?""",
        re.I)
    if remove:
        return attr_re.sub("", src, count=1)
    escaped = str(value).replace("&", "&amp;").replace('"', "&quot;")
    chunk = f' {attr}="{escaped}"' if value != "" else f" {attr}"
    if attr_re.search(src):
        # lambda replacement: the chunk is literal text, never regex escapes
        return attr_re.sub(lambda _m: chunk, src, count=1)
    end = -2 if src.rstrip().endswith("/>") else -1
    return src[:end] + chunk + src[end:]


def compile_structured_ops(ops, ref_table, walkthrough_id: str):
    """Compile set_attribute / set_text ops into page-scoped literal ops.

    Returns (literal_ops, errors). Passes op=replace (and legacy find/replace)
    items through untouched. Every compiled op is verified against the bytes
    of its ref's page before it is emitted: 0 anchors means the file differs
    from the parse (entities, exotic whitespace) and >1 means identical
    sibling tags — both are errors naming the fallback, never a guess."""
    literal, errors = [], []
    for op in ops or []:
        kind = str(op.get("op") or "replace")
        if kind == "replace" or "find" in op:
            literal.append(op)
            continue
        ref = str(op.get("ref") or "")
        entry = ref_table.get(ref)
        label = f'capture edit {kind} on {ref or "(no ref)"}'
        if entry is None:
            errors.append(f"{label}: that ref is not in the page digests")
            continue
        node = entry.get("node")
        src = getattr(node, "src", None)
        if not src:
            errors.append(f"{label}: the digests carry no source tag for it — "
                          f"use a literal find/replace instead")
            continue
        page = entry["page"]
        text = store.capture_text(walkthrough_id, page)
        if text is None:
            errors.append(f"{label}: could not read {page}")
            continue

        if kind == "set_attribute":
            attr = str(op.get("attr") or "").strip()
            if not attr or re.search(r"[\s\"'>/=]", attr):
                errors.append(f"{label}: `attr` must be a bare attribute name")
                continue
            find = src
            replace = _rewrite_start_tag(src, attr, op.get("value", ""),
                                         remove=bool(op.get("remove")))
        elif kind == "set_text":
            kids = getattr(node, "children", None) or []
            texts = [c for c in kids if isinstance(c, str)]
            if len(texts) != len(kids) or not texts:
                errors.append(f"{label}: set_text only works on elements whose "
                              f"content is plain text — this one has child "
                              f"elements (or none); use a literal find/replace")
                continue
            old = "".join(texts)
            find = src + old
            replace = src + str(op.get("value") or "")
        else:
            errors.append(f"{label}: unknown op {kind!r} — use replace, "
                          f"set_attribute, or set_text")
            continue

        n = text.count(find)
        if n == 0:
            errors.append(f"{label}: its exact bytes were not found in {page} "
                          f"(the file differs from the parse — likely "
                          f"entities or whitespace); use a literal "
                          f"find/replace copied from the digest instead")
            continue
        if n > 1:
            errors.append(f"{label}: the anchor occurs {n} times in {page} "
                          f"(identical sibling markup) — use a literal "
                          f"find/replace with more surrounding context")
            continue
        literal.append({"find": find, "replace": replace, "page": page})
    return literal, errors


SUBMIT_EDIT_TOOL = {
    "name": "submit_edit",
    "description": ("Submit the finished edit: the complete updated "
                    "walkthrough.json, the capture_edits list (may be empty), "
                    "and a one-or-two-sentence past-tense summary. Validates "
                    "against the pages AFTER the capture edits are applied; "
                    "if errors come back, fix exactly those and submit again. "
                    "A submission with no errors but with warnings is "
                    "returned once so you can fix them; resubmit (changed or "
                    "unchanged) and it is accepted."),
    "input_schema": {
        "type": "object",
        "properties": {
            "walkthrough": {"type": "object",
                            "description": "The complete updated walkthrough.json object."},
            "capture_edits": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {
                              "op": {"type": "string",
                                     "enum": ["replace", "set_attribute", "set_text"],
                                     "description": ("Default replace (literal "
                                                     "find/replace). set_attribute and "
                                                     "set_text are compiled server-side "
                                                     "against a ref's exact bytes and "
                                                     "refused when ambiguous — prefer "
                                                     "them when the digest names the "
                                                     "element.")},
                              "find": {"type": "string"},
                              "replace": {"type": "string"},
                              "page": {"type": "string",
                                       "description": ("replace only, optional: restrict "
                                                       "the op to one snapshot path (and "
                                                       "its byte-identical duplicates). "
                                                       "Omit to apply to every captured "
                                                       "page.")},
                              "ref": {"type": "string",
                                      "description": "set_attribute/set_text: the element's digest ref (eNN or dNN)"},
                              "attr": {"type": "string",
                                       "description": "set_attribute: the attribute name"},
                              "value": {"type": "string",
                                        "description": ("set_attribute: the new value "
                                                        "(empty for a bare attribute like "
                                                        "required); set_text: the new text "
                                                        "content")},
                              "remove": {"type": "boolean",
                                         "description": "set_attribute: true removes the attribute"},
                          }},
            },
            "summary": {"type": "string"},
        },
        "required": ["walkthrough"],
    },
}

# The other way out of the edit loop. Authors ask the AI panel questions as
# often as they ask it for changes ("why doesn't this dropdown open — do we
# have the options from the capture?"), and with submit_edit as the only exit
# a question got answered by an edit nobody asked for. Calling this ends the
# run with NOTHING saved: no history row, no page writes, no undo needed.
ANSWER_TOOL = {
    "name": "answer_author",
    "description": ("Answer the author's question instead of editing. Ends "
                    "the run and saves nothing. Use it when the message asks "
                    "something rather than asking for a change — after "
                    "checking the captured pages with your tools, so the "
                    "answer rests on what is actually there. If what you "
                    "found suggests a change the author may want, put it in "
                    "`suggested_change` as one imperative sentence they can "
                    "apply with a click."),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string",
                       "description": ("Plain prose for the author, two to five "
                                       "sentences: the direct answer first, then "
                                       "the evidence (which page, which "
                                       "element). No markdown headings, no "
                                       "selectors unless they asked for one.")},
            "suggested_change": {"type": "string",
                                 "description": ("Optional. One imperative "
                                                 "sentence, written as an edit "
                                                 "instruction to this editor "
                                                 "(e.g. \"Add the five salutation "
                                                 "options to the picklist on "
                                                 "step 2's page\"). Omit when "
                                                 "nothing needs to change.")},
        },
        "required": ["answer"],
    },
}


# Screenshots ground the teaching content in what is actually on screen — the
# digests alone describe markup, and a hint written from DOM order routinely
# points at the wrong part of the page. Capture screenshots are viewport-sized
# (1280x800 default, retina up to ~2560px wide), inside the model's native
# 2576px ceiling — no resizing needed. The cap bounds worst-case image spend
# (~4.8K tokens each at full retina); pages past it stay reachable through the
# view_screenshot tool.
MAX_INLINE_SCREENSHOTS = 8

VIEW_SCREENSHOT_TOOL = {
    "name": "view_screenshot",
    "description": ("View the capture-time screenshot of one captured page — "
                    "what the learner actually saw on screen. Use it when a "
                    "page's screenshot was not inlined and you need the "
                    "visual layout to write a hint or judge visibility."),
    "input_schema": {
        "type": "object",
        "properties": {"page": {"type": "string",
                                "description": "A snapshot path exactly as listed in the digests, e.g. step-002/page.html"}},
        "required": ["page"],
    },
}


def _image_block(data: bytes) -> dict:
    """One image content block from PNG bytes.

    Bytes, not a path: a frame is an object in the captures folder, and the two callers
    that hold a real file (the probe and verify passes, whose screenshots live
    in a TemporaryDirectory for the seconds between the browser and the model)
    can read it themselves. render_check.py had to keep a local copy of this
    while it took a Path; it imports this one again.
    """
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(data).decode("ascii")}}


def _screenshot_for(walkthrough_id: str, rel: str):
    """The capture-time screenshot of a snapshot page as bytes, or None."""
    return store.capture_bytes(walkthrough_id,
                               f"{rel.split('/', 1)[0]}/screenshot.png")


def _view_screenshot_handler(walkthrough_id: str, alias_map, valid_pages):
    def handler(inp):
        page = str(inp.get("page") or "")
        canon = (alias_map or {}).get(page, page)
        shot = _screenshot_for(walkthrough_id, canon) if canon in valid_pages else None
        if shot is None:
            return (f"No capture-time screenshot exists for page {page!r} "
                    f"(older captures predate screenshots)."), None
        return [{"type": "text",
                 "text": f"Capture-time screenshot of {canon}:"},
                _image_block(shot)], None
    return handler


def _force_invariants(walkthrough: dict, source: dict, walkthrough_id: str):
    """Enforce the fields the model may neither drop nor invent, in place.

    `source` is the draft (authoring pass) or the current walkthrough (edit
    pass) — the record whose wiring survives regardless of what came back."""
    walkthrough["schema"] = "walkthrough-def@1"
    walkthrough["id"] = walkthrough_id
    # No `capture_dir`. It was a path from the definition to the capture folder
    # beside it, and it is gone from the schema entirely — the capture lives
    # under the walkthrough's own uuid, so `blobs.prefix_for(id)` is the whole
    # answer and it is derived rather than stored. Stamping it here as None
    # would put a dead key back into every definition this pass writes.
    if source.get("owner"):
        walkthrough["owner"] = source["owner"]
    walkthrough.pop("_source", None)
    # the capture window is recorded wiring, not authored content — the model
    # may neither drop it nor invent one
    if source.get("window"):
        walkthrough["window"] = source["window"]
    else:
        walkthrough.pop("window", None)
    # You-try lets a learner through an input step once they have typed
    # min_length characters (bridge.js, `min_length || 1`). A model answer
    # shorter than that — a rejected "0" or "31" carrying the usual 3 — left a
    # learner who typed exactly what Show-me typed stuck on the step for good.
    for step in walkthrough.get("steps") or []:
        action = step.get("action") if isinstance(step, dict) else None
        if not isinstance(action, dict) or action.get("type") != "input":
            continue
        demo = action.get("demo_value")
        if not isinstance(demo, str) or not demo.strip():
            continue   # validate() reports it
        typed = len(norm_ws(demo))
        ml = action.get("min_length")
        if not isinstance(ml, int) or isinstance(ml, bool) or ml < 1:
            action["min_length"] = min(3, typed)
        elif ml > typed:
            action["min_length"] = typed


def _read_page_tool_handler(pages, alias_map):
    """Progressive disclosure: the digests stay lean up front and the model
    pulls raw HTML on demand. Excerpts come from prepare_page output — with
    scripts kept (capped per block), unlike the inlined fallback pages: this
    tool is where the model goes to inspect behaviour, and a silently
    script-stripped page reads as an inert one. Never the raw multi-MB
    snapshot."""
    page_map = dict(pages)
    prepared = {}

    def handler(inp):
        page = str(inp.get("page") or "")
        canon = (alias_map or {}).get(page, page)
        if canon not in page_map:
            return (f"Unknown page {page!r}. Valid pages: "
                    + ", ".join(sorted(page_map))), None
        if canon not in prepared:
            prepared[canon] = prepare_page(page_map[canon], keep_scripts=True)
        body = prepared[canon]
        query = str(inp.get("query") or "")
        if not query:
            head = body[:20_000]
            return (f"First {len(head):,} of {len(body):,} chars of the "
                    f"prepared page (pass a `query` to search deeper):\n"
                    f"{head}"), None
        low, qlow = body.lower(), query.lower()
        windows, start = [], 0
        while len(windows) < 5:
            i = low.find(qlow, start)
            if i == -1:
                break
            windows.append(body[max(0, i - 2000): i + len(query) + 2000])
            start = i + len(query)
        if not windows:
            return (f"{query!r} does not occur in the prepared HTML of "
                    f"{canon}."), None
        sep = "\n\n--- next match ---\n\n"
        return (f"{len(windows)} match window(s) for {query!r} in {canon}:\n"
                + sep.join(windows)), None
    return handler


MATCH_CONTEXT_CHARS = 120       # each side of a verify_capture_edit match
MATCH_CONTEXT_WINDOWS = 4       # windows shown across all files


def _verify_capture_edit_handler(walkthrough_id: str):
    """verify_capture_edit: where does this find string actually land?

    The capture is read once per run, not once per call. This used to walk the
    capture directory on every invocation, which was a handful of local reads;
    the same walk over Storage is a full re-download of every page, and the
    model calls this tool repeatedly while it converges on an anchor. Caching is
    safe because nothing writes a capture page during a run — apply_capture_edits
    holds its results in memory and only the save writes them.
    """
    capture, state = {}, {"loaded": False}

    def handler(inp):
        find = str(inp.get("find") or "")
        if not find:
            return "`find` must be a non-empty string.", None
        if not state["loaded"]:
            state["loaded"] = True
            for rel in store.capture_files(walkthrough_id, ".html"):
                text = store.capture_text(walkthrough_id, rel)
                if text is not None:
                    capture[rel] = text
        counts, windows = {}, []
        for rel, text in sorted(capture.items()):
            n = text.count(find)
            if not n:
                continue
            counts[rel] = n
            # Context so the model can judge the replacement in situ — a
            # count alone says an edit will land, not that it will read
            # correctly where it lands.
            start = 0
            while len(windows) < MATCH_CONTEXT_WINDOWS:
                i = text.find(find, start)
                if i == -1:
                    break
                w = text[max(0, i - MATCH_CONTEXT_CHARS):
                         i + len(find) + MATCH_CONTEXT_CHARS]
                windows.append(f"--- in {rel} ---\n…{norm_ws(w)}…")
                start = i + len(find)
        if not counts:
            return (f'"{find}" occurs 0 times in the captured HTML — copy the '
                    f"find string byte-exactly from the digest (an outline "
                    f"line or a src: excerpt)."), None
        listed = "; ".join(f"{n}x in {rel}"
                           for rel, n in sorted(counts.items())[:10])
        return (f'"{find}" occurs {sum(counts.values())} time(s): {listed}\n'
                + "\n".join(windows)), None
    return handler


def norm_ws(s: str) -> str:
    """Collapse whitespace for a one-line context window."""
    return re.sub(r"\s+", " ", s)


def _set_cache_breakpoints(messages: list):
    """One fixed breakpoint on the initial user prompt (system+tools+prompt is
    the stable prefix every iteration re-sends) and one rolling breakpoint on
    the newest message, moved forward each turn. Everything else is stripped
    so the 4-breakpoint budget is never exceeded."""
    for i, m in enumerate(messages):
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and i > 0:
                    b.pop("cache_control", None)
    last = messages[-1]
    if len(messages) > 1 and isinstance(last.get("content"), list) \
            and last["content"] and isinstance(last["content"][-1], dict):
        last["content"][-1]["cache_control"] = {"type": "ephemeral"}


def _describe_tool_call(name, inp):
    """One human-readable line per tool call, for the progress stream.

    The author watching the catalog wants "reading step-004/page.html", not a
    tool name and a JSON blob. Unknown tools fall through to their raw name so
    a newly added tool is still legible before anyone writes a phrasing for it.
    """
    page = str(inp.get("page") or "")
    return {
        "validate_walkthrough": "checking the walkthrough so far",
        "read_page_html": f"reading {page or 'a page'}"
                          + (f" for {inp['query']!r}" if inp.get("query") else ""),
        "view_screenshot": f"looking at the screenshot of {page or 'a page'}",
        "verify_capture_edit": f"counting matches for {str(inp.get('find', ''))[:60]!r}",
        "probe_target": ("probing "
                         + str(inp.get("ref") or inp.get("selector") or "a target")[:60]
                         + f" in the player on {page or 'a page'}"),
        "submit_walkthrough": "submitting the finished walkthrough",
        "submit_edit": "submitting the edit",
        "answer_author": "writing the answer",
    }.get(name, str(name))


def call_claude_agentic(system: str, user: str, model: str, tools: list,
                        tool_handlers: dict, *, effort: str,
                        max_tokens: int = AGENTIC_MAX_TOKENS, progress=None):
    """Tool loop over the same request seam as call_claude.

    Each iteration is one model request. Tool calls are executed via
    `tool_handlers` ({name: fn(input) -> (result_text, final_or_None)}) and
    their results sent back; the loop returns the first non-None `final` a
    handler produces (the submit path). A turn that ends with no tool call
    gets one corrective nudge — models occasionally narrate instead of
    calling — then fails. Loop exhaustion, refusal and truncation raise
    AuthorError like the single-shot path. LOOP_WARNING_TURNS_LEFT turns
    before the end, the model is told to submit (see _turns_left_warning).

    `progress(phase, message)` is called at the loop's own boundaries — before
    each request and after each tool result. It is the only cancellation point
    the loop has: the caller's callback may raise (jobs.Job.progress raises
    Cancelled), which unwinds the run *between* requests rather than during
    one. A request already in flight therefore still completes and is still
    billed; nothing here can prevent that, and pretending otherwise would mean
    killing a thread mid-write. Defaults to a no-op so the CLI path is
    unchanged.
    """
    progress = progress or (lambda *_a, **_k: None)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthorError("ANTHROPIC_API_KEY is not set, and AI authoring and edits "
                          "call the Anthropic API. " + envfile.missing_key_hint())
    preflight_prompt_size(system, _content_text(user), model)
    fallbacks_attached = supports_fallbacks(model)
    # `user` is a plain string or a prepared content-block list (which may
    # already carry a cache marker on its stable prefix, e.g. the edit pass's
    # digest block). The last block gets a marker either way: the whole
    # initial prompt is a stable prefix within this loop, re-sent every
    # iteration.
    content = ([dict(b) for b in user] if isinstance(user, list)
               else [{"type": "text", "text": user}])
    content[-1]["cache_control"] = {"type": "ephemeral"}
    messages = [{"role": "user", "content": content}]
    nudged = False
    called = {}
    for iteration in range(1, MAX_LOOP_ITERATIONS + 1):
        progress("thinking", f"turn {iteration} of {MAX_LOOP_ITERATIONS}")
        _set_cache_breakpoints(messages)
        params = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
        }
        if fallbacks_attached:
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"

        with langsmith_trace.span(
            "anthropic.messages", run_type="llm",
            inputs=langsmith_trace.chat_inputs(system, f"[iteration {iteration}]"),
            metadata={"ls_provider": "anthropic", "ls_model_name": model,
                      "max_tokens": max_tokens, "effort": effort,
                      "iteration": iteration},
        ) as sp:
            data = _request(params)
            usage = data.get("usage") or {}
            print(f"[author] {model} agentic#{iteration} effort={effort} "
                  f"in={usage.get('input_tokens', '?')} "
                  f"out={usage.get('output_tokens', '?')}/{max_tokens} "
                  f"cache_read={usage.get('cache_read_input_tokens') or 0} "
                  f"cache_write={usage.get('cache_creation_input_tokens') or 0}")
            sp.add_metadata({"stop_reason": data.get("stop_reason")})
            if data.get("stop_reason") == "max_tokens":
                raise LoopTruncated(
                    f"Model response was truncated mid-loop: it hit max_tokens "
                    f"({usage.get('output_tokens', '?')}/{max_tokens}). The "
                    f"budget covers thinking AND the response — try lowering "
                    f"the effort for this call (see EFFORT / AUTHOR_EFFORT, "
                    f"this call ran {effort!r}) before raising max_tokens.")
            if data.get("stop_reason") == "refusal":
                detail = (data.get("stop_details") or {}).get("category")
                raise AuthorError(
                    "The model declined this request"
                    + (f" ({detail})" if detail else "")
                    + ". The prompt carries the captured pages verbatim, so "
                      "something in the capture tripped a safety classifier "
                      "— recapture without it, or set CAPTCHER_MODEL to "
                      "another model."
                    + (" (A fallback model was also tried and declined too.)"
                       if fallbacks_attached else ""))
            content = data.get("content") or []
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text")
            sp.set_outputs(langsmith_trace.chat_outputs(
                text or f"[{sum(1 for b in content if b.get('type') == 'tool_use')} tool call(s)]",
                usage))
            # A turn the fallback model served carries ITS thinking blocks,
            # and those go back verbatim on the next request. Thinking blocks
            # are tied to the model that wrote them, so the rest of the loop
            # stays on the model that actually answered rather than handing
            # the primary a transcript it may refuse to read.
            served = data.get("model")
            if _served_by_fallback(data) and served and served != model:
                print(f"[author] agentic#{iteration} served by fallback model "
                      f"{served} (primary declined) — continuing on it")
                sp.add_metadata({"served_by_fallback": served})
                model = served
                fallbacks_attached = supports_fallbacks(model)

        # The assistant turn goes back verbatim — thinking blocks included,
        # the API requires them unchanged on the next request.
        messages.append({"role": "assistant", "content": content})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            if nudged:
                raise AuthorError(
                    "The model ended its turn twice without submitting — "
                    "re-run the pass. Last reply started: " + text[:200])
            nudged = True
            # The edit loop can also end by answering, and a model that
            # narrated an answer must be pointed at that tool, not told to
            # invent an edit.
            answerable = any(t.get("name") == ANSWER_TOOL["name"] for t in tools)
            messages.append({"role": "user", "content": [{
                "type": "text",
                "text": "Do not return the result as text — call the submit "
                        "tool with the finished walkthrough to complete the "
                        "run" + (", or answer_author if the author asked a "
                                 "question" if answerable else "") + "."}]})
            continue

        results, final = [], None
        for tu in tool_uses:
            name = tu.get("name")
            called[name] = called.get(name, 0) + 1
            progress("tool", _describe_tool_call(name, tu.get("input") or {}))
            handler = tool_handlers.get(name)
            if handler is None:
                result_text = f"Unknown tool {name!r}."
            else:
                result_text, tool_final = handler(tu.get("input") or {})
                if tool_final is not None and final is None:
                    final = tool_final
            results.append({"type": "tool_result",
                            "tool_use_id": tu.get("id"),
                            "content": result_text})
        if final is not None:
            return final
        if MAX_LOOP_ITERATIONS - iteration == LOOP_WARNING_TURNS_LEFT:
            results.append({"type": "text",
                            "text": _turns_left_warning(tools)})
        messages.append({"role": "user", "content": results})

    # The caller decides what an exhausted loop means: both passes try to
    # recover a candidate that already validated before giving up.
    raise LoopExhausted(
        f"The loop did not produce an accepted submission in "
        f"{MAX_LOOP_ITERATIONS} turns.", tools_called=called)


# The model is not told its turn budget up front, and a run that explores
# until the turns run out ends with nothing to salvage. Warning it this many
# turns before the end leaves one turn to submit and one to repair what the
# submission's validation sends back.
LOOP_WARNING_TURNS_LEFT = 2


def _turns_left_warning(tools):
    names = [t.get("name") for t in tools]
    submit = next((n for n in names
                   if n in (SUBMIT_WALKTHROUGH_TOOL["name"], SUBMIT_EDIT_TOOL["name"])),
                  "the submit tool")
    text = (f"You have {LOOP_WARNING_TURNS_LEFT} turns left, and the run ends "
            f"with nothing saved if it has not submitted by then. Stop "
            f"exploring and call {submit} now with your best complete result; "
            f"if it comes back with errors you will have one turn to repair "
            f"them.")
    if ANSWER_TOOL["name"] in names:
        text += (" If the instruction cannot be carried out with the captured "
                 "pages, call answer_author instead and tell the author why.")
    return text




# --------------------------------------------------------------- validation

VALID_ACTIONS = {"click", "select", "input", "drag", "observe"}


# Show-me cursor shapes. Must match the `br-pointer` handling in
# static/bridge.js and the <select> in static/player.html; "dot" is the brand
# default and what an absent field means.
CURSOR_STYLES = {"dot", "pointer"}


def validate(walkthrough: dict, walkthrough_id: str, valid_pages: set, page_html: dict = None,
             trees: dict = None, alias_map: dict = None, render_notes: dict = None):
    """Returns (errors, warnings). Errors block the write; warnings surface
    to the author for Stage 4 attention. page_html optionally overrides
    what is stored (capture-relative path -> html), for validating pending
    capture edits.

    trees (canonical rel path -> digest.Tree, with alias_map folding duplicate
    paths onto canonical ones) enables real selector resolution: a target
    whose selector parses in the digest grammar is resolved against the page
    tree and warned about when it matches zero or several elements. A warning,
    never an error — the matcher is a subset engine, and a selector it cannot
    evaluate (full CSS from an older walkthrough or a hand edit) gets no
    verdict at all rather than a guess.

    render_notes is the module function of that name's map, when a
    render-accuracy check has been run: a page it judged unusable earns one
    warning naming the steps that play there. Absent (the default) simply
    produces no such warnings."""
    errors, warnings = [], []

    # `capture_dir` is not in this list any more. Requiring it would fail every
    # walkthrough ingest.convert() produces — it no longer emits one, because
    # the capture prefix is the walkthrough's id.
    for field in ("schema", "id", "title", "track", "level", "estimated_minutes",
                  "intro", "steps", "completion"):
        if field not in walkthrough:
            errors.append(f"missing top-level field: {field}")
    if "TODO" in json.dumps(walkthrough):
        errors.append("output still contains TODO markers")
    # Optional; absent means the brand default. Pinned to the shapes bridge.js
    # can actually draw, so a typo cannot reach a saved walkthrough.
    cursor = walkthrough.get("cursor")
    if cursor is not None and cursor not in CURSOR_STYLES:
        errors.append(f"cursor must be one of {sorted(CURSOR_STYLES)}, got {cursor!r}")
    if "_source" in walkthrough:
        errors.append("_source block was not removed")
    if not isinstance(walkthrough.get("steps"), list) or not walkthrough.get("steps"):
        errors.append("steps is empty")
        return errors, warnings
    intro = walkthrough.get("intro") or {}
    if not intro.get("scenario") or not intro.get("objectives"):
        errors.append("intro.scenario / intro.objectives missing or empty")
    completion = walkthrough.get("completion") or {}
    if not completion.get("summary") or not completion.get("success_criteria"):
        errors.append("completion.summary / success_criteria missing or empty")

    # optional capture window: playback is framed to this region of the page
    win = walkthrough.get("window")
    if win is not None:
        if not isinstance(win, dict):
            errors.append("window must be an object")
        elif not ((win.get("target") or {}).get("selector") or win.get("rect")):
            errors.append("window needs target.selector or a rect fallback")

    page_text_cache = {}
    # capture-relative .html paths, listed at most once and only if asked for
    capture_html, listed = set(), {"done": False}

    def text_of(page_rel):
        """The page's visible text: from the caller's in-memory copy when it
        has one, else read back from the capture.

        The fallback is a Storage read, cached for the duration of this call
        only — and both passes call validate on every tool turn. Handing in the
        pages the pass already holds would avoid it, but `page_html` is not a
        cache: it means "these pages have pending capture edits", and it
        suppresses the selector resolution below. Paying for the read is the
        cheaper mistake."""
        if page_rel not in page_text_cache:
            if page_html and page_rel in page_html:
                raw = page_html[page_rel]
            else:
                raw = store.capture_text(walkthrough_id, page_rel) or ""
            page_text_cache[page_rel] = visible_text(raw)
        return page_text_cache[page_rel]

    def is_capture_page(page_rel):
        """Whether a path names a real .html object in this capture.

        One listing rather than an existence probe per page: a probe was a stat
        and is now a round trip. It also replaces the containment check that
        used to guard this — `paths.is_within` against a resolved capture root.
        There is no root to resolve, and the rule about what a capture-relative
        path may be lives in `blobs.key_for`, which `store.capture_files` is on
        the other side of. A second copy here would be the drifting duplicate
        the store rewrite exists to remove."""
        if not listed["done"]:
            listed["done"] = True
            capture_html.update(store.capture_files(walkthrough_id, ".html"))
        return page_rel in capture_html

    current_page = None
    seen_ids = set()
    played_on = {}          # page rel -> step labels, for the render-check warning
    for i, step in enumerate(walkthrough["steps"], 1):
        label = f"step {i} ({step.get('id', '?')})"
        sid = step.get("id")
        if not sid or sid in seen_ids:
            errors.append(f"{label}: missing or duplicate id")
        seen_ids.add(sid)

        page = step.get("page")
        if page is not None:
            if page in valid_pages:
                current_page = page
            else:
                # not a top-level snapshot — accept any real capture page
                # (older walkthroughs legitimately use auto/ snapshots)
                if is_capture_page(page):
                    current_page = page
                else:
                    errors.append(f"{label}: page \"{page}\" is not a captured snapshot")
        elif current_page is None:
            errors.append(f"{label}: page is null but no previous step set a page")
        if current_page is not None:
            played_on.setdefault(current_page, []).append(label)

        # A run of merged steps plays with no pause between its members, so the
        # player treats it as one beat. Both shapes below would break that, and
        # they are errors rather than warnings for the reason the cursor style
        # is: a warning still writes the file, and the result would be a
        # walkthrough whose counter and pacing disagree with its steps.
        if step.get("continues"):
            if i == 1:
                errors.append(f"{label}: continues is set on the first step, "
                              f"which has nothing to run on from")
            if page is not None:
                errors.append(f"{label}: continues is set on a step that loads "
                              f"\"{page}\" — a merged run cannot span two snapshots")

        action = step.get("action") or {}
        a_type = action.get("type")
        if a_type not in VALID_ACTIONS:
            errors.append(f"{label}: invalid action type {a_type!r}")
            continue
        if a_type == "observe":
            continue
        target = action.get("target") or {}
        if not target.get("selector"):
            errors.append(f"{label}: action has no target selector")
        if a_type == "select" and not action.get("value"):
            errors.append(f"{label}: select action has no value")
        if a_type == "input" and not str(action.get("demo_value") or "").strip():
            # blank or whitespace: nothing a learner could type would ever
            # satisfy the step in You-try
            errors.append(
                f"{label}: input action has no demo_value — an input step must type "
                "something. To teach submitting with a field left (or cleared) empty, "
                "drop this input step and say so in the submit step's instruction")
        ends = [("target", target)]
        if a_type == "drag":
            drop = action.get("drop_target") or {}
            if not drop.get("selector"):
                errors.append(f"{label}: drag action has no drop_target selector")
            ends.append(("drop_target", drop))

        for field in ("instruction", "showme_caption", "hint"):
            if not step.get(field):
                warnings.append(f"{label}: {field} is empty")

        for what, end in ends:
            # cheap hallucination check: does the target text exist on the page?
            needle = end.get("text") or end.get("text_exact")
            if needle and current_page:
                if needle not in text_of(current_page):
                    warnings.append(f"{label}: {what} text \"{needle}\" not found in "
                                    f"{current_page} — verify in the player")

            # real resolution when a parse tree is on hand. Skipped for pages
            # with pending capture edits (page_html) — the tree predates the
            # edit and a verdict against the old markup could be wrong in
            # either direction.
            if trees and current_page and end.get("selector") \
                    and not (page_html and current_page in page_html):
                canonical = (alias_map or {}).get(current_page, current_page)
                tree = trees.get(canonical)
                if tree is not None:
                    hits = digest.match(tree, end)
                    if hits is not None and len(hits) != 1:
                        if not hits:
                            warnings.append(f"{label}: {what} matches nothing on "
                                            f"{current_page} — verify in the player")
                        else:
                            warnings.append(f"{label}: {what} matches {len(hits)} "
                                            f"elements on {current_page} — the player "
                                            f"will take the first; scope it if that is "
                                            f"the wrong one")

    # One warning per unusable page, not per step on it: a twelve-step form on
    # one broken snapshot would otherwise push every other warning off the list
    # with twelve copies of the same sentence. Reported even for a page with
    # pending capture edits, unlike the selector resolution above — a verdict is
    # about styling and images, which a literal find/replace neither breaks nor
    # repairs.
    for page, labels in played_on.items():
        note = (render_notes or {}).get(page)
        if not note or note["severity"] not in RENDER_WARN_SEVERITIES:
            continue
        detail = "; ".join(note["differences"]) or "it does not match the capture screenshot"
        shown = ", ".join(labels[:4]) + (", …" if len(labels) > 4 else "")
        warnings.append(f"{page}: the render check found this page {note['severity']} "
                        f"({detail}) — {len(labels)} step(s) play on it ({shown}); "
                        f"check them in the player, and consider re-capturing the page")

    return errors, warnings


# -------------------------------------------------------------------- pass

def run_pass(walkthrough_id: str, scenario: str = None, model: str = None,
             progress=None) -> dict:
    model = model or _model_from_env()
    scenario = (scenario or "").strip()
    progress = progress or (lambda *_a, **_k: None)

    row = store.get(walkthrough_id)
    if row is None:
        raise AuthorError(f"No walkthrough named {walkthrough_id!r}.")
    draft = row.get("definition") or {}
    # Already authored is the `authored_at` column's question now. It used to be
    # "walkthrough.json still contains the string TODO", which cannot tell one of
    # ingest's placeholders from an author legitimately writing the word — the
    # same misreading that stopped verify_pass running at all on a walkthrough
    # about a ticket queue. See the note above store.authoring_state.
    if store.authoring_state(row):
        raise AuthorError(f"{walkthrough_id!r} has already been through the "
                          f"authoring pass. Reset it to its draft (the card "
                          f"menu's Reset to draft) to run the pass again.")

    pages = snapshot_pages(walkthrough_id)
    if not pages:
        # No capture directory to be missing any more: an empty prefix and a
        # prefix holding no page.html are the same condition, and they were
        # always the same answer to the person who asked for a pass.
        raise AuthorError(f"No captured pages found for {walkthrough_id!r}.")

    # The trace starts here rather than at the top of the function: everything
    # above is a precondition check that fails without ever reaching the model,
    # and a trace of "no such walkthrough" is noise with no LLM child under it.
    with langsmith_trace.span(
        "authoring pass", inputs={"walkthrough_id": walkthrough_id, "scenario": scenario},
        metadata={"model": model, "pages": len(pages),
                  "draft_steps": len(draft.get("steps", []))},
        tags=["authoring"],
    ) as sp:
        progress("digest", f"digesting {len(pages)} captured page(s)")
        prompt_draft = dict(draft)
        prompt_draft.pop("owner", None)  # internal account metadata is not model input
        blocks, ref_table, alias_map, trees = build_page_digests(pages)
        # blocks are one per unique page, in unique_pages order — the rel list
        # aligns each block with its snapshot folder's screenshot
        unique_rels = [rel for rel, _ in unique_pages(pages)[0]]
        valid_pages = {rel for rel, _ in pages}
        # A render check exists here only when the author opted into the
        # pre-check (or is re-authoring after a review) — the check stays on
        # demand because it costs a vision call per unique page. When it does
        # exist it feeds BOTH the validation warnings and the prompt, same as
        # the edit pass. It costs nothing to look: the verdicts are a column on
        # the row this function is already holding.
        #
        # `store.load_render_check(id, walkthrough_dir)` took a directory here
        # because author.py resolved walkthroughs from its own ROOT and store
        # resolved them from its own, and the two had to be told they meant the
        # same walkthrough. Neither resolves anything now — there is one row and
        # one reader of it — so the parameter is gone along with its reason.
        render_results = store.load_render_check(row)
        rnotes = render_notes(render_results)
        note_block = render_notes_block(rnotes,
                                        (render_results or {}).get("checked_at"))

        def _check(candidate):
            """Resolve, enforce invariants, and validate a model-produced
            walkthrough — the exact checks the save runs, on a copy. Shared
            between the single-shot path and the agentic tools so the model
            is judged by the same rules either way."""
            wt = copy.deepcopy(candidate)
            errors = resolve_refs(wt, ref_table, alias_map)
            _force_invariants(wt, draft, walkthrough_id)
            verrors, warnings = validate(wt, walkthrough_id, valid_pages,
                                         trees=trees, alias_map=alias_map,
                                         render_notes=rnotes)
            return wt, errors + verrors, warnings

        print(f"[author] {walkthrough_id}: calling {model} "
              f"({len(pages)} pages, {len(blocks)} unique, "
              f"{len(draft.get('steps', []))} draft steps)…")
        def _closer(text):
            # The render notes and the observed-behaviour block annotate the
            # digests, so they sit right after them, ahead of the ask — same
            # placement as the edit pass.
            effects_block = observed_effects_block(walkthrough_id, ref_table,
                                                   alias_map, trees)
            for extra in (note_block, effects_block):
                if extra:
                    text = extra + "\n\n" + text
            return text

        system = SYSTEM_PROMPT + example_walkthrough_block(draft.get("owner"))
        user = build_user_content(
            walkthrough_id, scenario, prompt_draft, blocks, unique_rels,
            note_overflow=True,
            closer=_closer("Now author the walkthrough and submit it with "
                           "submit_walkthrough."))

        # The best candidate the model validated clean but never submitted.
        # See the LoopExhausted handler below for why this is kept.
        salvage = {"clean": None, "errors": []}
        review = {"done": False}   # see WARNINGS_REVIEW_NOTE

        def handle_validate(inp):
            wt = inp.get("walkthrough")
            if not isinstance(wt, dict):
                return json.dumps({"errors": ["`walkthrough` must be the "
                                              "complete walkthrough JSON "
                                              "object"],
                                   "warnings": []}), None
            resolved, errors, warnings = _check(wt)
            progress("validated",
                     "no errors" if not errors
                     else f"{len(errors)} error(s) to repair")
            salvage["errors"] = errors
            if not errors:
                salvage["clean"] = (resolved, warnings)
            return json.dumps({"errors": errors, "warnings": warnings},
                              ensure_ascii=False), None

        def handle_submit(inp):
            wt = inp.get("walkthrough")
            if not isinstance(wt, dict):
                return json.dumps({"accepted": False,
                                   "errors": ["`walkthrough` must be the "
                                              "complete walkthrough JSON "
                                              "object"]}), None
            resolved, errors, warnings = _check(wt)
            if errors:
                progress("rejected",
                         f"submission rejected — {len(errors)} error(s) to repair")
                salvage["errors"] = errors
                return json.dumps({"accepted": False, "errors": errors,
                                   "warnings": warnings},
                                  ensure_ascii=False), None
            reviewable = _reviewable_warnings(warnings)
            if reviewable and not review["done"]:
                review["done"] = True
                salvage["clean"] = (resolved, warnings)
                progress("review",
                         f"{len(reviewable)} warning(s) handed back for review")
                return json.dumps({"accepted": False, "errors": [],
                                   "warnings": reviewable,
                                   "note": WARNINGS_REVIEW_NOTE},
                                  ensure_ascii=False), None
            progress("accepted", "submission accepted")
            return json.dumps({"accepted": True}), (resolved, warnings)

        try:
            walkthrough, warnings = call_claude_agentic(
                system, user, model,
                tools=[VALIDATE_TOOL, READ_PAGE_TOOL, VIEW_SCREENSHOT_TOOL,
                       SUBMIT_WALKTHROUGH_TOOL],
                tool_handlers={
                    "validate_walkthrough": handle_validate,
                    "read_page_html": _read_page_tool_handler(pages, alias_map),
                    "view_screenshot": _view_screenshot_handler(
                        walkthrough_id, alias_map, valid_pages),
                    "submit_walkthrough": handle_submit,
                },
                effort=AUTHOR_EFFORT, progress=progress)
        except LoopExhausted as e:
            walkthrough, warnings = _salvage_or_raise(salvage, e, "authoring pass")
            progress("salvaged", "recovered a validated walkthrough")

        # The pre-authoring text, kept before it is overwritten. `store.create`
        # already seeds the column from the same definition, and save_draft is
        # write-once, so this is normally a no-op — deliberately: when both
        # write, ingest's draft is the one that survives, which is the right
        # precedence. It stays because preserving the draft is this pass's own
        # guarantee, not something to inherit from whoever made the row.
        store.save_draft(walkthrough_id, draft)
        # The authored definition goes in through the same path as an editor
        # save, so the draft becomes a history entry as well as a column: a
        # re-run is a restore away, where it used to need a file copied back by
        # hand. It costs one history row per pass.
        stamp = store.save_with_history(walkthrough_id, walkthrough)

        result = {
            "walkthrough_id": walkthrough_id,
            "title": walkthrough.get("title"),
            "steps_before": len(draft.get("steps", [])),
            "steps_after": len(walkthrough["steps"]),
            "warnings": warnings,
            "stamp": stamp,
        }
        sp.set_outputs(result)
        print(f"[author] {walkthrough_id}: {result['steps_before']} → {result['steps_after']} steps, "
              f"{len(warnings)} warning(s)")
        return result


def apply_capture_edits(walkthrough_id: str, ops, alias_map: dict = None):
    """Apply literal find/replace ops to the capture's .html pages.
    Returns (changed: {capture-relative path: new_text}, warnings). Nothing is
    written — that property is load-bearing and survives the move: the result
    is what `store.save_with_history(id, definition, changed_pages=…)` takes,
    and the pages are rewritten there, after the definition is committed. An
    edit that fails validation must leave the capture untouched.

    No ops means no reads. That is not an optimisation for its own sake: the
    validate tool calls straight through here with an empty list on every turn
    of the edit loop, and this used to answer it by re-reading every page — a
    handful of local reads then, a full re-download of the capture now.

    An op may carry a `page` (a snapshot rel path) restricting it to that page
    — plus its byte-identical duplicates, in either direction through
    alias_map, because the digests present those paths as interchangeable and
    a step may reference any of them. An op without a `page` keeps the
    original semantics: every file.

    An unscoped op that lands in more than one file is reported as a warning
    with per-file counts. That is precisely the op most likely to be an
    over-edit — the model aimed at one page and hit three — and it used to be
    silent: the only warning was for ZERO hits, so the one failure mode that
    rewrites pages the author never looked at produced no signal at all."""
    warnings = []
    if not ops:
        return {}, warnings
    original = {}
    for rel in store.capture_files(walkthrough_id, ".html"):
        text = store.capture_text(walkthrough_id, rel)
        if text is not None:
            original[rel] = text
    files = sorted(original)
    contents = dict(original)

    def scope_group(page):
        """The page and everything byte-identical to it."""
        amap = alias_map or {}
        canon = amap.get(page, page)
        return {canon} | {dup for dup, c in amap.items() if c == canon}

    for op in ops or []:
        find = str(op.get("find", ""))
        repl = str(op.get("replace", ""))
        page = str(op.get("page") or "") or None
        if not find:
            continue
        targets = files
        scope_note = ""
        if page:
            group = scope_group(page)
            targets = [rel for rel in files if rel in group]
            scope_note = f' (scoped to {page})'
            if not targets:
                warnings.append(f'page edit "{find}" is scoped to "{page}", '
                                f'which is not a captured snapshot — nothing applied')
                continue
        per_file = {}
        for rel in targets:
            n = contents[rel].count(find)
            if n:
                contents[rel] = contents[rel].replace(find, repl)
                per_file[rel] = n
        if not per_file:
            warnings.append(f'page edit "{find}" matched nothing in the '
                            f'captured HTML{scope_note}')
        elif page is None and len(per_file) > 1:
            listed = ", ".join(f"{n}x in {rel}"
                               for rel, n in sorted(per_file.items()))
            warnings.append(f'page edit "{find}" applied across '
                            f'{len(per_file)} files ({listed}) — scope it '
                            f'with "page" if only one was intended')
    changed = {rel: t for rel, t in contents.items() if t != original[rel]}
    return changed, warnings


def find_selection_ref(sel: dict, ref_table: dict, alias_map: dict, trees: dict):
    """Which digest ref (if any) is the element the author pointed at.

    The editor's selector is browser-verified but written in bridge.js's own
    style; when the digest matcher can resolve it to exactly one node and that
    node is in the inventory, the annotation lets the model target it by ref
    like everything else. Returns (ref, node): ref None when the element is
    not inventoried, node None when the selector cannot be resolved at all —
    the verbatim-selector path still works either way, but a resolved node
    carries its raw start tag, which is the one legitimate capture_edits
    anchor for an element the digests do not name."""
    page = sel.get("page")
    if not page:
        return None, None
    tree = trees.get((alias_map or {}).get(page, page))
    if tree is None:
        return None, None
    target = {"selector": sel.get("selector")}
    if sel.get("text_exact"):
        target["text_exact"] = sel["text_exact"]
    elif sel.get("text"):
        target["text"] = sel["text"]
    hits = digest.match(tree, target)
    if not hits or len(hits) != 1:
        return None, None
    node = hits[0]
    for ref, entry in ref_table.items():
        if entry.get("node") is node:
            return ref, node
    return None, node


def describe_selection(sel: dict, ref: str = None, node=None,
                       display: bool = False) -> str:
    """Name the element the author pointed at in the editor.

    The selector arrives already verified: bridge.js scoped it with ancestors
    until it matched exactly one node in the live DOM. When the same element
    was found in the page digest, `ref` names its inventory line so the
    model's "this" and the digest connect — and the model can target it the
    normal way, by ref (or, for a display ref, anchor a capture edit on it).
    When the element resolved to a node but is in NO inventory, its raw start
    tag from the file is included: the one safe find-anchor for an element the
    digests do not name — without it, "this" was something the model could
    see but never touch.
    """
    lines = ['The author has POINTED AT one element on the page. When the '
             'instruction says "this", "this element", "the selected element", '
             'or names it loosely ("this button", "that field"), it means this '
             'one and no other:']
    if sel.get("page"):
        lines.append(f'  page: {sel["page"]}')
    lines.append(f'  selector: {sel["selector"]}'
                 + ('  (verified to match exactly one element on that page)'
                    if sel.get("matches") == 1 else ''))
    if sel.get("tag"):
        lines.append(f'  tag: <{sel["tag"]}>')
    if sel.get("text"):
        lines.append(f'  visible text: {sel["text"]}')
    if sel.get("text_exact"):
        lines.append(f'  exact text: {sel["text_exact"]}')
    if sel.get("matches") is not None and sel["matches"] != 1:
        lines.append(f'  WARNING: that selector matched {sel["matches"]} elements. '
                     'Scope it further before using it as a step target.')
    if sel.get("html"):
        # outerHTML is a serialisation of the parsed DOM: attribute order and
        # quoting are the browser's, entities may be resolved, and implied tags
        # appear. It identifies the element; it is NOT a substring of the file,
        # so it must never be copied into a literal find/replace.
        lines.append('  as the browser rendered it (attribute order and quoting are '
                     'the DOM\'s, not the file\'s — identify the element by this, but '
                     'copy `find` strings for capture_edits from the snapshot HTML '
                     f'below, never from here):\n    {sel["html"]}')
    if ref and display:
        lines.append(f'  in the page digest this element is {ref} — a DISPLAY '
                     f'element: it shows a value and cannot be a step target. '
                     f'To change what it shows, anchor a capture_edits find on '
                     f'its src: excerpt or its visible text.')
    elif ref:
        lines.append(f'  in the page digest this element is {ref} — to target it, '
                     f'write {{"ref": "{ref}"}}')
    elif node is not None and getattr(node, "src", None):
        lines.append('  it is in no digest inventory, so its start tag from the '
                     'snapshot file is given here — contiguous runs of this ARE '
                     'safe capture_edits find anchors (unlike the DOM '
                     'serialisation above):\n    '
                     + digest._src_excerpt(node.src))
    lines.append('Being pointed at does not by itself mean the walkthrough should '
                 'target this element — do what the instruction asks, with it.')
    return "\n".join(lines)


MAX_SWEEP_STEPS = 12            # post-edit probes are seconds each — bound them


def _post_edit_probe_warnings(replay_ctx, walkthrough_id, before: dict,
                              after: dict, changed_rels: set):
    """Probe the steps an edit touched; return author-facing warnings.

    Touched = the step's target differs from the same-id step before the
    edit, the step is new, or it plays on a page the edit rewrote. Probe
    counts are mechanical (no vision): 1 is silence, 0 or no-answer is a
    warning naming the step — the author's 'check these in the player' list,
    delivered with the edit instead of discovered later."""
    from . import verify_pass as _vp
    old_targets = {}
    for s in before.get("steps") or []:
        if isinstance(s, dict):
            old_targets[s.get("id")] = (s.get("action") or {}).get("target")

    def touched(sid, step, effective_page):
        target = (step.get("action") or {}).get("target")
        return (sid not in old_targets or old_targets[sid] != target
                or effective_page in changed_rels)

    jobs, kept = [], 0
    for page_rel, steps in _vp.step_jobs(after):
        picked = [(sid, s) for sid, s in steps if touched(sid, s, page_rel)]
        picked = picked[:max(0, MAX_SWEEP_STEPS - kept)]
        if picked:
            jobs.append((page_rel, picked))
            kept += len(picked)
    if not jobs:
        return []

    base_url, cookie_header = replay_ctx
    warnings = []
    with tempfile.TemporaryDirectory(prefix="edit-sweep-") as tmp:
        driven = _vp.drive_pages(base_url, cookie_header, walkthrough_id,
                                 jobs, Path(tmp))
    for page_rel, steps in jobs:
        for sid, _step in steps:
            count = (driven.get(sid) or {}).get("count", -1)
            if count == 0:
                warnings.append(f"replay check: step {sid!r} — its target "
                                f"matched NOTHING on {page_rel} in the "
                                f"player; fix it before a learner hits it")
            elif count < 0:
                warnings.append(f"replay check: step {sid!r} — {page_rel} "
                                f"did not answer the probe; open it in the "
                                f"player to confirm it still works")
            elif count > 1:
                warnings.append(f"replay check: step {sid!r} — its target "
                                f"matches {count} elements on {page_rel}; "
                                f"the player will take the first")
    return warnings


def run_edit(walkthrough_id: str, instruction: str, model: str = None,
             current_step: int = None, selection: dict = None,
             progress=None, replay_ctx=None, answerable: bool = True) -> dict:
    """Apply a natural-language edit instruction to a published walkthrough.

    The previous version is kept in the `walkthrough_history` table
    (store.history_entries / store.history_entry), along with copies of any
    capture pages the edit rewrote. It was a `history/` folder beside the
    walkthrough; the reason for the move is the same one the definition had —
    a row and its history row are written in one transaction, so a crash cannot
    leave a history entry pointing at a version that was never saved.

    replay_ctx, when given, is (base_url, cookie_header) — the running
    server's self-address, exactly what the verify pass drives Playwright
    with. It turns on two things: the probe_target tool inside the loop (the
    model can render a page in the real player and see its target ringed),
    and a mechanical post-save probe sweep over the steps the edit touched.
    Absent (CLI runs, tests), both are simply off.

    answerable offers the model `answer_author`, the exit for a message that
    asks a question rather than requesting a change. An answered run saves
    nothing and returns `kind: "answer"` with no stamp. The verify pass's
    repair turn turns it off: an instruction it wrote is never a question,
    and an answer there would be a repair that silently did nothing."""
    model = model or _model_from_env()
    progress = progress or (lambda *_a, **_k: None)
    instruction = (instruction or "").strip()
    if not instruction:
        raise AuthorError("An edit instruction is required — describe what should change.")

    row = store.get(walkthrough_id)
    if row is None:
        raise AuthorError(f"No walkthrough named {walkthrough_id!r}.")
    current = row.get("definition") or {}
    # Draft or authored is the `authored_at` column's question, not a substring
    # scan for "TODO" — see the note in run_pass and above store.authoring_state.
    if not store.authoring_state(row):
        raise AuthorError("This walkthrough is still a draft — run the authoring pass first, "
                          "then edit.")

    pages = snapshot_pages(walkthrough_id)
    if not pages:
        raise AuthorError(f"No captured pages found for {walkthrough_id!r}.")

    # The edit pass is where render verdicts actually pay off: unlike the
    # authoring pass it runs after a review, so a check has usually been run.
    # Off the row already in hand, which is one query rather than two.
    render_results = store.load_render_check(row)
    notes = render_notes(render_results)

    with langsmith_trace.span(
        "ai edit",
        inputs={"walkthrough_id": walkthrough_id, "instruction": instruction,
                "current_step": current_step, "selection": selection},
        metadata={"model": model, "pages": len(pages),
                  "steps_before": len(current.get("steps", [])),
                  "render_flagged_pages": len(notes)},
        tags=["edit"],
    ) as sp:
        blocks, ref_table, alias_map, trees = build_page_digests(pages)
        # Prompt order is stable-first for the cache: the digests are byte-
        # identical across repeated edits of one walkthrough (they change only
        # when a capture edit rewrites a page), so they lead the prompt under
        # a cache marker; everything that varies per edit — the current
        # walkthrough, render notes, the author's context and instruction —
        # follows. EDIT_SYSTEM_PROMPT describes this order to the model.
        # Observed behaviour rides with the digests under the cache marker:
        # the capture manifest never changes after upload, so it is as stable
        # as the pages themselves.
        effects_block = observed_effects_block(walkthrough_id, ref_table,
                                               alias_map, trees)
        digest_prefix = "\n\n".join([
            f"Walkthrough id: {walkthrough_id}",
            "--- CAPTURED PAGE DIGESTS (their paths are the only valid `page` values) ---",
            *blocks,
            *([effects_block] if effects_block else []),
        ])
        volatile = [
            "--- CURRENT walkthrough.json ---",
            json.dumps({k: v for k, v in current.items() if k != "owner"},
                       indent=2, ensure_ascii=False),
        ]
        # Right after the digests, because it annotates them: the pages are
        # what it talks about.
        note_block = render_notes_block(notes, (render_results or {}).get("checked_at"))
        if note_block:
            volatile.append(note_block)
        # What the author had on screen when they wrote the instruction. Both
        # resolve a "this" the words alone cannot, and both sit directly above
        # the instruction they qualify.
        steps = current.get("steps", [])
        if current_step is not None and 0 <= current_step < len(steps):
            s = steps[current_step]
            volatile.append(
                f'The author is currently viewing step {current_step + 1} of {len(steps)} '
                f'(id: {s.get("id")!r}, title: {s.get("title")!r}). '
                'When the instruction says "this step" or "the current step", '
                'it means that one.')
        if selection:
            sel_ref, sel_node = find_selection_ref(selection, ref_table,
                                                   alias_map, trees)
            volatile.append(describe_selection(
                selection, ref=sel_ref, node=sel_node,
                display=bool(sel_ref and ref_table.get(sel_ref, {}).get("display"))))
        volatile.append(f"EDIT INSTRUCTION from the author:\n{instruction}")

        valid_pages = {rel for rel, _ in pages}

        def edit_user_content(closer):
            return [
                {"type": "text", "text": digest_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "\n\n".join(volatile + [closer])},
            ]

        def _check_edit(candidate, ops):
            """Resolve refs, enforce invariants, compile structured ops,
            apply the capture edits in memory, and validate against the
            EDITED pages — the exact checks the save runs, on a copy. Shared
            between the single-shot path and the agentic submit tool."""
            wt = copy.deepcopy(candidate)
            ref_errors = resolve_refs(wt, ref_table, alias_map)
            _force_invariants(wt, current, walkthrough_id)
            literal_ops, op_errors = compile_structured_ops(ops, ref_table,
                                                            walkthrough_id)
            ref_errors = ref_errors + op_errors
            # `changed` is already keyed by capture-relative path, so the
            # Path-to-rel conversion that used to sit here is gone with the
            # Paths — and with it the chance of the two spellings of a page
            # disagreeing about whether it has a pending edit.
            changed, op_warnings = apply_capture_edits(walkthrough_id, literal_ops,
                                                       alias_map=alias_map)
            errors, warnings = validate(wt, walkthrough_id, valid_pages,
                                        page_html=changed, trees=trees,
                                        alias_map=alias_map, render_notes=notes)
            return wt, changed, ref_errors + errors, op_warnings + warnings

        print(f"[author] {walkthrough_id}: edit via {model}: {instruction[:80]}")
        user = edit_user_content("Now apply the instruction and submit "
                                 "the result with submit_edit.")
        system = EDIT_SYSTEM_PROMPT

        # As in run_pass: a candidate the model validated clean but never
        # submitted is worth more than nothing when the turns run out. The
        # edit's salvage carries no capture edits — those are only ever seen
        # by submit_edit, and applying half of an edit is worse than none.
        salvage = {"clean": None, "errors": []}
        review = {"done": False}   # see WARNINGS_REVIEW_NOTE

        def handle_validate(inp):
            wt = inp.get("walkthrough")
            if not isinstance(wt, dict):
                return json.dumps({"errors": ["`walkthrough` must be the "
                                              "complete walkthrough JSON "
                                              "object"],
                                   "warnings": []}), None
            resolved, changed, errors, warnings = _check_edit(wt, [])
            progress("validated",
                     "no errors" if not errors
                     else f"{len(errors)} error(s) to repair")
            salvage["errors"] = errors
            if not errors:
                salvage["clean"] = (resolved, changed, warnings,
                                    "Ran out of turns; saved the last version "
                                    "that validated cleanly.")
            return json.dumps({"errors": errors, "warnings": warnings},
                              ensure_ascii=False), None

        def handle_submit_edit(inp):
            wt = inp.get("walkthrough")
            if not isinstance(wt, dict):
                return json.dumps({"accepted": False,
                                   "errors": ["`walkthrough` must be the "
                                              "complete updated "
                                              "walkthrough JSON object"]}), None
            ops = inp.get("capture_edits") or []
            summary = str(inp.get("summary") or "").strip()
            resolved, changed, errors, warnings = _check_edit(wt, ops)
            if errors:
                progress("rejected",
                         f"submission rejected — {len(errors)} error(s) to repair")
                salvage["errors"] = errors
                return json.dumps({"accepted": False, "errors": errors,
                                   "warnings": warnings},
                                  ensure_ascii=False), None
            reviewable = _reviewable_warnings(warnings)
            if reviewable and not review["done"]:
                review["done"] = True
                salvage["clean"] = (resolved, changed, warnings, summary)
                progress("review",
                         f"{len(reviewable)} warning(s) handed back for review")
                return json.dumps({"accepted": False, "errors": [],
                                   "warnings": reviewable,
                                   "note": WARNINGS_REVIEW_NOTE},
                                  ensure_ascii=False), None
            progress("accepted", "submission accepted")
            return (json.dumps({"accepted": True}),
                    (resolved, changed, warnings, summary))

        def handle_answer(inp):
            answer = str(inp.get("answer") or "").strip()
            if not answer:
                return json.dumps({"accepted": False,
                                   "errors": ["`answer` is empty — write the "
                                              "answer for the author"]}), None
            suggested = str(inp.get("suggested_change") or "").strip()
            progress("answered", "answer ready")
            return (json.dumps({"accepted": True}),
                    {"answer": answer, "suggested_change": suggested})

        tools = [VALIDATE_TOOL, READ_PAGE_TOOL, VIEW_SCREENSHOT_TOOL,
                 VERIFY_CAPTURE_EDIT_TOOL, SUBMIT_EDIT_TOOL]
        tool_handlers = {
            "validate_walkthrough": handle_validate,
            "read_page_html": _read_page_tool_handler(pages, alias_map),
            "view_screenshot": _view_screenshot_handler(
                walkthrough_id, alias_map, valid_pages),
            "verify_capture_edit": _verify_capture_edit_handler(walkthrough_id),
            "submit_edit": handle_submit_edit,
        }
        if answerable:
            tools.append(ANSWER_TOOL)
            tool_handlers["answer_author"] = handle_answer
        # The probe tool only exists when a live server can serve the player:
        # a dead tool in the list teaches the model to route around tools.
        if replay_ctx:
            tools.insert(-1, PROBE_TOOL)
            tool_handlers["probe_target"] = _probe_tool_handler(
                replay_ctx, walkthrough_id, ref_table, alias_map, valid_pages)

        try:
            final = call_claude_agentic(
                system, user, model, tools=tools, tool_handlers=tool_handlers,
                effort=EDIT_EFFORT, progress=progress)
        except LoopExhausted as e:
            final = _salvage_or_raise(salvage, e, "edit")
            progress("salvaged", "recovered a validated walkthrough")
        if isinstance(final, dict):
            # Answered, not edited: nothing is written, so there is no stamp
            # and no post-save sweep — and no undo for the editor to offer.
            steps = len(current.get("steps", []))
            result = {"walkthrough_id": walkthrough_id, "kind": "answer",
                      "title": current.get("title"),
                      "answer": final["answer"],
                      "suggested_change": final["suggested_change"],
                      "steps_before": steps, "steps_after": steps,
                      "pages_changed": 0, "pages_changed_paths": [],
                      "warnings": [], "summary": "", "stamp": None}
            sp.set_outputs(result)
            print(f"[author] {walkthrough_id}: answered, nothing saved")
            return result
        walkthrough, changed, warnings, summary = final
        # The previous definition is no longer passed in: save_with_history
        # reads it inside its own transaction, under a row lock. The old
        # signature handed it the copy read at the top of this function, which
        # by now is minutes and one editor save old — and that stale copy was
        # what went into history.
        stamp = store.save_with_history(walkthrough_id, walkthrough,
                                        changed_pages=changed)

        # Post-save probe sweep: mechanically replay the steps this edit
        # touched and warn on any target the player cannot find. Runs AFTER
        # the save so the probes see the pages as edited. Advisory by
        # construction — the edit is already written, and a sweep failure
        # (no Playwright, a wedged browser) must never turn a saved edit
        # into an error.
        if replay_ctx:
            progress("verifying", "replaying the edited steps in the player")
            try:
                warnings = warnings + _post_edit_probe_warnings(
                    replay_ctx, walkthrough_id, current, walkthrough,
                    set(changed))
            except Exception as e:
                print(f"[author] {walkthrough_id}: post-edit probe sweep "
                      f"skipped ({e})")

        result = {
            "walkthrough_id": walkthrough_id,
            "kind": "edit",
            "title": walkthrough.get("title"),
            "steps_before": len(current.get("steps", [])),
            "steps_after": len(walkthrough["steps"]),
            "pages_changed": len(changed),
            # Which ones — diagnostics.record_ai_edit diffs each of them.
            "pages_changed_paths": sorted(changed),
            "warnings": warnings,
            "summary": summary,
            "stamp": stamp,
        }
        sp.set_outputs(result)
        print(f"[author] {walkthrough_id}: edited, {len(changed)} page(s) updated, "
              f"{len(warnings)} warning(s), previous version at {stamp}")
        return result


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "edit":
            r = run_edit(sys.argv[2], sys.argv[3])
        elif len(sys.argv) in (2, 3) and sys.argv[1] != "edit":
            r = run_pass(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
        else:
            print(__doc__)
            sys.exit(1)
    except AuthorError as e:
        sys.exit(f"error: {e}")
    print(json.dumps(r, indent=2, ensure_ascii=False))
    print("\nNext: Stage 4 — verify in the player (Show me + You try end-to-end).")
