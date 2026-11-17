# Captcher — server

Interactive training walkthroughs built from captured product flows. A capture
extension records a real task; an AI pass turns it into a playable walkthrough;
the player replays it over frozen page snapshots.

This file is for agents working in this repository: where the boundaries are,
what is load-bearing, and which traps are easy to walk back into. README.md is
the front door; ../CONTRIBUTING.md covers setup, tests and style.

## Where things live

Everything lives in one data folder, `CAPTCHER_HOME` (default `~/.captcher`,
`config.py`): settings in `.env`, the cookie-signing key in `session_secret`,
structured data in `captcher.db` (SQLite), every captured screen under
`captures/<walkthrough id>/`, and, for an installed package, the copy of the
extension Chrome loads in `extension/`. Nothing is written beside the code.
`CAPTCHER_HOME` itself comes only from the real environment, since it says
where `.env` is.

| Module | Owns |
|---|---|
| `server.py` | HTTP only: routing, the auth gate, request/response shaping |
| `config.py` | where the data folder, database file and captures folder are |
| `db.py` | SQLite — the one owner of the connection; schema in `schema.sql` |
| `blobs.py` | capture files under `captures/`, with containment checks and atomic writes |
| `store.py` | walkthroughs: the row, its history, and its captured objects |
| `auth.py` | the local owner, per-device capture tokens, one-time pairing codes, signed cookies, the optional access token |
| `envfile.py` | reading `CAPTCHER_HOME/.env` |
| `cli.py` | the `captcher` command: serve, extension, install-browser, token, devices, revoke |
| `install.py` | first run (data folder, commented `.env`), the extension copy, whether Playwright is ready |
| `ingest.py` | capture ZIP → draft definition; owns the wire schema |
| `jobs.py` | per-walkthrough exclusion, progress events, cancellation, deadlines |
| `author.py` | the Claude authoring and edit passes, and their prompts |
| `digest.py` | page digests: parse tree, verified-unique selectors, the player-faithful matcher |
| `langsmith_trace.py` | optional tracing around the Claude calls |
| `render_check.py` | render-accuracy checks: each captured page re-rendered headless vs its capture-time screenshot, judged by vision |
| `verify_pass.py` | replay verification: each step spotlighted in the player, judged by vision, failures handed to one repair turn |
| `brandfetch.py` | SSRF-guarded public URL fetch and brand inference |
| `branding.py` | brand profiles, Google Fonts, validation |
| `export.py` | standalone HTML / folder / zip exports |
| `scorm.py` | SCORM 1.2 and 2004 packaging over `export.py` |
| `templates.py` | file-backed templates; `script_json()` for JSON in `<script>` |
| `library.py` | collections — per-account, ordered sets of walkthroughs |
| `paths.py` | `is_within()` / `safe_join()` — filesystem containment |
| `thumbs.py` | one downscaled catalog card image per walkthrough |
| `usage.py` | `ai_usage`: one row per billed Claude API attempt, attributed to an operation, walkthrough, member and job; the price table and the AI spend page (`/usage`) |
| `diagnostics.py` | `walkthrough_logs`: the recorder's capture log per upload, a record (request as sent, exact diff) per AI edit attempt, and one per authoring pass (warnings, full error, progress trail). Authoring and edit records are shown, without diffs or tracebacks, in the catalog menu's "AI log" (`GET /api/walkthroughs/<id>/logs`); the menu always offers it, and Review capture; the card's status strip after a pass shows only whether it worked, with a link to the log |

**Nothing imports `server`.** Keep it that way; `export.py` and `scorm.py` once
lazily imported it to reach branding, and it was a cycle.

**`envfile` exists so the data layer does not import the AI client.**
`load_env` lives in `envfile.py` rather than `author.py` because importing `author` pulls in the
Anthropic SDK, so `db.py` could not be imported or tested without the whole
authoring stack behind it.

### Module names that must not change

- **`library.py` must never be renamed `collections.py`.** The repo root is
  `sys.path[0]` for both `python3 server.py` and the test suite, so a root-level
  `collections.py` shadows the stdlib `collections` package project-wide —
  taking `json`, `dataclasses`, `email`, `http.server` and `unittest` with it,
  with tracebacks that appear to come from inside Python. Nothing in CI catches
  stdlib shadowing;
  `tests/test_library_store.py::test_this_module_is_not_named_collections` is
  the only guard. (`branding.py` separately uses "collection" for its bundle of
  brand profiles. Two meanings coexist on purpose — do not unify the
  vocabulary.)
## The database

`db.py` keeps one SQLite connection per thread (the server is a
ThreadingHTTPServer) in WAL mode, with foreign keys on and a busy timeout.
Ownership is enforced in Python: every query touching member-owned data takes
an owner id and filters on it in SQL.

Things that follow, and are easy to break:

- **SQL keeps `%s` placeholders.** `db.py` rewrites them to `?`. Never write a
  literal `%s` in SQL; `%Y`/`%M` in a strftime format are fine.
