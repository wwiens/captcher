# Changelog

## 0.1.0 (server) and 1.1.0 (Captcher Recorder): first open-source release

Captcher was a hosted service. This release is the same product as a local,
single-user tool you run yourself.

**Install and run**

- `uv tool install` gives you the `captcher` command, and the Chrome recorder
  ships inside the package (`captcher extension` prints the folder to load).
- `captcher` creates `~/.captcher` on first run, starts on
  `http://127.0.0.1:8080` and opens a setup guide. Settings live in
  `~/.captcher/.env`.
- No accounts, sign-up or billing. AI authoring and editing use your own
  Anthropic API key; the AI spend page shows what each pass cost.
- Walkthroughs are stored in SQLite and captured pages as files, both under
  `~/.captcher`, instead of a hosted database and object store.

**Recorder**

- Talks to one Captcher server you choose in its settings (default
  `http://127.0.0.1:8080`), pairs with it in one click, and contacts nothing
  else. Plain http is allowed only for your own computer.

**Security**

- The server listens only on this computer unless you set an access token.
- Requests with a foreign `Host` header and cross-site writes are refused.

**Optional**

- The render and replay checks need Playwright: install `captcher[verify]`,
  then run `captcher install-browser`.

**Changed names** (for anyone moving from the hosted service)

- Settings are `CAPTCHER_*` (the old `SHADOWCAPTURE_*` names are ignored, with
  a warning).
- Upload tokens start `cap_`; browsers paired with the hosted service pair again.
- Player events are `captcher:step` and `captcher:complete`; the embed API is
  `window.CaptcherHelp` and `data-captcher-help`.
