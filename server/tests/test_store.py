"""Unit tests for store.py — the owner of the walkthroughs table and the bucket.

Replaces tests/test_store_cache.py, which pinned the mtime-fingerprint cache in
front of the old directory scan. That cache is gone (see the note at the foot of
store.py's docstring): a read is an indexed lookup on a primary key now, so
there is nothing to invalidate and no stale-entry class of bug to guard. What
survived the rewrite is the set of properties the cache existed to preserve —
a write is visible on the next read, and one account's rows never appear in
another's — and those are pinned here instead.

The rest is new, and covers the two things the migration introduced that have
no equivalent in the predecessor: uuid columns that must come back as `str`,
and ownership enforced by a WHERE clause rather than by a filter in Python.
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from captcher import blobs
from captcher import db
from captcher import store
from conftest import new_member

from . import capturefiles, fixtures


class WalkthroughReadTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()
        self.bob = new_member()

    # ---- writes are visible on the next read -------------------------------

    def test_a_new_walkthrough_is_visible_immediately(self):
        self.assertEqual(store.load_walkthroughs(), {})
        row = fixtures.make_walkthrough(self.alice)
        self.assertIn(row["id"], store.load_walkthroughs())

    def test_a_save_is_visible_on_the_next_read(self):
        """Through the real write path, not a hand-rolled one."""
        row = fixtures.make_walkthrough(self.alice)
        store.save_with_history(
            row["id"], dict(row["definition"], title="Saved Through The Store"))
        self.assertEqual(store.get(row["id"])["title"], "Saved Through The Store")

    def test_a_delete_is_visible_on_the_next_read(self):
        row = fixtures.make_walkthrough(self.alice)
        store.delete(row["id"])
        self.assertNotIn(row["id"], store.load_walkthroughs())
        self.assertIsNone(store.get(row["id"]))

    # ---- ownership ---------------------------------------------------------

    def test_another_owners_walkthrough_is_indistinguishable_from_a_missing_one(self):
        """`get` filters in SQL, so there is no answer that confirms the id
        exists. Both cases are the same 404 to a route, deliberately: the API
        must not be usable to probe for ids."""
        row = fixtures.make_walkthrough(self.alice)
        self.assertIsNotNone(store.get(row["id"], self.alice))
        self.assertIsNone(store.get(row["id"], self.bob))
        self.assertIsNone(store.get(str(uuid.uuid4()), self.bob))

    def test_the_owner_filter_happens_in_the_query(self):
        """A member's rows must not enter this process at all, which is a
        stronger statement than "they are filtered out afterwards"."""
        fixtures.make_walkthrough(self.alice, title="Alice only")
        fixtures.make_walkthrough(self.bob, title="Bob only")
        self.assertEqual([r["title"] for r in store.load_walkthroughs(self.alice).values()],
                         ["Alice only"])
        self.assertEqual([r["title"] for r in store.load_walkthroughs(self.bob).values()],
                         ["Bob only"])
        self.assertEqual(len(store.load_walkthroughs()), 2)

    def test_a_malformed_id_is_a_miss_rather_than_a_crash(self):
        """The id comes off the URL, so `/walkthrough/nonsense` must 404 rather
        than 500 with an InvalidTextRepresentation from psycopg."""
        for bad in ("not-a-uuid", "", "../etc/passwd", "1", None):
            with self.subTest(walkthrough_id=bad):
                self.assertIsNone(store.get(bad, self.alice))

    # ---- ids are strings --------------------------------------------------

    def test_ids_come_back_as_strings_not_uuid_objects(self):
        """Identity reaches the ownership comparisons from two places — a
        cookie, which is necessarily a string, and a query — and
        `UUID(x) == str(x)` is False. So ids must come back as strings
        everywhere; the auth half is tests/test_pairing.py::PairingTokenTests."""
        row = fixtures.make_walkthrough(self.alice)
        for column in ("id", "owner_id"):
            with self.subTest(column=column):
                self.assertIsInstance(row[column], str)
                self.assertIsInstance(store.get(row["id"])[column], str)
        self.assertEqual(row["owner_id"], self.alice)
        self.assertEqual(store.owner_of(row["id"]), self.alice)
        self.assertIsInstance(store.owner_of(row["id"]), str)

    def test_a_row_id_can_be_compared_against_a_session_id_directly(self):
        """The failure the cast prevents, stated as the thing that broke:
        `load_walkthroughs()` keys against an id carried in a cookie."""
        row = fixtures.make_walkthrough(self.alice)
        self.assertIn(row["id"], store.load_walkthroughs(self.alice))

    # ---- slugs -------------------------------------------------------------

    def test_the_slug_is_derived_from_the_title_and_unique_per_owner(self):
        """Unique per owner, not globally: two customers may both have
        `log-a-ticket`, and under the old scheme this loop ran on the primary
        key, which is what made ids collide with capture folder names."""
        first = store.create(self.alice, "Log a ticket", fixtures.walkthrough_def())
        second = store.create(self.alice, "Log a ticket", fixtures.walkthrough_def())
        theirs = store.create(self.bob, "Log a ticket", fixtures.walkthrough_def())
        self.assertEqual(first["slug"], "log-a-ticket")
        self.assertEqual(second["slug"], "log-a-ticket-2")
        self.assertEqual(theirs["slug"], "log-a-ticket")

    def test_nothing_addresses_a_walkthrough_by_its_slug(self):
        """The id is opaque and the slug is a human handle. If a lookup by slug
        ever appears on an owned path, this is the test that should have to
        change first."""
        row = fixtures.make_walkthrough(self.alice)
        self.assertIsNone(store.get(row["slug"], self.alice))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()
        self.bob = new_member()

    def test_configuration_alone_never_publishes(self):
        """`/embed/` and `/examples/` both go through is_embeddable, so neither
        can drift into serving a merely-configured walkthrough."""
        row = fixtures.make_walkthrough(self.alice)
        self.assertFalse(store.is_embeddable(store.get(row["id"])))
        store.save_embed_settings(row["id"], {"profile": "acme"})
        self.assertFalse(store.is_embeddable(store.get(row["id"])))
        store.save_embed_settings(row["id"], {"enabled": True})
        self.assertTrue(store.is_embeddable(store.get(row["id"])))

    def test_an_ambiguous_example_slug_resolves_to_nothing(self):
        """Slugs are unique per owner, so two accounts can publish the same
        one. Picking either would let an unrelated customer silently change
        whose walkthrough the marketing site shows."""
        mine = store.create(self.alice, "Log a ticket", fixtures.walkthrough_def())
        theirs = store.create(self.bob, "Log a ticket", fixtures.walkthrough_def())
        store.save_embed_settings(mine["id"], {"enabled": True})
        self.assertEqual(store.get_public_by_slug("log-a-ticket")["id"], mine["id"])
        store.save_embed_settings(theirs["id"], {"enabled": True})
        self.assertIsNone(store.get_public_by_slug("log-a-ticket"))

    def test_an_unpublished_slug_is_not_visible_to_the_examples_route(self):
        store.create(self.alice, "Log a ticket", fixtures.walkthrough_def())
        self.assertIsNone(store.get_public_by_slug("log-a-ticket"))

    def test_a_save_cannot_unpublish_a_live_embed(self):
        """Embed settings are their own column for exactly this reason: the
        editor's PUT rewrites the definition wholesale and used to take the
        sidecar with it."""
        row = fixtures.make_walkthrough(self.alice)
        store.save_embed_settings(row["id"], {"enabled": True, "chrome": "viewport"})
        store.save_with_history(row["id"], dict(row["definition"], title="Edited"))
        self.assertTrue(store.is_embeddable(store.get(row["id"])))
        self.assertEqual(store.load_embed_settings(store.get(row["id"]))["chrome"],
                         "viewport")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()
        self.row = fixtures.make_walkthrough(self.alice, steps=2)
        self.wid = self.row["id"]

    def test_a_page_reads_back_byte_identical(self):
        store.put_capture(self.wid, "step-000/page.html", "<html>original</html>")
        self.assertEqual(store.capture_text(self.wid, "step-000/page.html"),
                         "<html>original</html>")

    def test_capture_files_lists_capture_relative_paths_sorted(self):
        """Not keys. Everything above the store addresses a page by the path
        the definition names, and the prefix is the store's business."""
        self.assertEqual(store.capture_files(self.wid),
                         ["step-000/page.html", "step-001/page.html"])

    def test_the_prefix_is_the_walkthrough_id(self):
        """The whole point of the rewrite: no stored `capture_dir` that can
        name a folder other than the walkthrough pointing at it."""
        self.assertEqual(sorted(capturefiles.current().keys()),
                         [f"{self.wid}/step-000/page.html",
                          f"{self.wid}/step-001/page.html"])

    def test_a_traversing_path_is_refused_on_read_and_on_write(self):
        """The containment check that replaced five copies of str.startswith().
        An object store has no `..` resolution of its own, so such a key would
        not escape — but it would let two URLs address one object and sail past
        any later prefix test written the naive way."""
        for rel in ("../other/page.html", "step-000/../../escape.html",
                    "", "step-000//page.html", "step-000\\page.html"):
            with self.subTest(rel=rel):
                self.assertIsNone(store.capture_text(self.wid, rel))
                self.assertIsNone(store.capture_bytes(self.wid, rel))
                with self.assertRaises(ValueError):
                    store.put_capture(self.wid, rel, "x")

    def test_a_missing_page_is_none_rather_than_an_exception(self):
        self.assertIsNone(store.capture_text(self.wid, "step-009/page.html"))
        self.assertIsNone(store.capture_bytes(self.wid, "step-009/page.html"))

    def test_the_manifest_is_read_from_the_bucket_and_is_total(self):
        self.assertEqual(store.capture_manifest(self.wid), {})
        store.put_capture(self.wid, store.CAPTURE_MANIFEST, "{not json")
        self.assertEqual(store.capture_manifest(self.wid), {})
        store.put_capture(self.wid, store.CAPTURE_MANIFEST, '["a list"]')
        self.assertEqual(store.capture_manifest(self.wid), {})
        store.put_capture(self.wid, store.CAPTURE_MANIFEST, '{"actions": []}')
        self.assertEqual(store.capture_manifest(self.wid), {"actions": []})


class SaveWithHistoryTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()
        self.row = fixtures.make_walkthrough(self.alice, steps=1)
        self.wid = self.row["id"]

    def test_the_previous_definition_is_kept_and_the_new_one_is_live(self):
        stamp = store.save_with_history(
            self.wid, dict(self.row["definition"], title="Edited"))
        self.assertEqual(store.get(self.wid)["title"], "Edited")
        self.assertEqual(store.history_entry(self.wid, stamp)["definition"]["title"],
                         fixtures.TITLE)

    def test_changed_pages_are_backed_up_before_being_overwritten(self):
        store.put_capture(self.wid, "step-000/page.html", "<html>original</html>")
        stamp = store.save_with_history(
            self.wid, self.row["definition"],
            changed_pages={"step-000/page.html": "<html>edited</html>"})
        self.assertEqual(store.capture_text(self.wid, "step-000/page.html"),
                         "<html>edited</html>")
        backup = (f"{blobs.prefix_for(self.wid)}.history/{stamp}"
                  "/step-000/page.html")
        self.assertEqual(capturefiles.current().text(backup), "<html>original</html>")

    def test_a_page_the_pass_invented_has_no_backup_and_that_is_not_an_error(self):
        """`blobs.copy` raises NotFound for a source that is not there, and the
        save path swallows exactly that — a page the authoring pass synthesised
        has no previous version to keep."""
        store.save_with_history(
            self.wid, self.row["definition"],
            changed_pages={"auto/summary.html": "<html>new</html>"})
        self.assertEqual(store.capture_text(self.wid, "auto/summary.html"),
                         "<html>new</html>")

    def test_history_backups_are_excluded_from_the_page_list(self):
        """Every caller that walks a capture — the export, the authoring
        snapshot, the page list — wants the live pages only."""
        store.put_capture(self.wid, "step-000/page.html", "<html>original</html>")
        store.save_with_history(
            self.wid, self.row["definition"],
            changed_pages={"step-000/page.html": "<html>edited</html>"})
        self.assertEqual(store.capture_files(self.wid), ["step-000/page.html"])

    def test_an_unsafe_page_path_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            store.save_with_history(
                self.wid, self.row["definition"],
                changed_pages={"../escape.html": "<html>no</html>"})
        self.assertEqual(store.history_entries(self.wid), [])

    def test_saving_a_walkthrough_that_is_gone_raises(self):
        store.delete(self.wid)
        with self.assertRaises(store.NoSuchWalkthrough):
            store.save_with_history(self.wid, self.row["definition"])

    # ---- retention: the newest HISTORY_KEEP versions ---------------------

    def _version(self, stamp, minutes_ago, pages=()):
        prefix = None
        if pages:
            prefix = f"{blobs.prefix_for(self.wid)}.history/{stamp}/"
            for rel in pages:
                blobs.put(prefix + rel, f"<html>{stamp}</html>", "text/html")
        db.execute(
            "insert into walkthrough_history (walkthrough_id, stamp, definition, "
            "  pages_prefix, created_at) "
            "values (%s, %s, '{}', %s, "
            "        strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-' || %s || ' minutes'))",
            (self.wid, stamp, prefix, minutes_ago))
        return prefix

    def test_pruning_keeps_the_newest_and_every_original_page_backup(self):
        first = self._version("20260101-000001", 60, pages=["step-000/page.html"])
        again = self._version("20260101-000002", 50, pages=["step-000/page.html"])
        self._version("20260101-000003", 40)
        later = self._version("20260101-000004", 30, pages=["step-001/page.html"])
        self._version("20260101-000005", 20)
        self._version("20260101-000006", 10)

        self.assertEqual(store.prune_history(self.wid, keep=2), 2)
        self.assertEqual(sorted(e["stamp"] for e in store.history_entries(self.wid)),
                         ["20260101-000001", "20260101-000004",
                          "20260101-000005", "20260101-000006"])
        # the originals survive for reset_to_draft; the second copy goes
        self.assertTrue(blobs.exists(first + "step-000/page.html"))
        self.assertTrue(blobs.exists(later + "step-001/page.html"))
        self.assertFalse(blobs.exists(again + "step-000/page.html"))

    def test_reset_to_draft_still_finds_the_captured_page_after_pruning(self):
        store.put_capture(self.wid, "step-000/page.html", "<html>captured</html>")
        store.save_draft(self.wid, self.row["definition"])
        with mock.patch.object(store, "HISTORY_KEEP", 2):
            for n in range(4):
                with mock.patch.object(store, "datetime") as clock:
                    clock.now.return_value = datetime(2026, 1, 1, 0, 0, n, tzinfo=timezone.utc)
                    store.save_with_history(
                        self.wid, self.row["definition"],
                        changed_pages={"step-000/page.html": f"<html>edit {n}</html>"})
            self.assertLessEqual(len(store.history_entries(self.wid)), 3)
            store.reset_to_draft(self.wid)
        self.assertEqual(store.capture_text(self.wid, "step-000/page.html"),
                         "<html>captured</html>")

    def test_under_the_limit_nothing_is_pruned(self):
        self._version("20260101-000001", 10)
        self.assertEqual(store.prune_history(self.wid, keep=2), 0)
        self.assertEqual(len(store.history_entries(self.wid)), 1)

    def test_the_draft_is_written_once_and_never_overwritten(self):
        """The pre-authoring text the authoring pass is diffed against. Two
        passes racing on one walkthrough must not both decide it is empty."""
        store.save_draft(self.wid, {"title": "second thoughts"})
        self.assertEqual(store.get(self.wid)["draft"]["title"], fixtures.TITLE)


