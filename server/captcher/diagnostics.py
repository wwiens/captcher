"""Diagnostic logs kept with a walkthrough, for troubleshooting.

Two kinds of entry, both in the `walkthrough_logs` table and neither shown in
any UI yet:

  capture   The recorder's complete panel log for the capture a walkthrough was
            uploaded from. The panel shows the same lines but caps them at 800
            and loses them when it closes; this is the copy that survives.
  ai_edit   One entry per AI-assisted edit attempt — saved, answered, failed
            or cancelled. It carries the author's instruction exactly as sent,
            what they had on screen (current step, selection), and for a saved
            edit the exact diff of the definition and of every capture page
            the edit rewrote. An answered question carries the answer.
  author    One entry per AI authoring pass — authored, failed or cancelled:
            the step counts, every validator warning, the full error, and the
            pass's progress trail. The catalog card shows only whether the
            pass worked; this is where the detail went.

WHY A TABLE AND NOT THE CAPTURE PREFIX

Everything under a walkthrough's prefix is a capture file to every caller that
walks one: the exports, the SCORM packages, the authoring snapshot. A log in
there would ship inside a customer's LMS package. The table also cascades with
the walkthrough row, so a delete takes the logs with it for free.

WHERE THE EDIT DIFF COMES FROM

Not from anything `run_edit` holds in memory. `store.save_with_history` already
writes the pre-edit definition to `walkthrough_history` under the stamp it
returns, and copies every page it is about to overwrite into that entry's
`pages_prefix`. So "before" is the history row and its page backups, and
"after" is the live row and capture — read while the edit's job still holds the
walkthrough, so nothing else can have written in between. Both definitions have
been through jsonb, which reorders keys, so they are diffed with sorted keys.

Recording is best-effort by construction. A diagnostic that could not be
written must never fail the upload or the edit it describes.
"""
import difflib
import hashlib
import json
import re
import traceback
from datetime import datetime, timezone


from . import blobs
from . import db
from . import jobs
from . import store

KINDS = ("capture", "ai_edit", "author")

# A pass emits tens of progress events; jobs.MAX_EVENTS bounds the job's own
# log, and this bounds what is copied into a row.
MAX_TRAIL_EVENTS = 300

CAPTURE_LOG_SCHEMA = "captcher-capture-log@1"
# A long recording logs a few lines per step plus one per failed resource; the
# recorder caps its buffer well under this. The bound is what keeps a
# malformed or hostile field from becoming a multi-megabyte jsonb row.
MAX_CAPTURE_LOG_BYTES = 4 * 1024 * 1024

# Per page. A capture edit is normally a handful of attribute rewrites, so its
# diff is a few KB; one that rewrites most of a 4MB page is recorded up to
# here and marked truncated — the full before copy is still in the history
# prefix the entry names.
MAX_PAGE_DIFF_CHARS = 1_000_000
DIFF_CONTEXT = 3

# Captured pages are SingleFile output: a few enormous lines. Diffing by line
# would put whole megabytes on each side of every change, so pages are split
# after every tag and every newline, and each piece becomes a diff "line".
_PAGE_PIECES = re.compile(r"(?<=>)|(?<=\n)")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def record(walkthrough_id, member_id, kind, entry):
    """Insert one log entry. Raises; callers that must not fail use the
    `record_*` wrappers below."""
    if kind not in KINDS:
        raise ValueError(f"unknown log kind: {kind!r}")
    db.execute(
        """insert into walkthrough_logs (walkthrough_id, member_id, kind, entry)
           values (%s, %s, %s, %s)""",
        (walkthrough_id, member_id, kind, db.Json(entry)),
    )


def entries(walkthrough_id, kind=None):
    """Every entry for one walkthrough, oldest first. No owner filter here:
    the one route that serves these to a member (GET …/logs) checks that the
    walkthrough is theirs before it calls this."""
    return db.query(
        """select id, member_id, kind, entry, created_at
             from walkthrough_logs
            where walkthrough_id = %s and (%s is null or kind = %s)
         order by created_at, id""",
        (walkthrough_id, kind, kind),
    )


# ---- capture logs -----------------------------------------------------------

