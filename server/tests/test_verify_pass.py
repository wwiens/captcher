"""The replay-and-verify pass.

verify_pass replays a published walkthrough the way the player renders it —
bridge probe spotlights each step's target — and judges the result with one
vision call per page, then hands failures to the AI edit pass for a single
repair turn. All three expensive seams are stubbed here: `drive_pages` (the
Playwright driver), `author.call_claude_vision` (the verdict), and
`author.run_edit` (the repair).
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from captcher import author
from captcher import store
from captcher import verify_pass
from conftest import new_member

from tests import fixtures
from tests.test_render_check import PNG


def fake_driver(counts, calls=None):
    """A drive_pages stub: writes a real PNG per step and reports `counts`
    (step_id -> match count; missing ids default to 1)."""
    def drive(base_url, cookie_header, walkthrough_id, jobs, out_dir):
        if calls is not None:
            calls.append([rel for rel, _ in jobs])
        out = {}
        for _rel, steps in jobs:
            for sid, _step in steps:
                n = counts.get(sid, 1)
                shot = None
                if n >= 0:
                    shot = Path(out_dir) / f"{sid}.png"
                    shot.write_bytes(PNG)
                out[sid] = {"count": n, "shot": str(shot) if shot else None}
        return out
    return drive


def vision_verdict(oks):
    """A call_claude_vision stub answering per the step texts in the prompt."""
    def vision(system, content, model, **kwargs):
        ids = [b["text"].split("\n")[0].split(": ", 1)[1]
               for b in content
               if b.get("type") == "text" and b["text"].startswith("Step id: ")]
        return json.dumps({"steps": [
            {"id": sid, "ok": oks.get(sid, True),
             "problem": "" if oks.get(sid, True) else "the ring sits on blank space"}
            for sid in ids]})
    return vision


class StepJobs(unittest.TestCase):
    def test_groups_by_effective_page_and_skips_targetless_steps(self):
        wt = {"steps": [
            {"id": "a", "page": "step-000/page.html",
             "action": {"type": "click", "target": {"selector": "#x"}}},
            {"id": "b", "page": None,
             "action": {"type": "input", "target": {"selector": "#y"},
                        "min_length": 2, "demo_value": "hi"}},
            {"id": "c", "page": "step-001/page.html", "action": {"type": "observe"}},
            {"id": "d", "page": None,
             "action": {"type": "click", "target": {"selector": "#z"}}},
        ]}
        jobs = verify_pass.step_jobs(wt)
        self.assertEqual([(rel, [sid for sid, _ in steps]) for rel, steps in jobs],
                         [("step-000/page.html", ["a", "b"]),
                          ("step-001/page.html", ["d"])])


class RunVerify(unittest.TestCase):
    def setUp(self):
        self.owner = new_member()
        self.wt_id = fixtures.make_walkthrough(self.owner, steps=2)["id"]
        # The pass refuses a draft, and "draft or authored" is the
        # `authored_at` column's question now — not a substring scan for
        # "TODO", which could not tell one of ingest's placeholders from an
        # author legitimately writing the word.
        store.mark_authored(self.wt_id)

    def _run(self, driver, vision, run_edit=None, repair=True):
        patches = [
            mock.patch.object(verify_pass, "drive_pages", driver),
            mock.patch.object(author, "call_claude_vision", vision),
        ]
        if run_edit is not None:
            patches.append(mock.patch.object(author, "run_edit", run_edit))
        with patches[0], patches[1], (patches[2] if len(patches) > 2
                                      else mock.patch.object(author, "run_edit",
                                                             side_effect=AssertionError(
                                                                 "run_edit must not be called"))):
            return verify_pass.run_verify(self.wt_id, "http://x", repair=repair)

    def test_all_ok_writes_the_sidecar_and_never_repairs(self):
        results = self._run(fake_driver({}), vision_verdict({}))
        self.assertEqual({e["status"] for e in results["steps"].values()}, {"ok"})
        self.assertFalse(results["repaired"])
        stored = verify_pass.load_results(self.wt_id)
        self.assertEqual(stored["steps"], results["steps"])
        self.assertEqual(stored["schema"], verify_pass.SCHEMA)

    def test_a_drag_whose_drop_zone_matches_nothing_is_missing(self):
        wt = store.get(self.wt_id)["definition"]
        wt["steps"][1]["action"] = {"type": "drag",
                                    "target": {"selector": "article.deal"},
                                    "drop_target": {"selector": "div.gone"}}
        store.save_with_history(self.wt_id, wt)
        base = fake_driver({})

        def driver(*args, **kwargs):
            out = base(*args, **kwargs)
            out["step-0"]["drop_count"] = 1     # no drag: never consulted
            out["step-1"]["drop_count"] = 0     # the zone matched nothing
            return out

        results = self._run(driver, vision_verdict({}), repair=False)
        self.assertEqual(results["steps"]["step-0"]["status"], "ok")
        s1 = results["steps"]["step-1"]
        self.assertEqual(s1["status"], "missing")
        self.assertEqual(s1["drop_count"], 0)
        self.assertIn("drop zone", s1["problem"])
        self.assertIn("DROP ZONES",
                      verify_pass._repair_instruction({"step-1": s1}))

    def test_a_missing_target_triggers_one_repair_and_a_reverify(self):
        sweeps = []
        drivers = [fake_driver({"step-0": 0}, calls=sweeps),
                   fake_driver({}, calls=sweeps)]

        def driver(*a, **kw):
            return drivers[0 if len(sweeps) == 0 else 1](*a, **kw)

        edits = []

        def run_edit(wid, instruction, model=None, **kwargs):
            edits.append(instruction)
            return {"summary": "Retargeted step-0."}

        results = self._run(driver, vision_verdict({}), run_edit=run_edit)
        self.assertTrue(results["repaired"])
        self.assertEqual(results["repair_summary"], "Retargeted step-0.")
        self.assertEqual(len(sweeps), 2)   # verify, repair, re-verify
        self.assertEqual(len(edits), 1)
        self.assertIn("step-0", edits[0])
        self.assertIn("matched nothing", edits[0])
        self.assertIn("Change nothing else", edits[0])
        self.assertEqual({e["status"] for e in results["steps"].values()}, {"ok"})

    def test_the_repair_turn_cannot_answer_instead_of_repairing(self):
        sweeps = []
        drivers = [fake_driver({"step-0": 0}, calls=sweeps), fake_driver({}, calls=sweeps)]

        def driver(*a, **kw):
            return drivers[0 if len(sweeps) == 0 else 1](*a, **kw)

        seen = {}

        def run_edit(wid, instruction, model=None, **kwargs):
            seen.update(kwargs)
            return {"summary": "Retargeted step-0."}

        self._run(driver, vision_verdict({}), run_edit=run_edit)
        self.assertIs(seen.get("answerable"), False)

    def test_a_failed_repair_leaves_the_first_verdicts_standing(self):
        def run_edit(wid, instruction, model=None, **kwargs):
            raise author.AuthorError("AI output failed validation")

        results = self._run(fake_driver({}), vision_verdict({"step-1": False}),
                            run_edit=run_edit)
        self.assertFalse(results["repaired"])
        self.assertIn("failed validation", results["repair_error"])
        self.assertEqual(results["steps"]["step-1"]["status"], "flagged")
        self.assertIn("blank space", results["steps"]["step-1"]["problem"])
        self.assertEqual(results["steps"]["step-0"]["status"], "ok")

    def test_probe_and_judge_errors_never_trigger_a_repair(self):
        """States of the CHECK are not states of the walkthrough — repairing
        over them would thrash."""
        results = self._run(fake_driver({"step-0": -1, "step-1": -1}),
                            vision_verdict({}))
        self.assertEqual({e["status"] for e in results["steps"].values()},
                         {"error"})
        self.assertFalse(results["repaired"])

    def test_a_draft_is_refused(self):
        """A walkthrough with no `authored_at` has not been through the AI
        pass, and there is nothing worth replaying yet. The word "TODO"
        appearing in learner-facing text is no longer what decides this."""
        draft = fixtures.make_walkthrough(self.owner, title="Still a draft")["id"]
        with self.assertRaises(verify_pass.VerifyError) as cm:
            verify_pass.run_verify(draft, "http://x")
        self.assertIn("draft", str(cm.exception))

    def test_an_unknown_walkthrough_is_a_clean_error(self):
        with self.assertRaises(verify_pass.VerifyError):
            verify_pass.run_verify("not-a-uuid", "http://x")


if __name__ == "__main__":
    unittest.main()