class ResetToDraftTests(unittest.TestCase):
    """Undoing the authoring pass so it can be re-run on the same capture."""

    def setUp(self):
        # History stamps have one-second resolution and a colliding stamp is
        # dropped, so the four saves below each get their own second.
        tick = iter(range(100))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = mock.patch.object(store, "datetime", wraps=datetime)
        fake = clock.start()
        fake.now.side_effect = lambda tz=None: base + timedelta(seconds=next(tick))
        self.addCleanup(clock.stop)
        self.alice = new_member()
        self.row = fixtures.make_walkthrough(self.alice, steps=1)
        self.wid = self.row["id"]
        self.page = "step-000/page.html"
        store.put_capture(self.wid, self.page, "<html>captured</html>")
        # an authoring pass, then an AI edit that rewrote the page twice
        store.save_with_history(self.wid, dict(self.row["definition"], title="Authored"))
        store.mark_authored(self.wid)
        store.save_with_history(self.wid, dict(self.row["definition"], title="Edited"),
                                changed_pages={self.page: "<html>edit 1</html>"})
        store.save_with_history(self.wid, dict(self.row["definition"], title="Edited"),
                                changed_pages={self.page: "<html>edit 2</html>"})
        store.save_render_check(self.wid, {"pages": {}})
        store.save_verify_check(self.wid, {"steps": []})

    def test_the_draft_is_live_again_and_the_card_is_back_in_the_queue(self):
        store.reset_to_draft(self.wid)
        row = store.get(self.wid)
        self.assertEqual(row["definition"], row["draft"])
        self.assertEqual(store.authoring_state(row), {})

    def test_each_page_gets_its_earliest_backup_the_one_as_captured(self):
        _, pages = store.reset_to_draft(self.wid)
        self.assertEqual(pages, 1)
        self.assertEqual(store.capture_text(self.wid, self.page), "<html>captured</html>")

    def test_the_verdicts_are_cleared_so_the_next_pass_rechecks(self):
        store.reset_to_draft(self.wid)
        row = store.get(self.wid)
        self.assertIsNone(row["render_check"])
        self.assertIsNone(row["verify_check"])

    def test_the_authored_version_stays_restorable(self):
        stamp, _ = store.reset_to_draft(self.wid)
        self.assertEqual(store.history_entry(self.wid, stamp)["definition"]["title"], "Edited")

    def test_a_walkthrough_with_no_draft_is_refused_untouched(self):
        db.execute("update walkthroughs set draft = null where id = %s", (self.wid,))
        with self.assertRaises(store.NoDraft):
            store.reset_to_draft(self.wid)
        self.assertTrue(store.authoring_state(store.get(self.wid)))

    def test_a_walkthrough_that_is_gone_raises(self):
        store.delete(self.wid)
        with self.assertRaises(store.NoSuchWalkthrough):
            store.reset_to_draft(self.wid)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.alice = new_member()

    def test_delete_removes_the_row_the_history_and_every_object(self):
        row = fixtures.make_walkthrough(self.alice, steps=2)
        store.save_with_history(row["id"], dict(row["definition"], title="Edited"))
        store.delete(row["id"])
        self.assertIsNone(store.get(row["id"]))
        self.assertEqual(store.history_entries(row["id"]), [])
        self.assertEqual(capturefiles.current().keys(), [])

    def test_delete_leaves_another_walkthroughs_objects_alone(self):
        """The prefix is `<uuid>/`, and uuids do not prefix one another — but
        the check that matters is that the delete is scoped at all."""
        mine = fixtures.make_walkthrough(self.alice, title="Mine")
        other = fixtures.make_walkthrough(self.alice, title="Other")
        store.delete(mine["id"])
        self.assertEqual(store.capture_files(other["id"]),
                         ["step-000/page.html", "step-001/page.html"])

    def test_deleting_a_walkthrough_that_is_gone_raises(self):
        with self.assertRaises(store.NoSuchWalkthrough):
            store.delete(str(uuid.uuid4()))

    def test_deleting_a_member_takes_their_walkthroughs_with_them(self):
        """`members.id references auth.users(id) on delete cascade`, and
        `walkthroughs.owner_id references members(id) on delete cascade`. That
        chain is what turned account deletion from two steps into one."""
        row = fixtures.make_walkthrough(self.alice)
        db.execute("delete from members where id = %s", (self.alice,))
        self.assertIsNone(store.get(row["id"]))


if __name__ == "__main__":
    unittest.main()