- **Types come back by column name.** JSON columns (`db.JSON_COLUMNS`) are
  decoded to dicts, every `*_at` column to an aware UTC datetime, and
  `db.BOOL_COLUMNS` to bools. A new JSON or boolean column must be added to
  the matching set; a new timestamp column must end in `_at`. Write JSON with
  `db.Json(value)`.
- **Timestamps are fixed-width UTC text** (`YYYY-MM-DDTHH:MM:SS.mmmZ`), from the
  schema's defaults and from `now()`, which `db.py` registers as a SQL
  function. They compare correctly as text only because the width is fixed;
  pass datetimes as parameters (the adapter formats them) rather than
  building strings.
- **A transaction is `with db.connection() as conn, conn.cursor() as cur:`**,
  which runs `BEGIN IMMEDIATE`: the write lock is taken up front, which is what
  `select ... for update` did on Postgres.
- **Lists in `in (...)` use `db.placeholders(values)`.** SQLite accepts an
  empty list, which is the "match nothing" case.
- **`db.execute` returns a rowcount, and that is the useful half.**
  `execute(...) == 0` after an UPDATE with an owner predicate is how the stores
  tell "no such row" from "not yours" without a second SELECT. Both answers are
  the same 404 to the caller, deliberately, so the API cannot be used to probe
  for ids.
- **Schema changes go in `db.UPGRADES`**, keyed by the version they produce,
  never by editing `schema.sql` alone: an existing database is only ever
  upgraded, and `PRAGMA user_version` records how far.

## Walkthroughs and their captures

A walkthrough is **one row**, not a definition file plus a capture folder. Its
definition, draft, render and verify verdicts, and embed publication are columns
on it; its version history is a second table; its captured screens are files
under `captures/<the walkthrough's uuid>/`.

- **The id is an opaque uuid.** The title slug is a separate column that nothing
  addresses by, unique per owner rather than globally, so two customers may both
  have `log-a-ticket`. Route ids are never run through `slugify()` — that
  mangles a uuid into a 404. `slugify` survives only where a *title* becomes a
  slug.
- **`capture_dir` does not exist.** The prefix is the id, and
  `blobs.prefix_for()` is the only code that knows the layout. Do not
  reintroduce a stored path: the previous one was relative to the file that
  referenced it, five call sites re-derived it independently, and a capture
  folder could carry a different name from the walkthrough pointing at it.
- **`blobs.key_for()` is the containment check** for anything arriving off the
  `/capture/` route: a `..`, `.` or empty segment, a backslash or a NUL is
  rejected. Every file path is then resolved through `paths.safe_join` against
  the captures folder as well, so a caller that skips `key_for` still cannot
  escape it.
- **Saves are atomic and ordered to fail safe.** `store.save_with_history`
  copies the pre-edit pages into the history prefix *first*, outside the
  transaction, then writes the definition and the history row together, then
  writes the new page bytes. Files have no transactions; a crash after the
  copies leaves orphaned backup objects, which cost storage, while a crash the
  other way round would lose the only copy of the originals.
- **`store.delete` clears the capture folder before deleting the row.** An
  interrupted delete then leaves a walkthrough whose capture is missing, which
  the catalog reports and the owner can delete again. The reverse order leaves
  unreachable objects paying for themselves forever.
- **Authoring state is a column, not something derived from the content.**
  Counting "TODO" cannot tell a leftover placeholder from the word appearing
  legitimately in authored text, which strands a card in the queue with no way
  to clear it.

## Who owns what

**There are no accounts.** Everything belongs to one local owner, a fixed row in
`members` (`auth.LOCAL_OWNER_ID`) that the server creates for itself
(`auth.ensure_local_owner`, at startup and lazily on first use). The `owner_id`
columns and every owner filter stay, so ownership stays tested and a multi-user
mode remains possible.

- **`Handler.current_user()` decides.** A valid signed cookie names its member
  (tests mint these to act as other members). Without one, a browser request is
  the local owner — unless `CAPTCHER_ACCESS_TOKEN` is set, when the cookie from
  `/unlock` is required and there is no default.
- **A Bearer request is the extension, never the browser.** `current_user()`
  returns None whenever an `Authorization: Bearer` header is present, so a
  capture token cannot pass the browser-only routes (pairing, `/api/token`,
  device revocation, branding, collections) and mint its own replacements.
  `tests/test_pairing.py::PairingEndpointTests::test_pairing_requires_a_session_not_a_capture_token`
  pins it.
- **Request safety replaces the login.** On loopback, `host_allowed()` refuses
  any Host header that is not this server's (DNS rebinding), and
  `cross_site_write()` refuses POST/PUT/DELETE that a browser marks as
  cross-site via `Sec-Fetch-Site` or `Origin` (CSRF). Bearer requests and
  `/api/extension/claim` are exempt. Both run before any owner is resolved.
- **Listening beyond loopback needs `CAPTCHER_ACCESS_TOKEN`.** `main()` exits
  otherwise. In that mode the Host check is off, since the cookie is the
  protection.
- **conftest empties `members` itself between tests**, along with every other
  table, so a test that mints a member never leaks it into the next.

## Environment

