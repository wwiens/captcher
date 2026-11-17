"""A/B the Stage 3 authoring pass across models (and efforts) — read-only.

Runs `author.run_pass` on real captures from the configured project, once per
(walkthrough, model, effort) combination, and writes every result to
evals/out/<timestamp>/ instead of the database:

  - <id>__<model>__<effort>.json   the authored walkthrough plus run metrics
  - summary.md                     one table comparing all runs

Nothing is saved to the walkthrough row: `store.save_draft` and
`store.save_with_history` are replaced for the duration of the run, and an
already-authored walkthrough is re-authored from its preserved `draft` column
(read-only — the row is never changed). The captures and the database are only
read. Each run is a real, billed authoring pass.

    python3 evals/ab_author.py <walkthrough-id> [<walkthrough-id> ...]
    python3 evals/ab_author.py <id> --models claude-opus-5 claude-opus-5-5 \
        --efforts high xhigh --scenario "optional scenario intent"

Pick 2-3 captures that differ: a short form flow, a long multi-page one, and
one that used to cause trouble (truncation, loop exhaustion, bad hints).
"""
import argparse
import copy
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from captcher import author  # noqa: E402  (loads .env)
from captcher import store   # noqa: E402

# $/MTok: input, output, cache write (5-minute), cache read. Estimates for a
# side-by-side, not an invoice — check current pricing before quoting them.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5":   (5.00, 25.00, 6.25, 0.50),
}


def cost(model, u):
    p = PRICES.get(model)
    if not p:
        return None
    return round((u["input"] * p[0] + u["output"] * p[1] +
                  u["cache_write"] * p[2] + u["cache_read"] * p[3]) / 1e6, 4)


def run_one(wt_id, model, effort, scenario):
    row = store.get(wt_id)
    if row is None:
        raise SystemExit(f"No walkthrough {wt_id!r} in the configured project.")
    draft = row.get("draft") if store.authoring_state(row) else row.get("definition")
    if not draft:
        raise SystemExit(f"{wt_id!r} is authored and has no preserved draft to re-author.")
    fake_row = dict(row, definition=copy.deepcopy(draft), authored_at=None)

    turns, events, saved = [], [], {}
    real_send = author._send_request

    def recording_send(params):
        t0 = time.time()
        data = real_send(params)
        u = data.get("usage") or {}
        turns.append({
            "seconds": round(time.time() - t0, 1),
            "stop_reason": data.get("stop_reason"),
            "served_by": data.get("model"),
            "tools": [b.get("name") for b in data.get("content") or []
                      if b.get("type") == "tool_use"],
            "input": u.get("input_tokens") or 0,
            "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0,
        })
        return data

    def progress(phase, message):
        events.append([phase, message])
        print(f"    [{phase}] {message}")

    def capture_save(_id, definition, changed_pages=None):
        saved["walkthrough"] = definition
        return "dry-run"

    t0 = time.time()
    error = None
    with mock.patch.object(author, "_send_request", recording_send), \
            mock.patch.object(author, "AUTHOR_EFFORT", effort), \
            mock.patch.object(store, "get", lambda *_a, **_k: fake_row), \
            mock.patch.object(store, "save_draft", lambda *_a, **_k: None), \
            mock.patch.object(store, "save_with_history", capture_save):
        try:
            result = author.run_pass(wt_id, scenario=scenario, model=model,
                                     progress=progress)
        except author.AuthorError as e:
            result, error = None, str(e)
    usage = {k: sum(t[k] for t in turns)
             for k in ("input", "output", "cache_read", "cache_write")}
    return {
        "walkthrough_id": wt_id, "model": model, "effort": effort,
        "scenario": scenario, "error": error,
        "wall_seconds": round(time.time() - t0, 1),
        "turns": len(turns), "usage": usage, "est_cost_usd": cost(model, usage),
        "rejections": sum(1 for p, _ in events if p == "rejected"),
        "reviewed_warnings": any(p == "review" for p, _ in events),
        "salvaged": any(p == "salvaged" for p, _ in events),
        "steps_before": len(draft.get("steps", [])),
        "steps_after": (result or {}).get("steps_after"),
        "warnings": (result or {}).get("warnings"),
        "turn_log": turns, "events": events,
        "walkthrough": saved.get("walkthrough"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="+", help="walkthrough ids (uuids)")
    ap.add_argument("--models", nargs="+",
                    default=["claude-opus-5", "claude-opus-5-5"])
    ap.add_argument("--efforts", nargs="+", default=[author.AUTHOR_EFFORT])
    ap.add_argument("--scenario", default=None)
    args = ap.parse_args()

    out = ROOT / "evals" / "out" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for wt_id in args.ids:
        for model in args.models:
            for effort in args.efforts:
                print(f"== {wt_id} · {model} · effort={effort}")
                r = run_one(wt_id, model, effort, args.scenario)
                (out / f"{wt_id}__{model}__{effort}.json").write_text(
                    json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
                rows.append(r)
                print(f"   {'FAILED: ' + r['error'] if r['error'] else 'ok'} — "
                      f"{r['turns']} turns, {r['wall_seconds']}s, "
                      f"out={r['usage']['output']} tok, ~${r['est_cost_usd']}")

    lines = ["| walkthrough | model | effort | result | turns | rejected | warnings reviewed | "
             "steps | warnings | out tok | in tok | cache read | sec | est $ |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        status = ("error" if r["error"] else "salvaged" if r["salvaged"] else "ok")
        lines.append(
            f"| {r['walkthrough_id'][:8]} | {r['model']} | {r['effort']} | {status} | "
            f"{r['turns']} | {r['rejections']} | {'yes' if r['reviewed_warnings'] else 'no'} | "
            f"{r['steps_before']}→{r['steps_after']} | {len(r['warnings'] or [])} | "
            f"{r['usage']['output']:,} | {r['usage']['input']:,} | "
            f"{r['usage']['cache_read']:,} | {r['wall_seconds']} | {r['est_cost_usd']} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {len(rows)} run(s) to {out}")


if __name__ == "__main__":
    main()
