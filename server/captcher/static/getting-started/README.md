# Getting started screenshots

- `author-with-ai-2026-11.png` and `try-walkthrough-2026-11.png`: taken
  2026-11-16 by `extension/tests/e2e/setup-shots.cjs`. The draft card is a real
  recording of the Cadence CRM sample app (`~/Projects/sample_sites`) uploaded
  to a throwaway local Captcher; the player is `examples/log-a-new-deal`, an
  export of the same player.
- `edit-walkthrough-2026-10.png` and `download-walkthrough-2026-10.png`: taken
  2026-10-12 from the real application against throwaway data. They show no
  account details and are still accurate.
- The recorder image is `../extension-setup/recorder-ready-2026-11.png`.

All are 2x screenshots of the real UI, not mockups. The captured screens are
fictional demo apps.

**Replacing an image means a new filename** (these carry the month they were
taken). `/static/` serves PNGs as `immutable` for a year
(`IMMUTABLE_STATIC_SUFFIXES` in `server.py`), so new bytes under an old name
are never fetched by a browser that has seen the old ones.
`tests/test_captcher_assets.py` checks every image the page names exists and
that its width/height attributes are half its pixel size.

Images are screenshots, not generated mockups. Crops focus on the relevant UI:
the draft's authoring panel, the full player in You try, the editor's side
panel, and the two dialogs. Refresh them when these interfaces change, and
update the width/height attributes in `templates/getting-started.html` to
match (half the PNG's pixel size).