Every knob is `CAPTCHER_*`, except the Anthropic, LangSmith and Google Fonts
variables, which keep their vendors' names. **There is no prefix-compatibility
layer**: each knob is read under the name in `.env.example` and no other. The
old `SHADOWCAPTURE_*` names are not read; `server.legacy_settings()` makes the
startup banner warn when one is still set. Add new ones under `CAPTCHER_`.

`captcher` creates the folder and a commented `.env` on first run
(`install.prepare`). `captcher --dev` (or `CAPTCHER_DEBUG=1`) is developer mode:
restart on `.py` changes, templates re-read per request, tracebacks in error
responses. It is off by default.

The wheel carries the extension at `captcher/extension/` (`hatch_build.py`;
skipped for editable installs), and `install.extension_folder()` copies it to
`CAPTCHER_HOME/extension` whenever the bundled files change. A source checkout
uses `../extension` directly.

`.env` is read once at import by `envfile.load_env()`. It fills in only names
*absent* from the environment and reads top-down, so a real shell variable beats
the file entirely and the **first** occurrence of a duplicated key wins.

## Brand

The visual system is **Captcher Brand Guide v1**, summarised by the rules
below; read them before touching anything visual or any user-facing string.
Shared tokens live in `static/captcher.css`, which
every server-rendered page links; `templates/_tokens.css` is deliberately empty
and is inlined only so `server._chrome()` and the auth pages have a splice point.

**Light is home.** `:root` holds the light palette and `[data-sc-theme="dark"]`
the derived dark one. The product name is **Captcher**, title case.

| Token | Value | Job |
|---|---|---|
| Indigo | `#3B49C6` | the lead, primary actions, links, and **You try** |
| Sky | `#29B6FF`, text `#0A6FA8` | **Show me**, AI features, info, progress, the hint |
| Mint | `#3ECFA2`, text `#0B7A5A` | checked, correct, published |
| Coral | `#FF6A5C`, text `#B83227` | recording (a dot — UI v2), alerts, try again |
| Ink / Slate / Line / Mist / Paper | `#14151A` / `#4B4F5A` / `#E6E7EC` / `#F2F3F7` / `#FAFAF9` | type, body, hairlines, app ground, page ground |

Rules that constrain code, not just design:

- **No purple, ever.** Warren retired Violet everywhere (2026-09-25): the app,
  the players, the recorder rings, the logo and the marketing site. Do not
  reintroduce a violet, purple or lavender value, including tints.
  `tests/test_captcher_assets.py::test_no_purple_anywhere` enforces it.
- **The hue contract is the product.** Indigo is You try, Sky is Show me, and
  a learner knows the mode before reading a word (the Show me switch is also
  outlined while You try is filled). A hue is assigned, never
  decorative. The four hues appear together only in the logo mark.
- **UI v2: squares belong to the logo alone.** Warren retired the blocky v1
  application (2026-09-25): controls 12px / cards 18px / dialogs 24px radius,
  pill search fields, chips and segmented controls, Outfit 600 headings
  (`--weight-heading`; the wordmark stays Black), thin line icons in the
  sidebar (`.nav-icon`, inline SVG in `_toolbar.html`), tinted `.notice`s with no left stripe, round `.dot`s, and
  a coral `.rec-dot` for a live capture. No stepping-squares motif, no square
  nav markers, no square email row. `tests/test_captcher_ui.py` pins this.
- **Bright hues never carry text on light grounds.** Fill with Ink text (Mint,
  Sky, Coral) or use the deep step. Every text pairing must clear 4.5:1; form
  control boundaries use `--control-border` (3:1), not the soft hairline.
- **Success is never derived from the accent.** In the player the accent means
  "look here" and success means "you were right"; collapsing the two makes a
  hint look like a pass. `branding.BRANDING_COLORS` says so in the field help,
  because a white-label profile can set them independently.
- **Brand governs chrome only.** Nothing may be composited onto a captured page
  except the cursor and the spotlight ring. `static/bridge.js` is the only code
  allowed to put brand inside a captured page. `#sim`/`#sim-wrap` keep a literal
  `#fff` rather than `var(--surface)` for the same reason `--stage` exists: a
  white-label surface colour must not bleed into the captured page. Frames
  around captured screens, and the spotlight ring, stay square.
- **The mark's squares are Coral, Sky, Indigo, Mint, Coral** (recoloured once,
  2026-09-25, to drop Violet; do not change them again without Warren). The
  frame follows the chrome (`currentColor`); the squares keep their colours. On
  the Indigo tile the Indigo centre square is white. The embed launcher on a
  customer-set fill is the one mono exception.
- **Depth is a surface plus a hairline;** one soft shadow is allowed for
  surfaces that float (dialogs, popovers, the popup step card) and, in UI v2,
  for a library card on hover (`--shadow-soft`).
- **Status never carries meaning by colour alone** — always word plus fill.
- **Sentence case for every heading, button and nav item.** Title Case appears
  nowhere except the product name. No emoji, anywhere, ever. Labels are written
  lowercase in source and uppercased in CSS, so they still read correctly if
  the transform is dropped.