def parse_capture_log(raw):
    """The recorder's `capture_log` field as a dict, or ValueError.

    Checked for shape and size only. The lines themselves are kept exactly as
    the panel wrote them: a log tidied on the way in is not the log.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_CAPTURE_LOG_BYTES:
        raise ValueError(f"capture log is {len(raw)} bytes "
                         f"(max {MAX_CAPTURE_LOG_BYTES})")
    try:
        log = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"capture log is not JSON: {e}")
    if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
        raise ValueError("capture log has no `entries` list")
    return log


def record_capture_log(walkthrough_id, member_id, raw, recorder=None):
    """Store an uploaded capture log. Returns True when it was stored."""
    try:
        log = parse_capture_log(raw)
        if recorder:
            log.setdefault("recorder", str(recorder)[:64])
        log["received_at"] = _now_iso()
        record(walkthrough_id, member_id, "capture", log)
        return True
    except Exception as e:
        print(f"[warn] capture log for {walkthrough_id} not stored: {e}")
        return False


# ---- AI edit records ----------------------------------------------------------

def _canonical(definition):
    return json.dumps(definition or {}, indent=2, sort_keys=True,
                      ensure_ascii=False).splitlines()


def definition_diff(before, after):
    """Unified diff of two definitions, keys sorted (see the module note)."""
    return "\n".join(difflib.unified_diff(
        _canonical(before), _canonical(after),
        "definition (before)", "definition (after)",
        n=DIFF_CONTEXT, lineterm=""))


def _pieces(html):
    # Each piece loses its trailing newline so the diff output stays one piece
    # per line; the sha256 pair in the entry is what pins the exact bytes.
    return [p.rstrip("\n") for p in _PAGE_PIECES.split(html or "") if p]


def page_diff(before, after, rel):
    """(diff text, truncated) for one capture page, split at tag boundaries."""
    text = "\n".join(difflib.unified_diff(
        _pieces(before), _pieces(after),
        f"{rel} (before)", f"{rel} (after)", n=DIFF_CONTEXT, lineterm=""))
    if len(text) > MAX_PAGE_DIFF_CHARS:
        return text[:MAX_PAGE_DIFF_CHARS], True
    return text, False


def _sha(text):
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _changed_pages(walkthrough_id, history, paths):
    """Per-page diff records for an edit's rewritten pages."""
    prefix = history.get("pages_prefix")
    if paths is None:
        # No result to name them (the edit raised after saving): the backups
        # are the pages it overwrote.
        paths = sorted(k[len(prefix):] for k in blobs.list_keys(prefix)) if prefix else []
    out = []
    for rel in paths:
        before = None
        if prefix:
            try:
                before = blobs.get_text(prefix + rel)
            except blobs.NotFound:
                pass   # a page the edit created; save_with_history had nothing to back up
        after = store.capture_text(walkthrough_id, rel)
        diff, truncated = page_diff(before, after, rel)
        out.append({"page": rel,
                    "before_sha256": _sha(before), "after_sha256": _sha(after),
                    "before_chars": len(before or ""), "after_chars": len(after or ""),
                    "diff": diff, "truncated": truncated})
    return out


def edit_changes(walkthrough_id, stamp, paths=None):
    """The exact before/after of the save recorded under history `stamp`."""
    history = store.history_entry(walkthrough_id, stamp)
    row = store.get(walkthrough_id)
    if history is None or row is None:
        return {"error": f"history entry {stamp!r} or the walkthrough is gone"}
    return {
        "history_stamp": stamp,
        "pages_prefix": history.get("pages_prefix"),
        "definition_diff": definition_diff(history["definition"], row["definition"]),
        "pages": _changed_pages(walkthrough_id, history, paths),
    }


def history_stamps(walkthrough_id):
    """The stamps already in history. Taken before an edit starts, so a
    failed edit that nonetheless saved is told apart from one that never did
    by set difference — not by comparing the database's clock with ours.

    None when history could not be read: this runs before the edit, and a
    diagnostic must not be the reason an edit never starts."""
    try:
        return {e["stamp"] for e in store.history_entries(walkthrough_id)}
    except Exception as e:
        print(f"[warn] history for {walkthrough_id} unreadable before an edit: {e}")
        return None


