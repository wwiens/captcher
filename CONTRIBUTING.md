# Contributing to Captcher

Thanks for helping. Captcher is two programs in one repository, and most
changes touch one of them:

| Folder | What it is | Language |
|---|---|---|
| `server/` | the `captcher` command: catalog, AI authoring, player, exports | Python 3.13, standard library HTTP server, SQLite |
| `extension/` | Captcher Recorder, the Chrome (MV3) extension | plain JavaScript, no build step |

`server/CLAUDE.md` is the detailed map of the server: which module owns what,
the rules that are load-bearing, and the traps that have been walked into
before. Read the part about the module you are changing.

## Set up

You need [uv](https://docs.astral.sh/uv/), Node 22 and desktop Chrome.

```bash
git clone https://github.com/wwiens/captcher && cd captcher/server
uv sync --extra dev
export CAPTCHER_HOME=~/captcher-dev     # keep development data apart from your own
uv run captcher --dev                   # restarts when the Python changes
```

Load `extension/` into Chrome with **Load unpacked**. A source checkout uses
that folder directly, so after editing the extension, press its reload arrow in
`chrome://extensions` and reopen the side panel.

The AI passes need `ANTHROPIC_API_KEY` in `$CAPTCHER_HOME/.env`. Everything else
(recording, playback, exports, the test suite) works without it.

## Run the tests

```bash
cd server
uv run pytest                 # about 950 tests, a couple of minutes
uv run ruff check .

cd ../extension
node --test tests/*.test.cjs
```

The server suite runs against a throwaway data folder, never yours, and calls
no outside service. A few tests drive a real browser; they need
`uv run python -m playwright install chromium` once and fail without it.

**Golden pages.** `server/tests/golden/` holds a snapshot of every rendered
page. When you change a page on purpose, regenerate them and read the diff:

```bash
CAPTCHER_UPDATE_GOLDEN=1 uv run pytest tests/test_pages.py
```

**End to end.** `extension/tests/e2e/` records real flows in Chrome for Testing
against a running server; see its README. It is not part of CI.

## Rules worth knowing before you start

- **Nothing imports `server.py`.** It is the HTTP layer only. Shared logic
  goes in a module both sides can import; `server.py` imports it.
- **Server and extension change together.** The pairing messages, the ping
  answer, the presence marker and the upload format must match on both sides,
  and drift fails silently. They are listed in `docs/contract.md`, and the
  contract tests in `server/tests/` read the extension's source to catch it.
  Change both halves in the same pull request.
- **Changing the capture format.** New fields in the upload manifest are safe:
  `ingest.py` ignores keys it does not know. Removing or renaming one is not.
  Never drop `page-capture-walkthrough@1` from `ingest._ACCEPTED_SCHEMAS`;
  recorders already installed send it. If a new schema version is needed,
  accept both, and add an ingest test with a capture in each.
- **Database changes** go in a numbered step in `db.UPGRADES`, never by
  editing `schema.sql` for an existing database. Add a test that upgrades a
  database created by the previous schema.
- **The recorder captures other people's pages.** Anything that touches
  redaction (`extension/sensitive.js`) needs a test in
  `extension/tests/redaction-boundary.test.cjs`.
- **Copy.** Sentence case for headings, buttons and menu items. The product
  is "Captcher"; the extension is "Captcher Recorder".

## Style

- Python: `ruff check .` must pass (config in `server/pyproject.toml`). Comments
  say why, not what.
- JavaScript: match the file you are in. No dependencies, no bundler.
- Keep pull requests to one change, with tests, and say in the description how
  you checked it.

## Licence

Captcher is under the [GNU AGPL v3 or later](LICENSE). By contributing you agree
that your contribution is licensed the same way.