- **Some features are out of scope on purpose:** sign-in, transcripts, captions,
  analytics, storage plans, trash and search.

**CSS variable names in `player.css` are white-label slots, not colour names.**
`--primary`, `--accent`, `--bg`, `--surface`, `--text`, `--muted`, `--border`,
`--success`, `--error` map 1:1 onto a saved brand profile's keys via
`export.apply_branding`'s `token_map`. Renaming one without changing that map
silently stops white-label exports from theming. `--stage` is deliberately not
in that map. The defaults equal `branding.DEFAULT_BRANDING["colors"]`.

### Fonts

**DM Sans (interface) and Outfit (headlines, wordmark), vendored as woff2 in
`static/fonts/`, never a CDN.** JetBrains Mono and Sora stay bundled for saved
brand profiles and machine-output text. They are inlined as base64 into every
standalone export, which is what the air-gapped-LMS claim rests on: a SCORM
package has to run with no internet. DM Sans and Outfit are each one variable
file declared once with a weight range, so each is inlined once (UI v2 collapsed
Outfit's five per-weight faces, which were the same bytes, into
`outfit-latin-900.woff2` at `100 900` so the 600 headings are a true 600; the
other `outfit-latin-*.woff2` files are no longer referenced).

- `branding._INLINED_FONTS` is the set of families needing no Google URL. It
  exists **twice** — there and as `INLINED_FONTS` in `templates/branding.html` —
  and `tests/test_font_url_parity.py` runs both under node to pin them together.
  That duplication is deliberate: the branding preview recomputes the URL on
  every keystroke, so a round trip would be wrong.
- **A custom-branded export is not air-gapped** for other families: they come
  from `fonts.googleapis.com` at play time. See TODO.md.

### The Show-me cursor is per-walkthrough

An optional top-level `cursor` field, chosen in the player's settings modal:
`"dot"` (the default, and what an absent field means) or `"pointer"` for
captured UIs where a dot reads as part of the product. Both are drawn purely in
CSS — the arrow as a `background-image` data URI, not an inline `<svg>` — so a
captured page's own `svg { … }` rules cannot reach inside them.

Four places must agree on the set of shapes and `tests/test_cursor_style.py`
pins them: `author.CURSOR_STYLES`, the `br-*` classes in `static/bridge.js`, the
`#opt-cursor` `<select>` in `static/player.html`, and `bridgeConfig()` in
`static/player.js` (the single builder both send sites use — do not hand-roll a
second `{cmd:"config"}` payload). An unknown value is a save *error*, not a
warning: a warning still writes, and a shape the bridge cannot draw leaves
Show-me with no cursor at all.

It is the one thing in the settings modal that is not a viewer preference.
Everything else there persists to `localStorage`; this belongs to the
walkthrough. `cursorOverride` is deliberately not persisted, entering edit mode
clears it, and `effectiveCursor()` resolves override → walkthrough → `"dot"`.

## Dependencies and runtime

Third-party runtime dependencies are allowed; declare them in
`pyproject.toml`. Keep the list short: a self-hosted tool is installed by
people who did not choose its dependencies. (The LangSmith client talks REST
over `urllib` rather than using the SDK, for that reason.)

Current runtime deps: **`anthropic`** and **`Pillow`** (optional at runtime —
without it `thumbs.py` serves the full-size frame). The database is the
standard library's `sqlite3`. The `[verify]` extra adds **`playwright`**
(headless Chromium for `render_check.py` and `verify_pass.py`; the browser
itself is a one-time `python -m playwright install chromium`, the import is
lazy, and the server runs fine without it).

The HTTP layer is a hand-rolled `ThreadingHTTPServer` and templating is
`__PLACEHOLDER__` substitution. Swapping either for a framework is a deliberate
change with its own migration, not a cleanup to do in passing.

Dev-only tooling (pytest, ruff) is the `[dev]` extra.

## Jobs

Every long-running AI pass — authoring, AI edit, render check, replay verify —
is claimed as a **job** (`jobs.py`) before it starts.

**Exclusion is per walkthrough id, not per (id, kind).** One active job per
walkthrough, whatever its kind, makes it safe by construction for a check and a
pass to share state rather than by remembering to compose keys. `save`, `delete`
and `restore` take the same claim and hold it for the duration of the write.

**Cancellation is cooperative, and that is not a limitation to be fixed.** The
passes take an optional `progress(phase, message)` callable and call it at their
own boundaries; `Job.progress` raises `jobs.Cancelled` from inside that call
once the flag is set or the deadline has passed. A cancel lands at the *next*
boundary: a model request already in flight completes and is still billed. Say
so in the UI rather than implying a hard stop; the alternative is killing a
thread mid-write.

`progress` defaults to a no-op everywhere, which is what keeps the passes
callable outside the server.

**Two response shapes, opted into per request.** Without `?background=1` a POST
runs the pass in the request thread and returns the finished JSON — the shape
the review page's two checks rely on. With it, the POST returns
`202 {job_id}` and the client watches `GET /api/jobs/<id>/events` (SSE). The
catalog's authoring pass and the editor's AI assist tab use the background
shape.