def record_ai_edit(walkthrough_id, member_id, request, started_at,
                   prior_stamps, result=None, error=None):
    """Store one AI edit attempt. `request` is what the author sent, as sent;
    `prior_stamps` is `history_stamps()` from before the edit began.

    Exactly one of `result` (run_edit's return) or `error` is given. Returns
    True when the entry was stored.
    """
    try:
        entry = {
            "request": request,
            "started_at": started_at.isoformat(),
            "finished_at": _now_iso(),
        }
        if result is not None and result.get("kind") == "answer":
            # A question answered: nothing was written, so there is no diff.
            entry["outcome"] = "answered"
            entry["result"] = {k: result.get(k) for k in
                               ("answer", "suggested_change")}
        elif result is not None:
            entry["outcome"] = "saved"
            entry["result"] = {k: result.get(k) for k in
                               ("summary", "warnings", "steps_before",
                                "steps_after", "pages_changed", "stamp")}
            entry["changes"] = edit_changes(walkthrough_id, result["stamp"],
                                            result.get("pages_changed_paths"))
        else:
            cancelled = isinstance(error, jobs.Cancelled)
            entry["outcome"] = "cancelled" if cancelled else "failed"
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
            # AuthorError is the pass refusing with a message meant for the
            # author; anything else is a fault, and its stack is the evidence.
            if not cancelled and type(error).__name__ != "AuthorError":
                entry["error"]["traceback"] = "".join(
                    traceback.format_exception(error))[-20_000:]
            # A cancel or a crash can land after the save — the post-save sweep
            # reports progress, and progress is where a cancel is raised. If
            # it did, the walkthrough changed and the diff is still owed.
            now = history_stamps(walkthrough_id)
            saved = sorted(now - prior_stamps) if now is not None and prior_stamps is not None else []
            if saved:
                entry["outcome"] += "_after_save"
                entry["changes"] = edit_changes(walkthrough_id, saved[-1])
        record(walkthrough_id, member_id, "ai_edit", entry)
        return True
    except Exception as e:
        print(f"[warn] AI edit log for {walkthrough_id} not stored: {e}")
        return False


# ---- authoring passes ---------------------------------------------------------

def _trail(events, started_at):
    """The job's progress events, timed from the start of the pass."""
    t0 = started_at.timestamp()
    out = []
    for e in (events or [])[-MAX_TRAIL_EVENTS:]:
        out.append({"t": round(max(0.0, (e.get("at") or t0) - t0), 1),
                    "phase": str(e.get("phase") or ""),
                    "message": str(e.get("message") or "")})
    return out


def record_author(walkthrough_id, member_id, started_at, events,
                  result=None, error=None):
    """Store one authoring pass. Exactly one of `result` (run_pass's return)
    or `error` is given; `events` is the job's progress log. Returns True when
    the entry was stored."""
    try:
        entry = {
            "started_at": started_at.isoformat(),
            "finished_at": _now_iso(),
            "trail": _trail(events, started_at),
        }
        if result is not None:
            entry["outcome"] = "authored"
            entry["result"] = {k: result.get(k) for k in
                               ("title", "steps_before", "steps_after", "warnings")}
        else:
            cancelled = isinstance(error, jobs.Cancelled)
            entry["outcome"] = "cancelled" if cancelled else "failed"
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
            if not cancelled and type(error).__name__ != "AuthorError":
                entry["error"]["traceback"] = "".join(
                    traceback.format_exception(error))[-20_000:]
        record(walkthrough_id, member_id, "author", entry)
        return True
    except Exception as e:
        print(f"[warn] authoring log for {walkthrough_id} not stored: {e}")
        return False


def _ai_edit_summary(entry):
    """An AI edit record without its diffs. The diffs are evidence for a
    developer; in the owner's log they would be megabytes of page markup."""
    out = {k: entry.get(k) for k in ("request", "started_at", "finished_at",
                                     "outcome", "result", "error")
           if entry.get(k) is not None}
    req = out.get("request")
    if isinstance(req, dict) and isinstance(req.get("selection"), dict):
        sel = req["selection"]
        # the pointed-at element by name, not its markup excerpt
        out["request"] = {**req, "selection": {k: sel.get(k) for k in
                                               ("page", "selector", "text", "tag")
                                               if sel.get(k)}}
    if isinstance(out.get("error"), dict):
        out["error"] = {k: v for k, v in out["error"].items() if k != "traceback"}
    changes = entry.get("changes")
    if isinstance(changes, dict):
        out["pages_rewritten"] = [p.get("page") for p in changes.get("pages") or []]
    return out


def owner_view(walkthrough_id):
    """The AI log as the catalog shows it: authoring passes and AI edits,
    newest first, without diffs or tracebacks. The recorder's capture log is
    left out — it is the extension's transcript, not something the AI did."""
    rows = db.query(
        """select id, kind, entry, created_at
             from walkthrough_logs
            where walkthrough_id = %s and kind in ('author', 'ai_edit')
         order by created_at desc, id desc""",
        (walkthrough_id,),
    )
    out = []
    for row in rows:
        entry = row["entry"] or {}
        if row["kind"] == "author":
            entry = dict(entry)
            if isinstance(entry.get("error"), dict):
                entry["error"] = {k: v for k, v in entry["error"].items()
                                  if k != "traceback"}
        else:
            entry = _ai_edit_summary(entry)
        out.append({"id": row["id"], "kind": row["kind"],
                    "created_at": row["created_at"].isoformat(),
                    "entry": entry})
    return out
