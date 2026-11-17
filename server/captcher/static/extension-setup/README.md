# Extension setup screenshots

Captured 2026-11-16 from Captcher Recorder 1.0.1 by
`extension/tests/e2e/setup-shots.cjs`: the real side panel in Chrome for
Testing with a fresh profile, paired with a throwaway local Captcher on
`127.0.0.1:8080`. 2x resolution (the panel is 400 CSS pixels wide), no image
manipulation. The only account is the local owner, `you@localhost`.

- `recorder-setup-2026-11.png`: first run, opened on another site.
- `recorder-connected-2026-11.png`: the connection readout right after pairing.
- `recorder-ready-2026-11.png`: title field and Start recording.

**Replacing an image means a new filename** with the month it was taken:
`/static/` serves PNGs as `immutable` for a year. Update the names and the
width/height attributes (half the pixel size) in `templates/extension.html`
and `templates/getting-started.html`.