**An AI edit can end without editing.** `run_edit` offers `answer_author`
beside `submit_edit`: a message that asks a question is answered, nothing is
saved, and the result carries `kind: "answer"` with no stamp (so no undo).
The verify pass's repair turn passes `answerable=False` — its instruction is
never a question. `tests/test_ai_assist.py` pins the loop and the editor tab.

- `GET /api/jobs` lists what *this member* has running, which is how a reopened
  catalog tab re-attaches instead of offering a button that would start a second
  pass.
- `POST /api/jobs/<id>/cancel` answers `cancel_requested`, not "stopped".
- A job stays readable by id for `RETENTION_SECONDS` after it finishes, so a
  dropped stream can still collect the result.
- Job ownership is checked on every read and on cancel, and "no such job" and
  "not yours" deliberately return the same 404.

**`watch_and_reload` defers to running jobs.** In debug mode it used to
`os.execv` the moment a watched file changed, killing any pass in flight — and
editing `author.py` mid-pass is exactly what someone iterating on prompts does.

## Observability

Every model call goes through **`author.call_claude`** (or the agentic loop
`author.call_claude_agentic`, which shares its `_send_request` seam). That is the
single place to instrument, and `langsmith_trace.py` is what does it: an `llm`
run per request, nested under a `chain` run for the operation that asked for it.

**Spend is recorded at `author._request`**, after `_send_request` returns and
before anything inspects the reply, so a refused or truncated response — still
billed — is counted. `usage.record` never raises. Attribution is a context
variable set by `usage.attribute()` where work starts (`server.run_as_job`,
the brand import); a new thread pool around a model call must run each task in
a `contextvars.copy_context()` taken in the parent, as `render_check` does, or
its calls land as `other`. `ai_usage` has no foreign keys on purpose: spend
outlives the walkthrough it was spent on. Prices live in `usage.PRICES`, are
applied at read time, and must be updated by hand when Anthropic's change.

Three properties are load-bearing, because this sits in the path of a paid call
that can run for minutes:

- **Opt-in.** Off unless `LANGSMITH_TRACING` is truthy *and* `LANGSMITH_API_KEY`
  is set. A flag with no key is off and **silent** — a missing observability
  backend must never stop someone authoring a walkthrough, and a warning per
  call would bury the output that matters.
- **Non-blocking.** One background daemon thread draining one FIFO queue. The
  single worker is what guarantees a run's `PATCH` cannot overtake its `POST`;
  do not parallelise it. `flush()` is registered with `atexit` because `python3
  author.py <id>` exits the moment the pass returns.
- **Silent on failure.** Nothing raises into its caller, and the first transport
  failure prints one line and then goes quiet.

Two details that look like details and are not:

- **Traced strings are clipped** to `CAPTCHER_LANGSMITH_MAX_CHARS` (20K).
  The prompts are captured *customer* pages — one pass can carry close to a
  million tokens of third-party HTML — so this is a disclosure budget, not a
  bandwidth one. Raise it knowing what leaves the machine.
- **`ls_model_name` in the run metadata is what LangSmith prices a custom LLM
  run against.** Drop it and tokens still arrive but every cost reads zero.

The parent spans start *after* the precondition checks in `run_pass` /
`run_edit`: those failures never reach the model, and tracing them leaves
childless runs cluttering the project.

## The authoring digest

The authoring and edit passes do **not** send captured HTML to the model. A
13-page capture prepared to 2.1M chars (about 1M tokens, over budget); its
visible text was 43K chars. `build_page_digests` sends instead, per *unique*
page (byte-identical duplicates collapse via `unique_pages`, with the alias list
shown so every recorded path stays a valid `page` value): the visible-text
outline plus an inventory of interactive elements, each with a ref id and a
selector `digest.target_for()` generated and **verified unique** against its own
parse tree. The model returns `{"ref": "e17"}` targets; `resolve_refs()`
substitutes the stored target before `validate()` — an unknown ref, or a ref from
a different page than the step plays on, is an *error*.

Rules that hold this together:

- **`digest.match()` implements exactly the grammar `target_for()` emits and
  returns `None` on anything else.** Extend the generator only together with the
  matcher, and both only with `bridge.js` `targetMatches()` semantics — the
  player is the source of truth and `tests/test_digest.py` pins the parity.
- **A page the digest cannot handle degrades, alone**, to prepared HTML capped
  at `FALLBACK_PAGE_CHARS`; the model writes selectors directly for that page
  only. `prepare_page` and its pruning exist for this path — not dead code.
- **The recorder's selector ladder (`cssFor` in the extension's `recorder.js`)
  deliberately duplicates `digest.target_for()`** — one runs in the live DOM, one
  on the server's parse tree; change them in lockstep. `ingest.guess_target`
  prefers a recorded selector because the recorder verified it with
  `querySelectorAll` at event time.
