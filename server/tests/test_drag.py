"""The `drag` action: a card carried from one lane to another.

The recorder logs clicks, typing and selects, so a drag leaves no action —
only its result, a draggable element sitting in a different zone on the next
snapshot. The authoring pass used to bridge that gap by inventing a click on
whatever button also moved the card. These tests pin the pieces that let it
write the drag instead:

- the digest inventories draggable elements and the lanes they can land in,
  with the lane named by its heading and addressed by its own data-* handle,
- resolve_refs() accepts a drop-zone ref only as `drop_target`,
- validate() insists a drag has both ends and checks both against the page,
- ingest drafts a drag step from a recorder that reports drops,
- both prompts describe the action (the shared-block guard covers the rest).
"""
import unittest

from captcher import author
from captcher import digest
from captcher import ingest
from captcher import store
from conftest import new_member

from tests import fixtures

BOARD = """<!DOCTYPE html><html><head><title>Board</title></head><body>
<h1>Pipeline</h1>
<div class="board" id="board">
  <div class="col" data-stage="Proposal">
    <div class="colhead"><h3>Proposal</h3><span class="n">1</span></div>
    <article class="deal" draggable="true" data-id="15">
      <div class="co">Ironvale Steel</div><div class="amt">$26,500</div>
      <button class="adv" data-adv="15" aria-label="Move to next stage">›</button>
    </article>
  </div>
  <div class="col" data-stage="Negotiation">
    <div class="colhead"><h3>Negotiation</h3><span class="n">0</span></div>
  </div>
  <div class="col" data-stage="Closed Won">
    <div class="colhead"><h3>Closed Won</h3><span class="n">0</span></div>
  </div>
</div>
<a href="#" draggable="true">a link the browser could drag anyway</a>
</body></html>"""


def board_digest():
    tree = digest.parse_page(BOARD)
    block, entries = digest.page_digest("step-000/page.html", tree)
    return block, entries, tree


class DigestInventory(unittest.TestCase):
    def test_draggable_card_is_an_interactive_line(self):
        block, entries, _ = board_digest()
        card = [e for e in entries if e["tag"] == "article"]
        self.assertEqual(len(card), 1)
        self.assertEqual(card[0]["target"], {"selector": 'article[data-id="15"]'})
        self.assertFalse(card[0].get("dropzone"))
        self.assertIn('draggable "Ironvale Steel', block)

    def test_lanes_become_drop_zones_named_by_heading(self):
        block, entries, _ = board_digest()
        zones = [e for e in entries if e.get("dropzone")]
        self.assertEqual([z["name"] for z in zones],
                         ["Proposal", "Negotiation", "Closed Won"])
        self.assertEqual(zones[1]["target"], {"selector": 'div[data-stage="Negotiation"]'})
        self.assertIn("DROP ZONES", block)
        self.assertIn('dropzone "Negotiation"', block)
        # zones are e-refs (step-addressable), never d-refs
        self.assertTrue(all(z["ref"].startswith("e") for z in zones))

    def test_a_link_draggable_by_default_stays_a_link(self):
        block, entries, _ = board_digest()
        links = [e for e in entries if e["tag"] == "a"]
        self.assertEqual(len(links), 1)
        self.assertNotIn('draggable "a link', block)

    def test_a_drag_step_resolves_against_the_real_page(self):
        _, entries, tree = board_digest()
        card = next(e for e in entries if e["tag"] == "article")
        zone = next(e for e in entries if e.get("dropzone") and e["name"] == "Negotiation")
        self.assertEqual(len(digest.match(tree, card["target"])), 1)
        self.assertEqual(len(digest.match(tree, zone["target"])), 1)


