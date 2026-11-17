"""Collections — named, ordered sets of walkthroughs, private to one account.

A walkthrough may sit in several collections ("Issue a refund" belongs to both
Support onboarding and Billing), and a collection's order is the order its
author put it in. That ordering is the point: these are training materials, and
"these six, in this order" is the thing the flat catalog could not express.

NOTE ON THE MODULE NAME. This file must never be renamed to `collections.py`.
The repository root is `sys.path[0]` both for `python3 server.py` and for the
test suite, so a root-level `collections.py` shadows the standard library's
`collections` package for the entire project — taking `json`, `dataclasses`,
`email`, `http.server` and `unittest` down with it, with tracebacks that appear
to come from inside Python itself. Nothing in CI catches it; the guard is
`tests/test_library_store.py::test_this_module_is_not_named_collections`.
(Unrelatedly, `branding.py` already uses "collection" to mean a bundle of brand
profiles. Two different meanings coexist on purpose; do not unify the
vocabulary.)

Membership is its own table rather than a list on the collection, because
position is a property of the (collection, walkthrough) pair and has nowhere to
live on either one, and because an empty collection has to be expressible.

WHAT THE FOREIGN KEY REPLACED

Membership used to be a list of walkthrough ids inside a JSON file, so deleting
a walkthrough left dangling ids behind. The read path filtered them out on every
call and a separate `remove_walkthrough()` swept them on delete — two mechanisms
for one invariant, and the sweep had to be remembered by every future delete
path. `collection_items.walkthrough_id references walkthroughs(id) on delete
cascade` is now the whole of it: a deleted walkthrough leaves every collection
in the same transaction, and there is no such thing as a dangling member.
"""
import re
import uuid

from . import db

MAX_COLLECTIONS = 100
MAX_ITEMS = 500
MAX_NAME = 80
MAX_DESCRIPTION = 200
MAX_SLUG = 80

# Names and descriptions are rendered into HTML. Control characters are stripped
# rather than escaped because they have no legitimate use in either field and
# because a stray \x00 breaks the JSON round trip before any escaping runs.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LibraryError(Exception):
    """A rejected write, with a message meant for the person who made it."""


def _is_id(value):
    """Whether `value` is a well-formed uuid.

    Every id this module takes arrives off a URL or a JSON body, so it is
    attacker-chosen. Postgres raises 22P02 (`invalid input syntax for type
    uuid`) on a malformed one, and psycopg surfaces that as an exception the
    routes do not catch — so `DELETE /api/collections/not-a-uuid` answered
    **500** where every sibling route answered 404. A 500 on user input is a
    stack trace in a log and a leaked implementation detail in a response.

    Checked here rather than caught per-query. Three of the eleven functions in
    this module had a `try/except psycopg.Error` and the rest did not, which is
    the shape of a rule nobody can apply consistently: the next function added
    starts unguarded, and the gap only shows up when someone types a bad id.
    One test at the top of each public entry point is a rule that holds.

    A malformed id and an id that does not exist deliberately get the SAME
    answer, so the API cannot be used to distinguish "no such collection" from
    "not yours" from "not even an id".
    """
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _text(value, cap):
    return _CONTROL.sub("", str(value or "")).strip()[:cap]


def _slugify(name, fallback="collection"):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:MAX_SLUG].strip("-") or fallback


