# Captcher

Record a task in your browser and turn it into an interactive training
walkthrough. A Chrome extension captures each screen as you work; an AI pass
(Claude, with your own Anthropic API key) writes the teaching content; the
player replays it over the frozen pages. Walkthroughs export as standalone
HTML, a zip or SCORM. Everything runs on your own computer.

This package is the server, and it carries the Chrome recorder with it.

```bash
uv tool install captcher       # or: pipx install captcher
captcher                       # starts on http://127.0.0.1:8080 and opens it
captcher extension             # the folder to Load unpacked in chrome://extensions
```

Then add `ANTHROPIC_API_KEY=…` to `~/.captcher/.env` and restart to have
walkthroughs written for you. The optional render and replay checks need
`captcher[verify]` and `captcher install-browser`.

The full guide is the repository README. For working on the code:

```bash
uv sync --extra dev
uv run captcher --dev
uv run pytest
```

`CLAUDE.md` in this folder is the module map and the list of load-bearing
rules, written for anyone (or any agent) changing the code.
