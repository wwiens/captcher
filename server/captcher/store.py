"""Walkthroughs — the single owner of the `walkthroughs` table and the
`captures` bucket.

Before this module, server.py and export.py each resolved a walkthrough and its
capture folder independently, and the "save the previous version into history/
then write the new one" routine was copy-pasted between the editor save and the
authoring pass. Both live here.

WHAT MOVED, AND WHAT THAT FIXED

The definition, its draft, its sidecars and its version history were seven
separate files under `walkthroughs/<id>/`; they are now columns on one row.
Captured screens were a sibling tree under `captures/`; they are now objects in
Storage under the walkthrough's uuid.

Three long-standing problems go away with the layout:

  - **`capture_dir` is gone.** It was a path stored *relative to the
    walkthrough.json that referenced it*, so a capture folder could carry a
    different name from the walkthrough pointing at it, and five call sites
    re-derived the resolution independently. The prefix is now the uuid, and
    `blobs.prefix_for` is the only code that knows it.
  - **The id is opaque.** Ids were title slugs, de-duplicated by appending
    -2/-3 — which is how two capture folders came to be named `add-points` and
    `add-points-v2`, and why the containment check could never use
    `startswith()`. The slug survives as a column nothing addresses by.
  - **Saves are atomic.** `walkthrough.json` was written with a plain
    `write_text`, so a crash mid-write left a half-parsed definition and a
    walkthrough that vanished from the catalog. A row update is atomic or it
    did not happen.

WHAT IS NO LONGER HERE

The mtime-fingerprint cache. It existed because `load_walkthroughs` re-read and
re-parsed every walkthrough.json on the disk on every authenticated GET, and
again inside `owns_walkthrough()` for every mutating request. The equivalent is
now an indexed lookup on a primary key, so the cache — and the whole class of
bug where a write did not invalidate it — is unnecessary.
"""
import json
import re
from datetime import datetime, timezone


from . import blobs
from . import db

# The capture manifest, as the extension recorded it, stored inside the captures folder
# beside the pages it describes.
CAPTURE_MANIFEST = "walkthrough.json"

# Restore addresses a history entry by this stamp, so its shape is a wire
# contract with the route that validates it.
STAMP_FORMAT = "%Y%m%d-%H%M%S"

MAX_SLUG = 80


class NoSuchWalkthrough(Exception):
    pass


def _now_iso():
    return _iso_z(datetime.now(timezone.utc))


def _iso_z(dt):
    """UTC ISO-8601 with a Z suffix — the shape the extension already sends."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(title, fallback="walkthrough"):
    """A url-safe slug from a title.

    Not an identifier — the uuid is. This is a human handle, kept because a
    catalog full of uuids is unreadable and because the example-walkthrough
    configuration is written by a person.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return (slug[:MAX_SLUG].strip("-") or fallback)


def unique_slug(owner_id, title):
    """A slug free within one owner, suffixed -2, -3 … if taken.

    Scoped per owner, so two customers may both have `log-a-ticket`. Under the
    old scheme this loop ran on the *primary key*, which is what made ids
    collide with capture folder names.
    """
    base = slugify(title)
    taken = {
        row["slug"] for row in db.query(
            "select slug from walkthroughs where owner_id = %s and slug like %s",
            (owner_id, base + "%"),
        )
    }
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_COLUMNS = """
    id, owner_id, slug, title,
    definition, draft, recorded_at, authored_at,
    render_check, verify_check, embed, created_at, updated_at
"""


def get(walkthrough_id, owner_id=None):
    """One walkthrough row, or None.

    Pass `owner_id` for anything a signed-in member asked for: the filter is in
    the query, so somebody else's walkthrough is indistinguishable from one that
    does not exist. Omit it only on the deliberately cross-account paths —
    `/embed/` and `/examples/`, which serve published walkthroughs to the public.

    A malformed id simply matches nothing: the id comes off the URL, and a 404
    is the right answer to a request for a walkthrough that could not exist.
    """
    if not walkthrough_id:
        return None
    sql = f"select {_COLUMNS} from walkthroughs where id = %s"
    params = [walkthrough_id]
    if owner_id is not None:
        sql += " and owner_id = %s"
        params.append(owner_id)
    return db.one(sql, tuple(params))


