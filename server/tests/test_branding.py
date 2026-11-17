"""Which brand profile an export actually gets.

`resolve_branding_profile` is the one place that answers "whose brand is this
export wearing", and every route that can produce a file goes through it: the
Download dialog, `/embed/`, the SCORM builder. Its two lookups are stubbed
here, so what these tests pin is the branch order — which is where it went
wrong — rather than the database.
"""
import unittest
from unittest.mock import patch

from captcher import branding

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = {"id": "22222222-2222-2222-2222-222222222222", "brand_name": "Lumen Learn"}


class ResolveBrandingProfileTests(unittest.TestCase):
    def resolve(self, profile_id, owner_id=OWNER, active=OTHER["id"], stored=OTHER):
        """Resolve with the account's active profile stubbed to `active` and
        every lookup answered with `stored`."""
        with patch.object(branding, "active_profile_id", return_value=active), \
             patch.object(branding, "get_profile", return_value=stored):
            return branding.resolve_branding_profile(profile_id, owner_id)

    def test_asking_for_the_built_in_by_name_beats_the_active_profile(self):
        """The regression this guards. The Download dialog sends the built-in's
        id like any other (`o.value = p.id` in _catalog-script.html), and the
        resolver's only test was `profile_id != MYBRAND_ID` — so that id read as
        "nothing was requested" and it fell through to the active profile.
        Choosing "Shadow Capture — my brand" exported whichever profile you had
        last been editing, and looked like the dialog was ignored.
        """
        p = self.resolve(branding.MYBRAND_ID)
        self.assertEqual(p["id"], branding.MYBRAND_ID)
        self.assertTrue(p["read_only"])
        self.assertEqual(p["brand_name"], branding.DEFAULT_BRANDING["brand_name"])

    def test_a_requested_profile_still_wins(self):
        self.assertEqual(self.resolve(OTHER["id"])["id"], OTHER["id"])

    def test_no_request_falls_back_to_the_active_profile(self):
        self.assertEqual(self.resolve(None)["id"], OTHER["id"])

    def test_a_request_that_cannot_be_honoured_falls_through(self):
        """An /embed/ froze its profile id at publish time and that profile may
        since have been deleted — the export still has to produce a file."""
        p = self.resolve("33333333-3333-3333-3333-333333333333",
                         active=branding.MYBRAND_ID, stored=None)
        self.assertEqual(p["id"], branding.MYBRAND_ID)

    def test_a_signed_out_caller_gets_the_built_in(self):
        self.assertEqual(
            branding.resolve_branding_profile(None, owner_id=None)["id"],
            branding.MYBRAND_ID)

    def test_the_built_in_carries_our_assets_and_the_defaults_do_not(self):
        """DEFAULT_BRANDING is the base every customer profile starts from, so
        a logo URL left in it would ship our lockup inside someone else's
        white-label export."""
        p = branding.my_brand_profile()
        for field, url in branding.SHADOW_CAPTURE_ASSETS.items():
            with self.subTest(field=field):
                self.assertEqual(p[field], url)
                self.assertEqual(branding.DEFAULT_BRANDING[field], "")


if __name__ == "__main__":
    unittest.main()
