"""Synthetic walkthrough fixtures for the test suite.

Tests must never depend on content that happens to be in the developer's own
data folder. Everything a test needs is built here: a member minted into the
throwaway database conftest.py created, a walkthrough row, and its capture
pages in the throwaway captures folder.

WHAT CHANGED WHEN THE STORES MOVED

`make_walkthrough` used to write `walkthroughs/<id>/walkthrough.json` and a
sibling `captures/<id>/` tree into a temp directory, and hand back the root for
a test to point `store.WALKTHROUGHS_DIR` at. There is no directory and no such
global any more: it inserts a row and puts objects, and hands back the row.

The other consequence is that the id is no longer chosen by the caller. It is a
uuid the database mints, so a test that wants to refer to a walkthrough holds
the value `make_walkthrough` returned rather than a slug it made up. That is the
whole point of the opaque id, and a fixture that faked a predictable one would
hide every place the old assumption survives.
"""
from captcher import auth  # noqa: F401  -- imported for its side effect of loading the session secret
from captcher import store

# Re-exported so a suite needs one import for "a member and a client".
from conftest import new_member, signed_in_client  # noqa: F401

SCHEMA = "walkthrough-def@1"

# Distinctive strings tests can assert on without coupling to real content.
TITLE = "Test Walkthrough"
MARKER = "Zephyr Widget 3x"              # appears in the walkthrough body
PAGE_MARKER = "Synthetic capture page"   # appears in the page snapshots


def page_html(step_label: str) -> str:
    """A minimal capture snapshot, shaped like a SingleFile page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{step_label}</title></head>
<body>
  <h1>{PAGE_MARKER}: {step_label}</h1>
  <table><tr class="clickable"><td>Marcus Osei</td></tr></table>
  <button id="save-btn">Save</button>
  <input id="points" type="text">
</body>
</html>
"""


def walkthrough_def(walkthrough_id: str = None, steps: int = 2,
                    title: str = TITLE) -> dict:
    """A definition that validates, with `steps` steps.

    No `capture_dir`: pages are addressed by their capture-relative path and
    the prefix is derived from the id.

    `walkthrough_id` is optional and defaults to absent, because the row's id is
    the identifier and the copy inside the definition is not. Production stamps
    it — `author._force_invariants` on the way in, `export` on the way out — so
    a definition stored without one is the ordinary case, not a broken fixture.
    Pass it when the test is about validation, which does require the field.
    """
    return {
        "schema": SCHEMA,
        **({"id": walkthrough_id} if walkthrough_id else {}),
        "title": title,
        "track": "Testing",
        "level": "beginner",
        "estimated_minutes": 3,
        "intro": {
            "scenario": f"A synthetic scenario mentioning {MARKER} for assertions.",
            "objectives": ["Do the first thing", "Do the second thing"],
        },
        "steps": [
            {
                "id": f"step-{i}",
                "title": f"Step {i} title",
                "page": f"step-{i:03d}/page.html",
                "instruction": f"Instruction for step {i}.",
                "showme_caption": f"Caption for step {i}.",
                "hint": f"Hint for step {i}.",
                "action": {
                    "type": "click",
                    "target": {"selector": "tr.clickable", "text": "Marcus Osei"},
                },
            }
            for i in range(steps)
        ],
        "completion": {
            "summary": "You finished the synthetic walkthrough.",
            "success_criteria": ["Reached the end"],
        },
    }


def make_walkthrough(owner_id, title: str = TITLE, steps: int = 2,
                     definition: dict = None):
    """Create a walkthrough row plus its capture pages. Returns the row.

    The row's `id` is the uuid the database minted — use it, do not compose one.

    `title` reaches the definition as well as the column, because that is what
    production does: `handle_upload` and the editor PUT both write a definition
    whose `title` is the row's. A fixture that set only the column produced a
    walkthrough whose exported page carried a different name from its catalog
    card — which is not a state the app can reach, and which quietly made every
    assertion about a rendered title test the wrong string.
    """
    row = store.create(owner_id, title,
                       definition or walkthrough_def(steps=steps, title=title))
    if definition is None:
        make_capture(row["id"], steps)
    return row


def make_capture(walkthrough_id, steps: int = 2):
    """Put `steps` page snapshots into the bucket for an existing walkthrough."""
    for i in range(steps):
        store.put_capture(walkthrough_id, f"step-{i:03d}/page.html",
                          page_html(f"Step {i}"))