- **A target may carry `frames`** — one verified hop selector per captured
  same-origin iframe level (they arrive inlined as `srcdoc`; see FIELD-NOTES.md,
  HubSpot). Every layer descends identically, `querySelector` semantics per hop
  (first match, no text filter): the recorder's `framePath`, `digest.match()`,
  and `bridge.js` `contextDocFor()`. The bridge can only reach a frame because
  serve and export rewrite SingleFile's exact sandbox stamp to add
  `allow-same-origin` (`export.unlock_frames`, also applied byte-wise in
  `send_file`); remove that and every frame target silently stops resolving.
  `tests/test_frame_targets.py` pins the chain.
- **`preflight_prompt_size` stays** even though digests never trip it: it guards
  the fallback path.

### The tool loop

`run_pass` and `run_edit` are tool loops. The model gets tools and the pass ends
only when a **submit tool validates clean** (`call_claude_agentic`, max 8
iterations). `validate_walkthrough` runs the exact `resolve_refs` +
`_force_invariants` + `validate()` chain the save runs, so the model repairs its
own errors inside one run instead of the author re-paying for a re-run;
`read_page_html` returns excerpt windows of prepared snapshot HTML;
`view_screenshot` serves pages past the inline cap; `verify_capture_edit` counts
a proposed `find` string before an edit is submitted. Tests script the model side
at the `_send_request` seam.

**Loop exhaustion salvages.** Eight turns of Opus with inlined screenshots is the
most expensive thing this server does, and running out of turns used to discard
all of it — including runs where `validate_walkthrough` had already returned zero
errors and the model then spent its remaining turns polishing wording.
`_salvage_or_raise` writes that candidate instead. This is not "write it anyway":
the salvaged copy went through the same `_check` a submit runs, and
`LoopExhausted` carries the *actual* remaining errors.

A run with nothing to salvage is usually one that never submitted at all: it
spent every turn reading pages, typically on an edit that needs a capture edit
it cannot find. The model is not told its budget up front, so the loop appends
a warning to the tool results `LOOP_WARNING_TURNS_LEFT` (2) turns before the
end — submit now, or `answer_author` if the change cannot be made — and
`LoopExhausted.tools_called` lets `_salvage_or_raise` say "never submitted"
rather than "nothing validated" when no `CHECK_TOOLS` call was made.

### Shared prompt text

Both system prompts end with a schema reference, and the step/action half of it
is one string (`STEP_SCHEMA` + `SELECT_SEMANTICS`, composed by
`schema_reference()`). Nothing parses a prompt, so a schema change landing in
`validate()` and in only one prompt fails silently.
`tests/test_prompt_shared_blocks.py` pins the sharing.

**The prose rules are deliberately not shared.** Frozen pages, target picking,
the three voices and merged runs are each stated twice, differently: one pass
builds a walkthrough, the other preserves one while changing part of it. Merging
them would mean rewriting a live prompt to suit a refactor. Change one of those
rules and you still have to change it in both places by hand — that is the known
cost.

The authoring prompt carries each unique page's capture-time screenshot after
its digest (`MAX_INLINE_SCREENSHOTS`), because a digest knows an element exists
but not where it sits on screen — hints are written from the screenshot, not DOM
order.

### What the digest cannot see

A digest is parsed from HTML, so it knows an element exists but not whether it
is *visible*: a stylesheet that did not survive the capture leaves a perfectly
present element rendering as white space. `render_check.py` is the only thing
that knows, and `author.render_notes()` reads its verdicts back into two places:
`validate()` warns when steps play on a page the check called `broken` (one
warning per page listing the steps, never one per step), and `run_edit`'s prompt
gets a RENDER ACCURACY block.

- **Both are advisory and neither blocks a write.** A verdict is per-*page* prose
  from a vision comparison with no element-level localisation.
- **Two thresholds.** `RENDER_NOTE_SEVERITIES` (minor + broken) is what the model
  is told; `RENDER_WARN_SEVERITIES` (broken only) is what the author is warned
  about. Prompt context is nearly free; a warning has to be rare enough to be
  read. `no-baseline`/`pending`/`error` are states of the *check*, not of the
  page, and appear in neither.
- **`run_pass` gets both when a verdict exists.** The check stays on demand (it
  costs a vision call per unique page), but `POST /api/author` accepts
  `render_check: true` to run it first.
- **`verify_pass.py` is the third consumer**, downstream of authoring: it replays
  each step in the player, has vision judge target visibility, and hands failures
  to one `run_edit` repair turn. Advisory like everything else here.

**The verdict readers live in `store.py`, not in `render_check.py`** —
`render_check` imports `author`, so `author` importing `render_check` would be a
cycle. Both are now columns on the walkthrough row rather than sidecar files, so
the whole class of bug where a reader resolved a different directory than the
writer is gone with them.

## Collections

Named, **ordered** sets of walkthroughs; a walkthrough may sit in several. The
library groups by them, with anything unfiled under "Unsorted". With no
collections defined the catalog renders exactly as it always did — the feature is
invisible until used, and that is the point.

**Membership is its own table.** Position is a property of the (collection,
walkthrough) pair with nowhere to live on either one, and an empty collection has
to be expressible. `collection_items.walkthrough_id references walkthroughs(id)
on delete cascade` is the whole of the dangling-member problem: a deleted
walkthrough leaves every collection in the same transaction, so there is no
read-time filter and no delete-time sweep to keep in step.