class ResolveRefs(unittest.TestCase):
    def setUp(self):
        self.ref_table = {
            "e1": {"page": "p/page.html", "target": {"selector": 'article[data-id="15"]'},
                   "display": False, "dropzone": False},
            "e2": {"page": "p/page.html", "target": {"selector": 'div[data-stage="Negotiation"]'},
                   "display": False, "dropzone": True},
            "e3": {"page": "q/page.html", "target": {"selector": 'div[data-stage="Won"]'},
                   "display": False, "dropzone": True},
        }

    def step(self, action):
        return {"steps": [{"id": "s1", "page": "p/page.html", "action": action}]}

    def test_both_ends_are_substituted(self):
        wt = self.step({"type": "drag", "target": {"ref": "e1"}, "drop_target": {"ref": "e2"}})
        self.assertEqual(author.resolve_refs(wt, self.ref_table, {}), [])
        self.assertEqual(wt["steps"][0]["action"], {
            "type": "drag",
            "target": {"selector": 'article[data-id="15"]'},
            "drop_target": {"selector": 'div[data-stage="Negotiation"]'}})

    def test_a_drop_zone_cannot_be_a_target(self):
        for a_type in ("click", "drag"):
            with self.subTest(action=a_type):
                wt = self.step({"type": a_type, "target": {"ref": "e2"},
                                "drop_target": {"ref": "e2"}})
                errors = author.resolve_refs(wt, self.ref_table, {})
                self.assertEqual(len(errors), 1)
                self.assertIn("DROP ZONE", errors[0])

    def test_drop_target_on_another_page_is_an_error(self):
        wt = self.step({"type": "drag", "target": {"ref": "e1"}, "drop_target": {"ref": "e3"}})
        errors = author.resolve_refs(wt, self.ref_table, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("drop_target", errors[0])
        self.assertIn("q/page.html", errors[0])


class Validate(unittest.TestCase):
    def setUp(self):
        self.wt_id = fixtures.make_walkthrough(new_member(), steps=0)["id"]
        store.put_capture(self.wt_id, "step-000/page.html", BOARD)
        self.pages = {"step-000/page.html"}
        self.trees = {"step-000/page.html": digest.parse_page(BOARD)}

    def wt(self, action):
        wt = fixtures.walkthrough_def(self.wt_id, steps=1)
        wt["steps"] = [{"id": "move", "title": "Move it", "page": "step-000/page.html",
                        "instruction": "i", "showme_caption": "c", "hint": "h",
                        "action": action}]
        return wt

    def validate(self, action):
        return author.validate(self.wt(action), self.wt_id, self.pages,
                               trees=self.trees, alias_map={})

    def test_a_complete_drag_passes(self):
        errors, warnings = self.validate({
            "type": "drag", "target": {"selector": 'article[data-id="15"]'},
            "drop_target": {"selector": 'div[data-stage="Negotiation"]'}})
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_a_drag_without_a_drop_target_is_an_error(self):
        errors, _ = self.validate({"type": "drag",
                                   "target": {"selector": 'article[data-id="15"]'}})
        self.assertEqual(len(errors), 1)
        self.assertIn("drop_target", errors[0])

    def test_the_drop_target_is_resolved_like_the_target(self):
        _, warnings = self.validate({
            "type": "drag", "target": {"selector": 'article[data-id="15"]'},
            "drop_target": {"selector": 'div[data-stage="Nowhere"]'}})
        self.assertTrue(any("drop_target matches nothing" in w for w in warnings), warnings)
        _, warnings = self.validate({
            "type": "drag", "target": {"selector": 'article[data-id="15"]'},
            "drop_target": {"selector": "div.col"}})
        self.assertTrue(any("drop_target matches 3" in w for w in warnings), warnings)


class Ingest(unittest.TestCase):
    """convert() takes the parsed manifest and returns the definition; it
    writes nothing and there is no ingest.ROOT to redirect any more."""

    def test_a_recorded_drop_drafts_a_drag_step(self):
        draft = ingest.convert({
            "schema": ingest.CAPTURE_SCHEMA, "title": "Drag",
            "createdAt": "2026-09-05T00:00:00Z",
            "initialCapture": {"folder": "step-000"},
            "steps": [{"folder": "step-001", "files": {"html": "page.html"},
                       "actions": [
                           {"type": "drag", "element": "article", "label": "Ironvale Steel",
                            "selector": 'article[data-id="15"]',
                            "dropSelector": 'div[data-stage="Negotiation"]'},
                           {"type": "drag", "element": "article", "label": "no drop zone",
                            "selector": 'article[data-id="16"]'}]}],
        })
        actions = [s["action"] for s in draft["steps"]]
        self.assertEqual(actions[0], {
            "type": "drag", "target": {"selector": 'article[data-id="15"]'},
            "drop_target": {"selector": 'div[data-stage="Negotiation"]'}})
        # a drag with no recorded drop is not a step; the observe closes the draft
        self.assertEqual([a["type"] for a in actions], ["drag", "observe"])


class Prompts(unittest.TestCase):
    def test_the_authoring_pass_is_told_to_recover_drags(self):
        self.assertIn("RECOVER DRAGS", author.SYSTEM_PROMPT)
        self.assertIn("drop_target", author.SYSTEM_PROMPT)

    def test_the_edit_pass_can_turn_a_click_into_a_drag(self):
        self.assertIn('{"type": "drag"', author.EDIT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
