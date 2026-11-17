# What the server may not change

Once the recorder is in the Chrome Web Store, this repository is the only half
of the pair that can be fixed. A shipped extension cannot be updated on demand:
a change here that breaks it is on the customer's machine until they take an
update, and the review that gates one runs for days.

So the strings and shapes below are frozen. Not "prefer not to change" — frozen.
Each one is named here because breaking it is easy, the reason it broke is not
obvious from either side, and in most cases nothing anywhere reports a problem.

`tests/test_extension_contract.py` enforces this file. If you are reading this
because that test failed, the answer is almost never to update the test.

## The eight

| # | Contract | Where | Versioned | Fails |
|---|---|---|---|---|
| 1 | `GET /api/extension/ping` answering `{"captcher": true, "pairing": N}` | `server.py` | `pairing` is read from recorder 0.1.0 | silently |
| 2 | The pairing postMessage names and their `source` discriminators | `static/pair.js` | no | silently |
| 3 | `POST /api/extension/claim` answering `{"token": …, "email": …}` | `server.py` | no | loudly |
| 4 | `POST /api/upload`, multipart field `capture_zip` (plus optional `capture_log`), `Authorization: Bearer` | `server.py` | no | loudly (`capture_log`: silently) |
| 5 | The upload response keys `walkthrough_id`, `player_url`, `steps` | `server.py` | partly shimmed | gracefully |
| 6 | `page-capture-walkthrough@1` staying in `_ACCEPTED_SCHEMAS` | `ingest.py` | yes | loudly |
| 7 | The presence marker and event announce.js sets | `templates/_extension-panel.html` | no | silently |
| 8 | The selector ladder `cssFor()` mirrors | `digest.py` | no | quietly, as worse output |

"Silently" is the word to notice. Five of these fail with no error on either
side — the user sees a feature that stopped working and there is nothing in any
log to say why.

## What each one does when it breaks

**1 — the ping.** Rename the route or the `captcher` key and your real
server stops looking like Captcher to every installed recorder. The panel
then offers to *open* Captcher while the user is already looking at it.
Nothing is logged, on either side.

The `pairing` number is the one deliberate escape hatch in this list, and from
recorder 0.1.0 the extension reads it. Raising it tells older recorders to
say "this recorder is out of date, update it from the Web Store" instead of
failing shapelessly. Two optional fields ride the same response and are the only
way to say anything unplanned to a browser that is already out there:

- `notice` — free text, shown verbatim in a banner above every screen in the
  panel. Set `CAPTCHER_EXTENSION_NOTICE`.
- `minExtensionVersion` — the oldest recorder this server still works with. Set
  `CAPTCHER_MIN_EXTENSION`.

Use them. They are the difference between an incident being a deploy and being
a three-week resubmission with every user stuck on the broken build.

**2 — pairing postMessage.** Six literals across `static/pair.js` and the
extension's `connect.js`: two message names, two `source` values, and the
`code` / `error` payload keys. Rename one and the request is never heard or the
answer never recognised. The user waits six seconds and is then told to sign in,
which is the wrong diagnosis — and the only one the extension can offer.

**3 — claim.** The route and the `token` key. Break either and no browser can
pair again. This one at least fails loudly: "the server refused the pairing
code".

**4 — upload.** The route, the multipart field name, and Bearer auth. Break any
and every recording fails at Send. The recorder keeps the walkthrough for a
retry that cannot succeed, and closing the panel destroys it — walkthrough state
is held in memory and never written to disk.

**5 — upload response.** The best-behaved of the set. `steps` is already
tolerated as either an array or a count; the other two degrade to `undefined` in
a log line rather than breaking anything. Still: additive changes only.

**6 — the capture schema.** Additive fields on `@1` are safe in both directions
and always have been: `ingest.py` ignores keys it does not know, and the
extension relies on that. What is not safe is dropping `@1` from
`_ACCEPTED_SCHEMAS`. A server that ships `@2` and removes `@1` rejects every
installed recorder with a 400 whose text blames the extension for being too new.
If a `@2` is ever needed, accept both for as long as any `@1` recorder exists —
which, for a Web Store extension, is years.

**7 — the presence marker.** `data-captcher-capture` and the
`captcher:capture-present` event. Rename either and the "install the
extension" prompt shows forever to people who already have it. Cosmetic,
unfixable from this side, and the most confusing to diagnose.

**8 — the selector ladder.** `recorder.js`'s `cssFor()` and `digest.py`'s
`target_for()` have to agree on what a selector for an element looks like, and
`laneOf` has to agree with `_dropzone_nodes`. Drift here does not break
anything: authoring falls back to guessing and the walkthroughs get quietly
worse. The safest of the unversioned contracts, and the easiest to let rot.

## What is NOT frozen

Nearly everything. The authoring pipeline, the player, the catalogue, the
account pages, ingest internals, the database, prompts, pricing, the whole web
app. Additive fields on `@1`. Any response key the extension does not read.

The freeze is eight strings and shapes wide. It is only dangerous because
nothing except that test file stops someone walking into it.

## Telling what is out there

The upload may also carry a `capture_log` field: the panel's complete log for
that capture, as `captcher-capture-log@1` JSON (`{schema, kind, startedAt,
entries: [{at, elapsed, level, text}], omitted}`). `diagnostics.py` stores it in
`walkthrough_logs`; nothing displays it yet. It is optional both ways — older
recorders do not send it, and a missing or malformed one never fails the
upload — which is exactly why renaming it would go unnoticed: the logs would
simply stop arriving.

Since recorder 0.1.0, `POST /api/upload` carries
`X-Captcher-Recorder: <version>`, and a paired device's label on
`/account` ends with `· recorder <version>`. Those are how you find out which
builds are in use before deciding whether an old one still matters.