- **Slugs are computed once, at create, and never recomputed on rename.** A link
  to a collection should survive it being retitled.
- **`_clean_items` drops ids the caller does not own rather than rejecting the
  save.** The client sends the list it believes is current, and a walkthrough
  deleted in another tab is an ordinary race. Ownership is checked in the same
  query, which is also what stops a member filing somebody else's walkthrough.
- **`library.store_owns` is defined in `library.py` rather than imported from
  `store`**, to keep the import edge one-way: nothing in the collection path
  should pull the capture layer in behind it.
- **Every write is a whole-set replace, and every write is a POST.** `do_PUT` and
  `do_DELETE` take the entire remainder of `/api/walkthroughs/…` as an id — no
  suffix matching — so `PUT /api/walkthroughs/x/collections` looks for a
  walkthrough with that whole string as its id. Only `do_POST` has suffix
  dispatch. `tests/test_collections_api.py` pins that, and is what says no to a
  REST tidy-up.
- **`set_membership` leaves a walkthrough in the collections it should stay in**,
  so their positions survive: ticking a checkbox must not reorder somebody's
  curated sequence.

Front-end behaviour that is deliberate:

- **Tile alternation restarts in each section.** Continuing one index down the
  column would change a collection's colours whenever an unrelated collection
  *above* it gained a member.
- **Each section is wrapped in its own `.sec-group`.** Flat siblings gave a
  collection boundary the same gap as the space between two cards inside one, so
  the grouping did not read at all.
- **A collection's stored order is never re-sorted by the sort control**, which
  governs Unsorted only. Move up/down is withheld while a filter is active,
  because then the order on screen is not the stored order.
- **Collapse state is a viewer preference**, in `localStorage` under
  `sc-collections-collapsed`. A live filter force-expands sections so the "N of
  M" count cannot lie.
- **`sectionHead()` sets its title with `textContent`** and `menuItem()` appends
  its label as a text node. Both were `innerHTML`, which was safe only while
  every caller passed a literal — a user-named collection now flows through them.
- **`setCardStatus()` uses `querySelectorAll`.** A walkthrough in two collections
  renders two cards with the same `data-walkthrough`.

## Things that look wrong but are not

- **`static/player2.*` is the default player; `static/player.*` is not.**
  `/player/<id>` serves `player2.html` — the popup-instruction player, a
  playback-only fork where the step card floats next to the step's target. The
  classic side-panel player is still load-bearing three ways: it is the only edit
  surface (`/player/<id>?edit=1`), it is the backup at `/player1/<id>`, and
  `export.py`/`scorm.py` bake *its* files into every export and embed. Do not
  merge the forks without re-verifying exports; `tests/test_player2.py` pins the
  split.
- **`page-capture-walkthrough@1`** — the capture wire schema. "page-capture"
  refers to an extension that is no longer part of this project. It is the
  on-the-wire identifier, validated by `ingest.check_capture_schema()`; do not
  "fix" the name.
- **`lesson.json`** — an older manifest name the extension no longer writes. The
  fallback exists for already-recorded captures.
- **The extension wire names are contracts across two repos**, and every one of
  them fails by *silence* — the request is never heard, or the answer never
  recognised, and neither side raises anything:
  `captcher:pair-request` / `captcher:pair-offer` in `static/pair.js`,
  the `captcher-app` / `captcher-extension` source names, the
  `data-captcherCapture` presence marker and `captcher:capture-present`
  event in `templates/_extension-panel.html`, and `/api/extension/ping`'s
  `captcher` key. Three tests read the extension's files directly to catch
  drift, and **skip when that repo is not checked out beside this one** — so a
  green run proves nothing on its own. `test_pairing_handshake.py` goes further
  and simulates the three-file exchange in Node, because three files that each
  parse cleanly can still never meet. See PAIRING.md.
- **`static/pair.js` is loaded from `_toolbar.html`, on purpose.** Every signed-in
  page can then answer a pairing request, so the extension can connect from
  wherever somebody already is. Moving it onto `/account` would mean the side
  panel had to send people to a particular page first.
- **The app deliberately has no Connect button.** Connecting happens in the
  extension's side panel — the only surface that can see which tab is open,
  navigate it, and inject into it. A button here would be the entry point that
  cannot work from a clean install. `/account` reports and revokes.
- **`/api/extension/claim` is reachable without a cookie, by design.** The
  extension has no cookie for the origin it is pairing with — that is the problem
  being solved. The one-time code is the credential: 120 seconds, single use,
  consumed before the token is minted, and held in memory so a crash cannot leave
  a redeemable credential on disk. Moving it below the `authorized()` gate stops
  pairing working, and the failure is a timeout rather than a 401.
- **`/favicon.ico` in `PUBLIC_PATHS`** — deliberate. It stops the browser's
  automatic favicon request from redirecting to `/unlock` on a locked server.
- **The Google Fonts URL builder exists in Python and in JS.** The branding
  preview recomputes it on every keystroke, so a round trip would be wrong.
  `tests/test_font_url_parity.py` pins them together.