def _unique_slug(owner_id, name):
    """A slug free within one owner.

    Computed once, at create, and never recomputed on rename — the same rule the
    JSON store had. A link to a collection should survive it being retitled.
    """
    base = _slugify(name)
    taken = {
        row["slug"] for row in db.query(
            "select slug from collections where owner_id = %s and slug like %s",
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

def list_collections(owner_id):
    """Every collection for one account, with its members in order.

    One query for the collections and one for all their members, rather than a
    query per collection. The catalog renders this on every load.
    """
    rows = db.query(
        """select id, slug, name, description, created_at, updated_at
             from collections
            where owner_id = %s
         order by name""",
        (owner_id,),
    )
    if not rows:
        return []
    members = db.query(
        """select i.collection_id as collection_id,
                  i.walkthrough_id as walkthrough_id
             from collection_items i
             join collections c on c.id = i.collection_id
            where c.owner_id = %s
         order by i.collection_id, i.position""",
        (owner_id,),
    )
    by_collection = {}
    for m in members:
        by_collection.setdefault(m["collection_id"], []).append(m["walkthrough_id"])
    for row in rows:
        row["walkthroughs"] = by_collection.get(row["id"], [])
        row["count"] = len(row["walkthroughs"])
    return rows


def get_collection(owner_id, collection_id):
    """One collection with its members, or None.

    Filtered on owner in the query, so another account's collection is
    indistinguishable from one that does not exist.
    """
    if not _is_id(collection_id):
        return None
    row = db.one(
        """select id, slug, name, description, created_at, updated_at
             from collections where id = %s and owner_id = %s""",
        (collection_id, owner_id),
    )
    if not row:
        return None
    row["walkthroughs"] = [
        r["walkthrough_id"] for r in db.query(
            """select walkthrough_id
                 from collection_items where collection_id = %s
             order by position""",
            (collection_id,),
        )
    ]
    row["count"] = len(row["walkthroughs"])
    return row


def _require(owner_id, collection_id):
    row = get_collection(owner_id, collection_id)
    if not row:
        raise LibraryError("No such collection.")
    return row


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def create_collection(owner_id, name, description=""):
    """Create a collection. Returns it in the same shape as list_collections."""
    name = _text(name, MAX_NAME)
    if not name:
        raise LibraryError("A collection needs a name.")
    count = db.value(
        "select count(*) from collections where owner_id = %s", (owner_id,), default=0
    )
    if count >= MAX_COLLECTIONS:
        raise LibraryError(f"You can have at most {MAX_COLLECTIONS} collections.")
    row = db.one(
        """insert into collections (owner_id, slug, name, description)
           values (%s, %s, %s, %s)
           returning id, slug, name, description, created_at, updated_at""",
        (owner_id, _unique_slug(owner_id, name), name,
         _text(description, MAX_DESCRIPTION)),
    )
    row["walkthroughs"] = []
    row["count"] = 0
    return row


def update_collection(owner_id, collection_id, name=None, description=None):
    """Rename or re-describe. The slug is deliberately not recomputed."""
    _require(owner_id, collection_id)
    if name is not None:
        name = _text(name, MAX_NAME)
        if not name:
            raise LibraryError("A collection needs a name.")
    if description is not None:
        description = _text(description, MAX_DESCRIPTION)
    db.execute(
        """update collections
              set name = coalesce(%s, name),
                  description = coalesce(%s, description)
            where id = %s and owner_id = %s""",
        (name, description, collection_id, owner_id),
    )
    return get_collection(owner_id, collection_id)


def delete_collection(owner_id, collection_id):
    """Remove a collection. Its members cascade; the walkthroughs are untouched."""
    if not _is_id(collection_id):
        raise LibraryError("No such collection.")
    removed = db.execute(
        "delete from collections where id = %s and owner_id = %s",
        (collection_id, owner_id),
    )
    if not removed:
        raise LibraryError("No such collection.")
    return True


def _clean_items(owner_id, walkthrough_ids):
    """De-duplicated, order-preserving, owned-by-this-account walkthrough ids.

    Ids the caller does not own are dropped rather than rejected. The client
    sends the list it believes is current, and a walkthrough deleted in another
    tab between load and save is an ordinary race, not an error worth failing
    the whole save over. Ownership is checked in the same query, so this is
    also what stops a member filing somebody else's walkthrough in their
    collection.
    """
    seen = set()
    wanted = []
    for wid in (walkthrough_ids or []):
        wid = str(wid or "").strip()
        if wid and wid not in seen:
            seen.add(wid)
            wanted.append(wid)
    if not wanted:
        return []
    if len(wanted) > MAX_ITEMS:
        raise LibraryError(f"A collection can hold at most {MAX_ITEMS} walkthroughs.")
    # Filtered before the query rather than caught after it. The old code
    # dropped the WHOLE list when any single entry was malformed, so one bad id
    # from a stale tab silently emptied a collection.
    wanted = [wid for wid in wanted if _is_id(wid)]
    if not wanted:
        return []
    owned = {
        row["id"] for row in db.query(
            f"""select id from walkthroughs
                where owner_id = %s and id in ({db.placeholders(wanted)})""",
            (owner_id, *wanted),
        )
    }
    return [wid for wid in wanted if wid in owned]


def set_items(owner_id, collection_id, walkthrough_ids):
    """Replace a collection's members, in the given order.

    Delete-then-insert inside one transaction rather than a diff: the client
    sends the whole ordered list, positions shift for most members on any
    reorder, and a diff would be more code for the same result.
    """
    _require(owner_id, collection_id)
    items = _clean_items(owner_id, walkthrough_ids)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "delete from collection_items where collection_id = %s", (collection_id,)
        )
        for position, wid in enumerate(items):
            cur.execute(
                """insert into collection_items (collection_id, walkthrough_id, position)
                   values (%s, %s, %s)""",
                (collection_id, wid, position),
            )
        cur.execute(
            "update collections set updated_at = now() where id = %s", (collection_id,)
        )
    return get_collection(owner_id, collection_id)


def set_membership(owner_id, walkthrough_id, collection_ids):
    """Set which collections one walkthrough belongs to.

    The other direction of set_items, for the "add to collection" menu on a
    card. A walkthrough joining a collection goes on the end; one that is
    already a member keeps its position, because reordering somebody's
    curated sequence is not what ticking a checkbox should mean.
    """
    if not store_owns(owner_id, walkthrough_id):
        raise LibraryError("No such walkthrough.")
    # Malformed ids are dropped rather than raising: this is the "tick the
    # boxes" path, the client sends the whole set, and one bad entry should not
    # fail the other nine.
    wanted = {str(c).strip() for c in (collection_ids or [])
              if str(c).strip() and _is_id(c)}
    mine = {row["id"] for row in db.query(
        "select id from collections where owner_id = %s", (owner_id,)
    )}
    wanted &= mine
    with db.connection() as conn, conn.cursor() as cur:
        # Leave the collections it should still be in, so their positions
        # survive. `in ()` with an empty list is false for every row, which is
        # exactly the "remove it from everything" case — no branch needed.
        keep = sorted(wanted)
        cur.execute(
            f"""delete from collection_items
                where walkthrough_id = %s
                  and collection_id in (
                      select id from collections where owner_id = %s)
                  and collection_id not in ({db.placeholders(keep)})""",
            (walkthrough_id, owner_id, *keep),
        )
        for cid in wanted:
            cur.execute(
                """insert into collection_items (collection_id, walkthrough_id, position)
                   select %s, %s, coalesce(max(position) + 1, 0)
                     from collection_items where collection_id = %s
                   on conflict (collection_id, walkthrough_id) do nothing""",
                (cid, walkthrough_id, cid),
            )
    return list_collections(owner_id)


def store_owns(owner_id, walkthrough_id):
    """Whether this account owns that walkthrough.

    Here rather than imported from `store` to keep the import edge one-way:
    nothing in the collection path should be able to pull the capture layer in
    behind it.
    """
    if not _is_id(walkthrough_id):
        return False
    return bool(db.one(
        "select 1 from walkthroughs where id = %s and owner_id = %s",
        (walkthrough_id, owner_id),
    ))