def require(walkthrough_id, owner_id=None):
    """`get`, but raising NoSuchWalkthrough instead of returning None."""
    row = get(walkthrough_id, owner_id)
    if row is None:
        raise NoSuchWalkthrough(walkthrough_id)
    return row


def load_walkthroughs(owner_id=None):
    """Every walkthrough as {id: row}, optionally filtered to one owner.

    The shape callers want for the catalog and for ownership checks. Filtering
    happens in the query, not afterwards in Python, so an account's rows never
    enter this process at all.
    """
    sql = f"select {_COLUMNS} from walkthroughs"
    params = ()
    if owner_id is not None:
        sql += " where owner_id = %s"
        params = (owner_id,)
    sql += " order by created_at desc"
    return {row["id"]: row for row in db.query(sql, params)}


def get_public_by_slug(slug):
    """One embed-enabled walkthrough by slug, or None. For `/examples/<slug>`.

    The examples list is written by a person in configuration, so it names
    walkthroughs by slug — a list of uuids would be unreadable and unmaintainable.
    That creates a lookup with no owner to filter by, which needs two rules:

    **Only embed-enabled walkthroughs are visible here.** The same rule
    `/embed/` enforces. Configuration alone never publishes anything; naming a
    private walkthrough in the examples list resolves to nothing.

    **An ambiguous slug resolves to nothing, deliberately.** Slugs are unique
    per owner, not globally, so two accounts can both publish `log-a-ticket`.
    Picking one — the oldest, say — would mean the marketing site's example page
    silently changes whose walkthrough it shows when an unrelated customer
    publishes a colliding slug. Returning None instead turns that into a visible
    404 with a line on stdout naming the collision, which is a bug report rather
    than a mystery.
    """
    rows = db.query(
        f"""select {_COLUMNS} from walkthroughs
             where slug = %s and embed ->> 'enabled' = 1""",
        (slug,),
    )
    if not rows:
        return None
    if len(rows) > 1:
        print(f"[store] example slug {slug!r} is ambiguous: "
              f"{len(rows)} published walkthroughs share it, so it resolves to "
              "none. Publish only one, or name it by id.")
        return None
    return rows[0]


def owner_of(walkthrough_id):
    """The owner id, or None. For the public routes, which resolve ownership
    after the fact to pick a brand profile."""
    row = db.one(
        "select owner_id from walkthroughs where id = %s",
        (walkthrough_id,),
    )
    return row["owner_id"] if row else None


def captured_at(row):
    """When the capture behind this walkthrough was recorded — the queue's FIFO
    key. The extension's timestamp when it sent one, else when the row was
    created, since single-page sends do not set one."""
    ts = ((row.get("definition") or {}).get("_source") or {}).get("recorded_at")
    return ts or _iso_z(row.get("recorded_at") or row.get("created_at"))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def create(owner_id, title, definition, recorded_at=None):
    """Insert a new walkthrough and return its row.

    The draft column is written from the same definition at creation and never
    overwritten afterwards, which is what `walkthrough.draft.json` was for: it
    is the pre-authoring text the authoring pass is diffed against.
    """
    row = db.one(
        """insert into walkthroughs (owner_id, slug, title, definition, draft, recorded_at)
           values (%s, %s, %s, %s, %s, %s)
           returning """ + _COLUMNS,
        (owner_id, unique_slug(owner_id, title), title,
         db.Json(definition), db.Json(definition), recorded_at),
    )
    return row


