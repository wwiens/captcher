# Captcher — the authoring pipeline

The repeatable process for turning a raw screen capture into a published
interactive walkthrough. Five stages: **capture → ingest → AI authoring pass →
verify → publish**. Stages 1 and 2 are mechanical. Stage 3 is where the model —
or a human author — turns a raw action log into teaching content.

---

## Stage 1 — capture

Record the flow with the capture extension while performing it in the live (or
mock) product. The extension produces a ZIP containing:

- `walkthrough.json` — the action log (`page-capture-walkthrough@1` schema; the
  name refers to an earlier extension and is retained as the wire identifier,
  validated on ingest by `ingest.check_capture_schema`): every click, select and
  input, with element selectors and text
- `step-NNN/page.html` — the frozen DOM snapshot at each captured state, plus
  `screenshot.png` and `metadata.json`
- `auto/` — automatic snapshots taken around each action
- `recording.webm` — the screen and microphone recording

Three capture rules make the rest of the pipeline work:

1. Take a snapshot **before** every meaningful state change. The player shows the
   page as it was *before* the learner acts on it.
2. Perform one clean pass. No backtracking, no stray clicks — stray actions
   become garbage steps you delete later.
3. Narrate while recording. The audio is not used by the player, but it is the
   raw material for Show-me captions.

## Stage 2 — ingest (mechanical)

Ending a walkthrough sends the capture straight to `POST /api/upload`, which
ingests it and shows it in the catalog as a draft. `POST /api/upload` also
accepts a capture ZIP directly, from `curl` or a script, authenticated with a
capture token.

Ingest converts the action log into a draft definition and writes it as the
walkthrough's `definition` — and, once, as its `draft` column, which is the
pre-authoring text the authoring pass is later diffed against.

- Walks the capture steps and attaches each recorded action to the snapshot taken
  **before** it
- Guesses an action type (`click` / `select` / `input`, or `drag` from a recorder
  that reports drops) and a rough selector plus text match per action
- Emits one draft step per recorded action, plus a final `observe` step on the
  last page
- Stamps every human-facing field with `TODO`

The draft is deliberately dumb. It does not merge related actions, fix bad
selectors, or write any teaching content. That is stage 3.

**The order is: read the manifest, convert, create the row, extract into the
bucket.** A walkthrough's id is assigned by the database, so it does not exist
until the row does — and the capture's objects are keyed by that id. `convert()`
is a pure function of the manifest and writes nothing, which is also what makes
it testable without a database or a bucket. If the extraction fails, the row goes
with it.

`python3 ingest.py <capture.zip>` prints the definition a capture would convert
to and writes nothing. It is a dry run by construction: creating a walkthrough
means inserting a row owned by a member, and the command line has no member.

## Stage 3 — the AI authoring pass

**In the app (preferred).** Uploading a capture runs ingest, and the card then
offers **Author with AI**. The pass sends the draft and a digest of every
captured page to the Anthropic API, derives the scenario from the people and data
visible on those pages, validates the result, and writes the new definition
keeping the previous one in history. Needs `ANTHROPIC_API_KEY`; override the
model with `CAPTCHER_MODEL` (default `claude-sonnet-5-5`). Thinking depth is
`CAPTCHER_AUTHOR_EFFORT` (default `high`) for this pass and
`CAPTCHER_EDIT_EFFORT` (default `medium`) for AI edits. A submission that
validates with warnings but no errors is handed back to the model once for
review before it is accepted.

**Headless.** The scenario intent is optional and overrides the derived one:

```bash
python3 author.py <walkthrough-id> ["optional scenario intent"]
```

Validation failures write nothing. Warnings — a target text not found in the
captured page, steps playing on a page the render check called broken — surface
in the panel; check those first in stage 4.

### What the pass sends

The model does **not** see raw page HTML. It gets:

1. The current draft definition
2. A **digest** of every captured page: the page's visible-text outline plus an
   inventory of its interactive elements, each with a ref id (`e17`) and a
   selector the server has already verified unique against that page
3. Each unique page's capture-time screenshot, after its digest, because a digest
   knows an element exists but not where it sits on screen — hints are written
   from the screenshot, not from DOM order
4. The scenario intent

The model picks elements by ref (`"target": {"ref": "e17"}`) and the server
substitutes the verified selector before validating. A ref from a different page
than the step plays on is a hard error, which is how page assignment is
mechanically checked. A page the digest cannot handle degrades **alone** to
trimmed raw HTML, and only there does the model write a selector itself.

