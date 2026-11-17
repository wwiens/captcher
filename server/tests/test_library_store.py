"""Unit tests for library.py — the per-account collections store.

No server; everything runs against the throwaway schema conftest.py created.

WHAT THE MOVE TO POSTGRES CHANGED HERE

Collections were a JSON file per account, so a third of this suite was about
the file: where it lived, that its name was a hash rather than the address,
that a corrupt one read as empty, that a save left no temp file behind. None of
those things exist now and those tests are gone (see the report). Two of them
had a successor rather than a replacement, and both are below:

  - `remove_walkthrough` swept dangling ids out of every collection on delete,
    and the read path filtered out any it missed. Two mechanisms for one
    invariant. `collection_items.walkthrough_id references walkthroughs(id) on
    delete cascade` is now the whole of it, and the test that used to call the
    sweep now deletes a walkthrough and looks.
  - "a read must not rewrite the file" guarded against a page load destroying
    membership for a walkthrough that was merely absent. A walkthrough cannot
    be merely absent any more — the foreign key makes "gone" and "not a member"
    the same transaction — so the guard has nothing left to protect.
"""
import importlib.util
import threading
import unittest
import uuid
from pathlib import Path

from captcher import library
from captcher import store
from conftest import new_member

from . import fixtures


class ModuleNameTests(unittest.TestCase):
    """No database: this is about the repository layout."""

    def test_this_module_is_not_named_collections(self):
        """A root-level collections.py shadows the stdlib package for the whole
        project — json, dataclasses, email, http.server and unittest all break,
        with tracebacks that look like they come from inside Python.

        Nothing in CI catches stdlib shadowing; this assertion is the only
        guard there is.
        """
        root = Path(library.__file__).resolve().parent
        self.assertFalse((root / "collections.py").exists(),
                         "collections.py in the repo root shadows the stdlib")
        spec = importlib.util.find_spec("collections")
        self.assertNotIn(str(root), str(spec.origin),
                         "`collections` no longer resolves to the standard library")