def save_with_history(walkthrough_id, definition, changed_pages=None):
    """Write a new definition, keeping the previous one in history.

    `changed_pages` is {capture-relative path: new text}. When given, each
    page's current content is copied into the history prefix before being
    overwritten — that is what the AI authoring pass needs and the editor save
    does not.

    The definition write and the history row go in one transaction, so a failure
    cannot leave a history entry pointing at a version that was never saved.
    Page copies happen first and outside it: files have no transactions, so the
    ordering is chosen to fail safe. A crash after the copies leaves orphaned
    backup objects, which cost storage; a crash after the definition write with
    the copies missing would lose the only copy of the original pages.

    Returns the history stamp.
    """
    stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    pages_prefix = None

    if changed_pages:
        pages_prefix = f"{blobs.prefix_for(walkthrough_id)}.history/{stamp}/"
        for rel in changed_pages:
            src = blobs.key_for(walkthrough_id, rel)
            if src is None:
                raise ValueError(f"refusing to back up an unsafe page path: {rel!r}")
            try:
                blobs.copy(src, pages_prefix + rel)
            except blobs.NotFound:
                # A page the authoring pass invented rather than edited has no
                # previous version. Nothing to back up; not an error.
                pass

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select definition from walkthroughs where id = %s",
            (walkthrough_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise NoSuchWalkthrough(walkthrough_id)
        cur.execute(
            """insert into walkthrough_history
                   (walkthrough_id, stamp, definition, pages_prefix)
               values (%s, %s, %s, %s)
               on conflict (walkthrough_id, stamp) do nothing""",
            (walkthrough_id, stamp, db.Json(current["definition"]), pages_prefix),
        )
        cur.execute(
            """update walkthroughs
                  set definition = %s,
                      title = coalesce(%s, title)
                where id = %s""",
            (db.Json(definition), definition.get("title"), walkthrough_id),
        )

    # Only once the definition is committed. Reversed, a failed commit would
    # leave the capture rewritten to match a definition that was rolled back.
    for rel, text in (changed_pages or {}).items():
        blobs.put(blobs.key_for(walkthrough_id, rel), text, "text/html; charset=utf-8")

    # Housekeeping, so never the save's failure: an unpruned version costs
    # storage until the next save tries again.
    try:
        prune_history(walkthrough_id)
    except Exception as e:
        print(f"[warn] could not prune history for {walkthrough_id}: {e}")
    return stamp


# The published retention for version history: the newest this many versions.
HISTORY_KEEP = 20


def prune_history(walkthrough_id, keep=None):
    """Drop versions older than the newest `keep`. Returns how many went.

    One exception, and it is load-bearing: a version that holds the earliest
    backup of any page is kept whatever its age, because that backup is the
    page as captured and `reset_to_draft` restores from it. Pruning it would
    make "reset to draft" bring back an AI-edited page as the original.

    Objects go before the row, as in `delete`: an interruption leaves a listed
    version with missing pages, which the next prune finishes, rather than
    unlisted objects nothing will ever remove.
    """
    keep = HISTORY_KEEP if keep is None else keep
    entries = db.query(
        """select id, pages_prefix from walkthrough_history
            where walkthrough_id = %s
         order by created_at asc, id asc""",
        (walkthrough_id,),
    )
    if len(entries) <= keep:
        return 0
    old = entries[:len(entries) - keep]
    # The same walk reset_to_draft makes: oldest first, the first entry to
    # back a page up holds its original. Only the old entries can be pruned,
    # and every entry that precedes them is one of them, so the walk stops there.
    seen, victims = set(), []
    for entry in old:
        prefix = entry["pages_prefix"]
        if prefix:
            rels = {key[len(prefix):] for key in blobs.list_keys(prefix)}
            originals = {r for r in rels if r.lower().endswith(".html")} - seen
            if originals:
                seen |= originals
                continue
        victims.append(entry)
    for entry in victims:
        if entry["pages_prefix"]:
            blobs.delete_prefix(entry["pages_prefix"])
        db.execute("delete from walkthrough_history where id = %s", (entry["id"],))
    return len(victims)


def history_entries(walkthrough_id):
    """Every saved version, newest first, without the definitions themselves."""
    return db.query(
        """select stamp, created_at, pages_prefix is not null as has_pages
             from walkthrough_history
            where walkthrough_id = %s
         order by created_at desc""",
        (walkthrough_id,),
    )


def history_entry(walkthrough_id, stamp):
    """One saved version, or None."""
    return db.one(
        """select stamp, definition, pages_prefix, created_at
             from walkthrough_history
            where walkthrough_id = %s and stamp = %s""",
        (walkthrough_id, stamp),
    )


def delete(walkthrough_id):
    """Remove a walkthrough, its history, its collection memberships and every
    captured object. Returns what went.

    Ownership and the job claim are the caller's business — `handle_delete` and
    the admin purge both take them first. The row goes last: an interrupted
    delete then leaves a walkthrough whose capture is missing, which the catalog
    reports and the owner can delete again. The reverse order would leave
    unreachable objects paying for storage forever with nothing pointing at
    them.
    """
    row = get(walkthrough_id)
    if row is None:
        raise NoSuchWalkthrough(walkthrough_id)
    removed = []
    objects = blobs.delete_prefix(blobs.prefix_for(walkthrough_id))
    if objects:
        removed.append(f"{objects} captured object(s)")
    # history and collection_items cascade from the foreign key
    db.execute("delete from walkthroughs where id = %s", (walkthrough_id,))
    removed.append(f"walkthrough {row['slug']}")
    return removed


# ---------------------------------------------------------------------------
# Authoring state — the queue's source of truth
# ---------------------------------------------------------------------------
# A column rather than something derived from the content. Counting "TODO"
# cannot tell a leftover placeholder from the word appearing legitimately in
# authored learner-facing text, which would strand a card in the queue with no
# way to clear it.

def authoring_state(row):
    """{'authored_at': iso} for a walkthrough that has been through the AI pass,
    or {} for one still waiting."""
    when = row.get("authored_at") if isinstance(row, dict) else None
    return {"authored_at": _iso_z(when)} if when else {}


def mark_authored(walkthrough_id):
    """Record that the authoring pass completed. Never fails the pass over its
    own bookkeeping."""
    try:
        db.execute(
            "update walkthroughs set authored_at = now() where id = %s",
            (walkthrough_id,),
        )
    except Exception as e:
        print(f"[warn] could not record authoring state for {walkthrough_id}: {e}")


class NoDraft(Exception):
    """The walkthrough has no pre-authoring draft to go back to."""


def reset_to_draft(walkthrough_id):
    """Put a walkthrough back the way ingest left it, ready to author again.

    The definition becomes the `draft` column; every captured page an AI edit
    rewrote gets its original back; `authored_at` and both verdicts are cleared,
    so the card returns to the authoring queue and the next pass re-runs the
    render check as the first one did. The verdicts are about the authored steps
    and possibly-edited pages, so keeping them would describe something that is
    no longer there.

    A page's original is its *earliest* backup: each history entry's prefix
    holds the pages as they were before that save, so the first entry to back a
    page up holds it as captured. The write goes through `save_with_history`,
    which makes the reset itself a restorable version, and the authored copy is
    kept there.

    Ownership and the job claim are the caller's business, as for `delete`.
    Returns (stamp, pages restored).
    """
    row = get(walkthrough_id)
    if row is None:
        raise NoSuchWalkthrough(walkthrough_id)
    draft = row.get("draft")
    if not draft:
        raise NoDraft(walkthrough_id)

    originals = {}
    for entry in db.query(
        """select pages_prefix from walkthrough_history
            where walkthrough_id = %s and pages_prefix is not null
         order by created_at asc, id asc""",
        (walkthrough_id,),
    ):
        prefix = entry["pages_prefix"]
        for key in blobs.list_keys(prefix):
            rel = key[len(prefix):]
            if rel.lower().endswith(".html") and rel not in originals:
                originals[rel] = blobs.get_text(key)

    stamp = save_with_history(walkthrough_id, draft, changed_pages=originals or None)
    db.execute(
        """update walkthroughs
              set authored_at = null, render_check = null, verify_check = null
            where id = %s""",
        (walkthrough_id,),
    )
    return stamp, len(originals)


def save_draft(walkthrough_id, draft):
    """Write the pre-authoring draft, once. Later calls are ignored.

    `coalesce` in the WHERE rather than a read-then-write, so two authoring
    passes racing on the same walkthrough cannot both decide the column is
    empty.
    """
    db.execute(
        "update walkthroughs set draft = %s where id = %s and draft is null",
        (db.Json(draft), walkthrough_id),
    )


# ---------------------------------------------------------------------------
# Render-accuracy and replay verdicts
# ---------------------------------------------------------------------------
# render_check.py and verify_pass.py own their schemas; the accessors live here
# because author.py needs to read them and both of those modules import author.

def load_render_check(row):
    """The stored render-accuracy verdicts, or None when no check has run."""
    data = row.get("render_check") if isinstance(row, dict) else None
    return data if isinstance(data, dict) else None


def save_render_check(walkthrough_id, results):
    db.execute(
        "update walkthroughs set render_check = %s where id = %s",
        (db.Json(results), walkthrough_id),
    )


def load_verify_check(row):
    data = row.get("verify_check") if isinstance(row, dict) else None
    return data if isinstance(data, dict) else None


def save_verify_check(walkthrough_id, results):
    db.execute(
        "update walkthroughs set verify_check = %s where id = %s",
        (db.Json(results), walkthrough_id),
    )


# ---------------------------------------------------------------------------
# Embedding on customer sites
# ---------------------------------------------------------------------------
# A walkthrough can be published for embedding: /embed/<id> then serves the
# export publicly (no login) so customer pages can iframe it via
# static/embed.js. Settings are frozen at publish time from the embed dialog:
#   profile / intro  which brand, and whether to include the intro card
#   chrome           "full" (the whole player) or "viewport" (just the bordered
#                    captured region, looping) — baked into the export itself
#   backdrop         whether embed.js dims the customer's page behind the modal
#   max_width        caps how wide the modal may grow; None means no cap
#
# These were a sidecar file because the editor's PUT rewrote walkthrough.json
# wholesale and would have taken them with it. They are their own column for the
# same reason: a save must not be able to unpublish a live customer embed.

DEFAULT_EMBED = {"enabled": False}


def load_embed_settings(row):
    """The walkthrough's embed publication, or None when never published."""
    data = row.get("embed") if isinstance(row, dict) else None
    if not isinstance(data, dict) or not data:
        return None
    return data


def save_embed_settings(walkthrough_id, settings):
    settings = dict(settings or {})
    settings["updated_at"] = _now_iso()
    db.execute(
        "update walkthroughs set embed = %s where id = %s",
        (db.Json(settings), walkthrough_id),
    )


def is_embeddable(row):
    """Whether this walkthrough may be served publicly.

    Configuration alone never publishes: `enabled` must be explicitly true. Both
    `/embed/` and `/examples/` go through here, so neither can drift into
    accepting a merely-configured walkthrough.
    """
    settings = load_embed_settings(row)
    return bool(settings and settings.get("enabled") is True)


# ---------------------------------------------------------------------------
# Captured screens
# ---------------------------------------------------------------------------
# Thin, deliberately: these exist so call sites read as "the capture for this
# walkthrough" rather than composing capture keys themselves. The rules about
# what a key may look like live in blobs.key_for, in one place.

def capture_text(walkthrough_id, rel):
    """One captured page's text, or None when there is no such object."""
    key = blobs.key_for(walkthrough_id, rel)
    if key is None:
        return None
    try:
        return blobs.get_text(key)
    except blobs.NotFound:
        return None


def capture_bytes(walkthrough_id, rel):
    """One captured object's bytes, or None. The /capture/ route's read."""
    key = blobs.key_for(walkthrough_id, rel)
    if key is None:
        return None
    try:
        return blobs.get(key)
    except blobs.NotFound:
        return None


def put_capture(walkthrough_id, rel, data, content_type=None):
    key = blobs.key_for(walkthrough_id, rel)
    if key is None:
        raise ValueError(f"unsafe capture path: {rel!r}")
    return blobs.put(key, data, content_type)


def capture_files(walkthrough_id, suffix=None):
    """Capture-relative paths for one walkthrough, sorted.

    History objects are excluded: they are backups, and every caller that walks
    a capture — the export, the authoring snapshot, the page list — wants the
    live pages only.
    """
    prefix = blobs.prefix_for(walkthrough_id)
    out = []
    for key in blobs.list_keys(prefix):
        rel = key[len(prefix):]
        if rel.startswith(".history/"):
            continue
        if suffix and not rel.lower().endswith(suffix):
            continue
        out.append(rel)
    return out


def capture_manifest(walkthrough_id):
    """The capture manifest as the extension recorded it, or {}.

    `author.observed_effects_block` reads the recorded `actions[].effects` out
    of this, which is the only record of what actually happened during the
    recording.
    """
    text = capture_text(walkthrough_id, CAPTURE_MANIFEST)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