The pass runs as a tool loop: the model can validate work in progress, read raw
page HTML on demand, and must submit through a validation-gated tool — so
validation errors come back to the model for repair instead of aborting the run.
If it runs out of turns having already produced a version that validated cleanly,
that version is saved rather than the run being discarded. If nothing it produced
validated, nothing is written and the error names what was still wrong.

### Watching a pass, and stopping one

The catalog card shows what the pass is doing as it happens — which page it is
reading, when it validates, how many errors came back — and carries a **Stop**
button.

**Stopping is a request, not a kill switch.** The pass unwinds at its next step
boundary, so a model call already in flight finishes and is still billed. Closing
the tab does not stop a pass; reopening the catalog re-attaches to it. Under the
hood this is `POST /api/author?background=1`, which returns a job id, plus `GET
/api/jobs/<id>/events` — see "Jobs" in CLAUDE.md.

### The transformations, in order

**3a. Consolidate steps.** The action log over-records. Merge a `click` on a
`<select>` followed by a `select` action into one `select` step — the click that
opens the dropdown is not a teachable step. Delete stray actions: clicks on
non-interactive containers, duplicate clicks, focus clicks before typing. Expect
roughly 8 raw steps to become 5 to 7 teaching steps.

**3b. Fix pages.** Verify each step's `page` shows the state the learner acts on;
the ingest heuristic ("the page before the action") is sometimes off by one. Set
`page: null` on steps that continue on the same page, so the player does not
reload. The snapshots are **frozen** — clicking never opens a dialog or switches
a tab — so every step's target must already exist on the page loaded for that
step, and the first step acting inside a dialog loads the snapshot where the
dialog is already open.

**3b′. Recover drags.** The recorder logs clicks, typing and selects only, so a
drag leaves no action — just its result: a `draggable` element sitting in one
drop zone on a step's page and in another on the next snapshot. The digest lists
such elements and their lanes under a DROP ZONES heading, and the pass writes a
`drag` step rather than a click on whatever button also moves it. Show-me carries
a ghost of the element to the zone; You-try accepts a real drop inside it, native
or pointer, and reports anything else as wrong.

**3c. Pick targets by ref.** Choose each target from the element inventory of the
step's effective page and write `{"ref": "e17"}`. Selectors are hand-written only
for a page marked "raw HTML fallback"; there:

- Scope to a container: `select` → `.modal select`, `button` → `.modal .btn-danger`
- Prefer meaningful classes: `tr` → `tr.clickable`
- Clean the text match. Ingest concatenates cell text
  (`"Sandra Whitfields.whitfield@example.com"`); trim to a human-recognisable
  substring (`"Sandra Whitfield"`)
- Use `text_exact` when a substring would collide (plain `"Suspend"` also matches
  the "Suspend account" button)
- Keep text matching wherever possible — it survives re-captures better than
  brittle CSS paths

**3d. Write the teaching content.** Replace every `TODO` with three distinct
voices:

| Field | Voice | Example |
|---|---|---|
| `instruction` (You try) | Second person, task plus why | "This case came from the fraud team, so open the Reason code dropdown and choose FRAUD_REVIEW." |
| `showme_caption` (Show me) | Narrator "we", explains what and why | "This is a fraud-team referral, so we select FRAUD_REVIEW — Suspected fraud, under review." |
| `hint` | Concrete on-screen location | "The Suspend button is in the top right of the profile, between Edit contact info and Adjust points." |

Also:

- Rename step ids and titles semantically (`step-3` → `choose-reason`)
- Write `intro.scenario` as a realistic work situation with named fictional
  people, and 3 to 5 `objectives`
- Add `wrong_value_feedback` to `select` steps. Explain *why* the wrong choice
  does not fit the scenario; never write "incorrect"
- Improve `demo_value` on `input` steps to model good practice, and set a
  sensible `min_length`