class CreateRenameDeleteTests(unittest.TestCase):
    def setUp(self):
        self.owner = new_member()
        self.other = new_member()

    def test_the_id_is_opaque_and_the_slug_is_the_human_handle(self):
        """The id used to be the slug, which is what made two collections named
        the same collide in the primary key."""
        c = library.create_collection(self.owner, "New Support Rep Onboarding")
        self.assertEqual(uuid.UUID(c["id"]).version, 4)
        self.assertEqual(c["slug"], "new-support-rep-onboarding")
        self.assertEqual(c["name"], "New Support Rep Onboarding")
        self.assertEqual(c["walkthroughs"], [])
        self.assertEqual(c["count"], 0)

    def test_duplicate_names_get_distinct_slugs_within_an_owner(self):
        a = library.create_collection(self.owner, "Billing")
        b = library.create_collection(self.owner, "Billing")
        theirs = library.create_collection(self.other, "Billing")
        self.assertEqual((a["slug"], b["slug"]), ("billing", "billing-2"))
        self.assertEqual(theirs["slug"], "billing",
                         "slugs are unique per owner, not globally")

    def test_an_unslugifiable_name_still_gets_a_slug(self):
        """_slugify keeps [a-z0-9] only, so these slug to the empty string."""
        for name in ("日本語", "→→→", "!!!"):
            with self.subTest(name=name):
                c = library.create_collection(self.owner, name)
                self.assertTrue(c["slug"])
                self.assertEqual(c["name"], name)

    def test_an_empty_name_is_rejected(self):
        for name in ("", "   ", None, "\x00\x01"):
            with self.subTest(name=name), self.assertRaises(library.LibraryError):
                library.create_collection(self.owner, name)

    def test_name_and_description_are_capped(self):
        """Capped in Python and again by a check constraint. The Python cap is
        what turns a long paste into a saved collection rather than a 500."""
        c = library.create_collection(self.owner, "N" * 500, "D" * 500)
        self.assertEqual(len(c["name"]), library.MAX_NAME)
        self.assertEqual(len(c["description"]), library.MAX_DESCRIPTION)

    def test_control_characters_are_stripped_from_text(self):
        """Stripped rather than escaped: they have no legitimate use in either
        field, and a stray \\x00 breaks the round trip before escaping runs."""
        c = library.create_collection(self.owner, "Bill\x00ing\x1f")
        self.assertNotIn("\x00", c["name"])
        self.assertNotIn("\x1f", c["name"])

    def test_renaming_keeps_the_id_and_the_slug(self):
        """A link to a collection should survive it being retitled."""
        c = library.create_collection(self.owner, "Billing")
        renamed = library.update_collection(self.owner, c["id"],
                                            name="Billing & Refunds")
        self.assertEqual(renamed["id"], c["id"])
        self.assertEqual(renamed["slug"], "billing")
        self.assertEqual(renamed["name"], "Billing & Refunds")

    def test_renaming_an_unknown_collection_raises(self):
        for bad in (str(uuid.uuid4()), "not-a-uuid"):
            with self.subTest(collection_id=bad), \
                    self.assertRaises(library.LibraryError):
                library.update_collection(self.owner, bad, name="x")

    def test_deleting_a_collection_keeps_its_walkthroughs(self):
        wt = fixtures.make_walkthrough(self.owner)
        c = library.create_collection(self.owner, "Billing")
        library.set_items(self.owner, c["id"], [wt["id"]])
        library.delete_collection(self.owner, c["id"])
        self.assertEqual(library.list_collections(self.owner), [])
        self.assertIsNotNone(store.get(wt["id"], self.owner))

    def test_deleting_an_unknown_collection_raises(self):
        with self.assertRaises(library.LibraryError):
            library.delete_collection(self.owner, str(uuid.uuid4()))

    def test_deleting_a_malformed_collection_id_raises_libraryerror(self):
        """KNOWN FAILURE — a production bug, reported rather than worked around.

        `library.delete_collection` passes the id straight into `db.execute`
        with no guard, so `DELETE /api/collections/not-a-uuid` raises
        psycopg's InvalidTextRepresentation. `handle_collection_delete` catches
        only LibraryError, so that reaches the client as a 500 where every
        sibling route answers 404.

        Every other id-taking function in this module already guards it —
        `get_collection`, `_clean_items` and `store_owns` all wrap their query
        in `except db.psycopg.Error`, and `store.get` catches
        InvalidTextRepresentation for the same reason. This one was missed.

        The id comes off the URL, so the input is attacker-chosen and reaching
        it costs one request. The test is left failing deliberately: weakening
        it to `assertRaises(Exception)` would record the bug as the contract.
        """
        with self.assertRaises(library.LibraryError):
            library.delete_collection(self.owner, "not-a-uuid")

    def test_the_collection_cap_is_enforced(self):
        for i in range(library.MAX_COLLECTIONS):
            library.create_collection(self.owner, f"C{i}")
        with self.assertRaises(library.LibraryError):
            library.create_collection(self.owner, "one too many")

    def test_a_new_account_has_no_collections(self):
        self.assertEqual(library.list_collections(new_member()), [])


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.owner = new_member()
        self.other = new_member()
        self.wt = {name: fixtures.make_walkthrough(self.owner, title=name)["id"]
                   for name in ("alpha", "beta", "gamma")}

    def only(self, owner=None):
        return library.list_collections(owner or self.owner)[0]

    def test_set_items_sets_the_order(self):
        """The ordering is the point: these are training materials, and
        "these six, in this order" is what the flat catalog could not say."""
        c = library.create_collection(self.owner, "Onboarding")
        library.set_items(self.owner, c["id"], [self.wt["gamma"], self.wt["alpha"]])
        self.assertEqual(self.only()["walkthroughs"],
                         [self.wt["gamma"], self.wt["alpha"]])
        library.set_items(self.owner, c["id"], [self.wt["alpha"], self.wt["gamma"]])
        self.assertEqual(self.only()["walkthroughs"],
                         [self.wt["alpha"], self.wt["gamma"]])

    def test_members_are_deduped_in_first_seen_order(self):
        c = library.create_collection(self.owner, "Onboarding")
        got = library.set_items(self.owner, c["id"], [
            self.wt["beta"], self.wt["alpha"], self.wt["beta"], self.wt["gamma"]])
        self.assertEqual(got["walkthroughs"],
                         [self.wt["beta"], self.wt["alpha"], self.wt["gamma"]])

    def test_unknown_walkthrough_ids_are_dropped_not_raised(self):
        """The client sends the list it believes is current; a walkthrough
        deleted in another tab between load and save is an ordinary race. A 404
        naming another account's walkthrough would also confirm it exists."""
        c = library.create_collection(self.owner, "Onboarding")
        got = library.set_items(self.owner, c["id"], [
            self.wt["alpha"], str(uuid.uuid4()), self.wt["beta"]])
        self.assertEqual(got["walkthroughs"], [self.wt["alpha"], self.wt["beta"]])

    def test_a_malformed_id_in_the_list_does_not_take_the_save_down(self):
        """One bad entry costs that entry, not the whole collection.

        This assertion was inverted once, and the inversion is worth recording.
        `= any(%s::uuid[])` raises on a malformed member, so the original code
        caught the error and returned an empty list — "treat the whole list as
        unresolvable rather than guess which entry was wrong". Stated that way
        it sounds careful. What it actually does is empty a curated collection
        because one id in the payload was malformed, which a stale browser tab
        is enough to cause. That is data loss triggered by bad input.

        It was also inconsistent: ids the caller does not own were already
        dropped individually, with the rest of the save going through. A
        malformed id is less trustworthy than an unowned one, not more, so it
        cannot reasonably be the one that takes everything else with it.

        Malformed ids are now filtered before the query, alongside unowned ones.
        """
        c = library.create_collection(self.owner, "Onboarding")
        got = library.set_items(self.owner, c["id"],
                                [self.wt["alpha"], "not-a-uuid"])
        self.assertEqual(got["walkthroughs"], [self.wt["alpha"]],
                         "a malformed id emptied the collection instead of "
                         "being dropped on its own")

    def test_another_accounts_walkthrough_cannot_be_filed(self):
        """Ownership is checked in the same query that resolves the ids, so
        this is not a second check that can be forgotten."""
        theirs = fixtures.make_walkthrough(self.other)
        c = library.create_collection(self.owner, "Onboarding")
        got = library.set_items(self.owner, c["id"],
                                [self.wt["alpha"], theirs["id"]])
        self.assertEqual(got["walkthroughs"], [self.wt["alpha"]])

    def test_the_item_cap_is_enforced(self):
        c = library.create_collection(self.owner, "Onboarding")
        too_many = [str(uuid.uuid4()) for _ in range(library.MAX_ITEMS + 1)]
        with self.assertRaises(library.LibraryError):
            library.set_items(self.owner, c["id"], too_many)

    def test_set_membership_updates_every_collection_at_once(self):
        a = library.create_collection(self.owner, "A")
        b = library.create_collection(self.owner, "B")
        library.set_items(self.owner, a["id"], [self.wt["alpha"]])

        library.set_membership(self.owner, self.wt["beta"], [a["id"], b["id"]])
        by_id = {c["id"]: c for c in library.list_collections(self.owner)}
        self.assertEqual(by_id[a["id"]]["walkthroughs"],
                         [self.wt["alpha"], self.wt["beta"]], "joins on the end")
        self.assertEqual(by_id[b["id"]]["walkthroughs"], [self.wt["beta"]])

        library.set_membership(self.owner, self.wt["beta"], [b["id"]])
        by_id = {c["id"]: c for c in library.list_collections(self.owner)}
        self.assertEqual(by_id[a["id"]]["walkthroughs"], [self.wt["alpha"]])
        self.assertEqual(by_id[b["id"]]["walkthroughs"], [self.wt["beta"]])

    def test_set_membership_leaves_an_existing_position_alone(self):
        """Ticking a checkbox must not reorder somebody's curated sequence."""
        a = library.create_collection(self.owner, "A")
        library.set_items(self.owner, a["id"],
                          [self.wt["gamma"], self.wt["alpha"], self.wt["beta"]])
        library.set_membership(self.owner, self.wt["alpha"], [a["id"]])
        self.assertEqual(library.get_collection(self.owner, a["id"])["walkthroughs"],
                         [self.wt["gamma"], self.wt["alpha"], self.wt["beta"]])

    def test_set_membership_rejects_an_unknown_walkthrough(self):
        a = library.create_collection(self.owner, "A")
        theirs = fixtures.make_walkthrough(self.other)
        for bad in (str(uuid.uuid4()), theirs["id"], "not-a-uuid"):
            with self.subTest(walkthrough=bad), \
                    self.assertRaises(library.LibraryError):
                library.set_membership(self.owner, bad, [a["id"]])

    def test_set_membership_ignores_unknown_collection_ids(self):
        a = library.create_collection(self.owner, "A")
        theirs = library.create_collection(self.other, "Theirs")
        library.set_membership(self.owner, self.wt["alpha"],
                               [a["id"], str(uuid.uuid4()), theirs["id"]])
        self.assertEqual(self.only()["walkthroughs"], [self.wt["alpha"]])
        self.assertEqual(library.get_collection(self.other, theirs["id"])["walkthroughs"],
                         [])

    def test_ids_come_back_as_strings_not_uuid_objects(self):
        """Both columns are uuid, and both are compared against ids that
        arrived as strings — off a request body, out of a cookie. See the
        convention note in db.py."""
        a = library.create_collection(self.owner, "A")
        library.set_items(self.owner, a["id"], [self.wt["alpha"]])
        listed = self.only()
        self.assertIsInstance(listed["id"], str)
        self.assertIsInstance(listed["walkthroughs"][0], str)
        self.assertEqual(listed["walkthroughs"][0], self.wt["alpha"])


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.owner = new_member()
        self.other = new_member()

    def test_deleting_a_walkthrough_removes_it_from_every_collection(self):
        """The successor to `remove_walkthrough`, which swept dangling ids by
        hand and had to be remembered by every future delete path. This is now
        the foreign key's job and happens in the same transaction."""
        alpha = fixtures.make_walkthrough(self.owner, title="Alpha")["id"]
        beta = fixtures.make_walkthrough(self.owner, title="Beta")["id"]
        a = library.create_collection(self.owner, "A")
        b = library.create_collection(self.owner, "B")
        library.set_items(self.owner, a["id"], [alpha, beta])
        library.set_items(self.owner, b["id"], [beta])

        store.delete(beta)

        by_id = {c["id"]: c for c in library.list_collections(self.owner)}
        self.assertEqual(by_id[a["id"]]["walkthroughs"], [alpha])
        self.assertEqual(by_id[a["id"]]["count"], 1)
        self.assertEqual(by_id[b["id"]]["walkthroughs"], [])

    def test_there_is_no_such_thing_as_a_dangling_member(self):
        """The read path used to filter unresolvable ids out on every call,
        because the JSON file could hold them. Nothing can hold one now."""
        wt = fixtures.make_walkthrough(self.owner)["id"]
        c = library.create_collection(self.owner, "A")
        library.set_items(self.owner, c["id"], [wt])
        store.delete(wt)
        self.assertEqual(library.get_collection(self.owner, c["id"])["walkthroughs"],
                         [])

    def test_collections_are_private_to_their_owner(self):
        library.create_collection(self.owner, "Alice only")
        self.assertEqual(library.list_collections(self.other), [])
        library.create_collection(self.other, "Bob only")
        self.assertEqual([c["name"] for c in library.list_collections(self.owner)],
                         ["Alice only"])

    def test_another_accounts_collection_is_indistinguishable_from_a_missing_one(self):
        c = library.create_collection(self.owner, "Alice only")
        self.assertIsNone(library.get_collection(self.other, c["id"]))
        with self.assertRaises(library.LibraryError):
            library.update_collection(self.other, c["id"], name="hijacked")
        with self.assertRaises(library.LibraryError):
            library.delete_collection(self.other, c["id"])
        self.assertEqual(library.get_collection(self.owner, c["id"])["name"],
                         "Alice only")

    def test_store_owns_answers_for_this_account_only(self):
        """Duplicated here rather than imported from `store`, to keep the
        import edge one-way — nothing in the collection path should be able to
        pull the capture layer in behind it."""
        mine = fixtures.make_walkthrough(self.owner)["id"]
        theirs = fixtures.make_walkthrough(self.other)["id"]
        self.assertTrue(library.store_owns(self.owner, mine))
        self.assertFalse(library.store_owns(self.owner, theirs))
        self.assertFalse(library.store_owns(self.owner, "not-a-uuid"))


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_membership_writes_do_not_lose_an_update(self):
        """Two tabs filing different walkthroughs at once must both survive.

        The JSON store needed a lock and a read-modify-write to get this right.
        It is the database's problem now, and this test is what says so — the
        one thing that would still lose an update is a future rewrite that
        reads the membership into Python before writing it back.
        """
        owner = new_member()
        collections = [library.create_collection(owner, name)["id"]
                       for name in ("A", "B", "C")]
        walkthroughs = [
            fixtures.make_walkthrough(owner, title=f"W{i}", steps=0)["id"]
            for i in range(8)
        ]
        errors = []

        def add(wid):
            try:
                library.set_membership(owner, wid, collections)
            except Exception as e:      # noqa: BLE001 — surfaced by the assert below
                errors.append(e)

        threads = [threading.Thread(target=add, args=(w,)) for w in walkthroughs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        for c in library.list_collections(owner):
            with self.subTest(collection=c["name"]):
                self.assertEqual(sorted(c["walkthroughs"]), sorted(walkthroughs))


if __name__ == "__main__":
    unittest.main()