- **An ambiguous example slug resolves to nothing.** Slugs are unique per owner,
  not globally, so two accounts can publish `log-a-ticket`. Picking one would
  mean an example page silently changes whose walkthrough it shows. `store.get_public_by_slug` returns None and prints the collision.

## Public examples

- **`/examples/<slug>`** is a full-page public player. The slug must be in
  `CAPTCHER_EXAMPLE_WALKTHROUGHS` *and* the walkthrough must have
  embedding enabled — so a private walkthrough someone typed into the variable
  answers the same plain 404.

## Merged step runs

A step carrying **`continues: true`** plays straight on from the one before it
with no pause. Filling a form is one thing the learner is doing, and a Continue
button between every field turns it into busywork.

**Steps stay steps.** The flag is a marker, not a container: each field keeps its
own `hint`, `demo_value`, `min_length` and `wrong_value_feedback`, and only the
*pacing* and the *counting* merge. One step holding a list of actions was
rejected because the authoring pass is an LLM emitting JSON with no parser
underneath it, and because a compound action would have to re-invent all four of
those fields inside each sub-action.

The flag sits on the 2nd..Nth step of a run, so a run is always contiguous.
`validate()` makes two shapes **errors**, not warnings — a warning still writes
the file, and the result is a walkthrough whose counter disagrees with its own
steps:

- **not on the first step** — nothing precedes it to run on from
- **not on a step that sets `page`** — a run cannot span two snapshots

Things that follow:

- **`continuesRun()` in `player.js` is the only reader of the raw flag.** It
  carries the "not on step 0" half of the rule; a pacing path reading
  `step.continues` directly would treat a hand-edited stray flag as a run.
- **Learners are counted in beats, editors in steps.** A run is one beat, so the
  counter, the progress fill and the rail collapse it. Edit mode does not. Rail
  positions therefore stop matching step indices — `data-step` is the real index.
- **Run titles differ by player.** The classic player heads the whole run with
  its first step's title, holding steady while the captions advance; the popup
  player shows each step's own title, because its card sits next to the field
  being worked on. `tests/test_player2.py` pins the popup side; nothing enforces
  the prompt side.
- **`bridge.js` re-checks an expectation when it arms it**, against fields the
  learner has actually touched. Without it a run deadlocks in try mode. The
  `touched` WeakSet is what keeps that from also auto-completing a step on a
  snapshot that shipped with a field filled.
- Try mode is **strict order**. Any-order would change what `expectation` is
  rather than extending it.

## Templates

Markup lives in `templates/*.html`, not in Python strings. Substitution is
`__PLACEHOLDER__` via `templates.render()`. Do not reintroduce f-string
templates — the doubled `{{ }}` fights embedded CSS and JS.

JSON going into a `<script>` block must use `templates.script_json()`, not
`json.dumps`: the latter escapes neither `<` nor `/`, so a value containing
`</script>` closes the block.

## Testing

```bash
uv run pytest
uv run ruff check .
```

**The suite needs nothing running.** `conftest.py` points `CAPTCHER_HOME` at a
temporary folder for the session and empties every table and the captures
folder before each test, so tests use the real SQLite file and real capture
files, never `~/.captcher`. Only the Playwright tests skip without Chromium
(`python -m playwright install chromium`).

`tests/golden/` holds byte-exact snapshots of every rendered page; they are how a
refactor proves it changed nothing. When a page change is intentional:

```bash
CAPTCHER_UPDATE_GOLDEN=1 python3 -m pytest tests/test_pages.py
```

…then read the diff before committing. A golden that changes without a reason
you can name is a bug.

**`conftest.py` blanks live-service variables for the whole run** — empty, not
deleted, because `load_env()` fills only absent names, so an empty value shadows
the `.env` line. It blanks the Anthropic and LangSmith keys and the knobs `server.py` freezes
at import (access token, allowed hosts, examples); a new one of either kind
belongs in that list. Never point a mutating test at real configuration, and
prefer a fixture over anything that resolves through `envfile`.

## Secrets

Neither `.env` nor `session_secret` is in the repository: both live in the data
folder. `.gitignore` still lists `.env` in case one is created by hand. Comments in
`.gitignore` **must be on their own line** — a trailing `# …` becomes part of the
pattern and silently disables the rule, which is how ignored files ended up
committed once already.

Deleting `session_secret` rotates the signing key and invalidates every issued
cookie (access-token mode asks for the token again).
`CAPTCHER_SESSION_SECRET` pins it instead.

## Known gaps

- Captured pages are third-party JS served **same-origin** in the player's
  iframe, so they can reach this origin's cookies and APIs. Sandboxing the iframe
  was trialled and backed out as unverified — see the note at the `/capture/`
  route in `server.py`.
- `B904` (`raise … from`) is disabled in ruff: the sites need a per-site decision
  between chaining and hiding the cause.
- The extension's `rc-*` runtime message types are a pre-rebrand initialism.
  Three tests here match those literals while reading `recorder.js`, so rename
  them together with the extension, in one change.