- Make the final `observe` step an explicit verification checklist ("status chip
  reads Suspended, banner shows your reason code, button now says Reactivate")
- Write `completion.summary` and `success_criteria` as observable end states
- Fix the metadata: sentence-case `title`, correct `track`, realistic
  `estimated_minutes` (about 30 seconds per step, not the ingest guess), and
  delete the `_source` block

### The reusable prompt, for hand-authoring outside the pipeline

The automated pass sends digests and ref targets; a chat has neither, so a hand
run attaches the raw `page.html` files and writes selectors directly — the same
form the raw-HTML fallback path uses. Prefer `python3 author.py <id>` when the
server is available; this is the fallback.

> You are converting a draft interactive walkthrough into a published one.
> Attached: the draft definition (walkthrough-def@1 schema, fields marked TODO)
> and the captured `page.html` for each step.
>
> Scenario intent: *<one or two sentences: who the learner is, why they are doing
> this task, which named fictional member or campaign is involved>*
>
> 1. Consolidate steps: merge click-on-select plus select pairs into one select
>    step; delete stray clicks on non-interactive elements. Aim for 5 to 7
>    teaching steps.
> 2. Verify each step's `page` shows the pre-action state; use `null` for
>    same-page steps. The snapshots are frozen — a step's target must already
>    exist on the page it plays on.
> 3. Verify every selector uniquely matches in that step's page.html. Scope to
>    containers, prefer meaningful classes, trim text matches to clean
>    human-readable substrings, use `text_exact` when substrings collide.
> 4. Replace all TODOs. `instruction`: second person, task plus why.
>    `showme_caption`: narrator "we", explains the reasoning. `hint`: concrete
>    on-screen location. Add `wrong_value_feedback` on selects, realistic
>    `demo_value` on inputs. Rename ids and titles semantically. Write the intro
>    scenario and objectives, and the completion summary and success criteria, as
>    observable end states. Fix title casing, track, and estimated_minutes.
>    Remove `_source`.
> 5. Return the complete definition only.

### Editing a published walkthrough

Open the walkthrough and switch to **Edit** for step text. For anything that has
to change the captured pages too, use the AI edit — describe the change in plain
language ("make step 3's hint mention the red badge", "rename the member to
Jordan Lee throughout") and the model applies it with full snapshot context,
under the same validation as the authoring pass:

```bash
python3 author.py edit <walkthrough-id> "instruction"
```

The model returns exact find/replace operations, applied literally across every
snapshot — names, emails, ids, balances, labels — so the walkthrough's targets
stay consistent with the edited pages. The walkthrough is validated against the
edited pages *before* anything is written.

`screenshot.png` files are **not** edited. The player does not use them, but they
still show the original content, so a render check run after an edit compares
against a stale baseline.

Every save keeps the previous definition, and pre-edit copies of any changed
pages, in the walkthrough's history. The page copies are made *before* the
definition write and outside its transaction: Storage has no transactions, so a
crash after the copies leaves orphaned backup objects that cost storage, while a
crash the other way round would lose the only copy of the originals.

### The visual editor

Open a walkthrough and switch to **Edit**. The side panel edits the current
step's *text* — title, instruction, caption, hint — while the stage shows the
actual captured page with the step's target highlighted. **Play step** runs the
step's Show-me animation on a fresh page.

One setting is not per step: **Show-me cursor**, at the bottom of the playback
settings popup. The default *Dot* is the brand shape; pick *Arrow* when the
captured UI is busy enough that a dot reads as part of the product rather than as
a cursor.

Read the small label above that row — it says where your choice goes. In Edit
mode it is *Saved with this walkthrough*, and Save writes it. While simply
watching it is *This session only*: try the other shape freely, nothing is stored
and nobody else sees it. Switching into Edit drops a session try-out, so the
editor always shows what is actually saved.

Save validates server-side under the same rules as the AI passes. Revert reloads
the last saved state. For anything beyond step text — re-targeting, values, page
content — use the AI edit.

## Stage 4 — verify

Walkthroughs are read per request. No server restart is needed.

1. `python3 server.py`, then open the walkthrough
2. **Show me** end to end: every demonstration finds its target, and the captions
   read like narration
3. **You try** end to end: every correct action is accepted. Deliberately click
   wrong things and pick wrong dropdown values, and check the feedback and hints
4. Confirm the final observe step's checklist matches what the last page actually
   shows

If a target is not found, the selector does not match the captured DOM. Fix it in
3c terms, not by loosening it to a bare tag name.

Two optional checks, both on demand because each costs a vision call per unique
page, and both **advisory** — neither blocks a write:

- **Render check.** Renders each captured page headless through the live
  `/capture/` route and compares it with the capture-time screenshot. It is the
  only thing that knows whether an element the digest can see actually *renders*:
  a stylesheet that did not survive the capture leaves a present element showing
  as white space. Its verdicts feed both the author's warnings and the edit
  pass's prompt.
- **Replay verification.** Spotlights each step's target in the player and has
  vision judge whether it is visible, handing failures to one repair turn.

## Stage 5 — publish

- The walkthrough appears in its owner's catalog as soon as the row exists
- Its captured screens live under its uuid in the `captures` bucket. There is
  nothing to point at and nothing to keep in step: the prefix is the id
- Keep the capture manifest and `auto/` snapshots — they are the source of truth
  for re-authoring
- **Embedding is separate and opt-in.** Nothing is public until "Enable
  embedding" is switched on for that walkthrough. Naming a walkthrough in the
  examples configuration does not publish it
