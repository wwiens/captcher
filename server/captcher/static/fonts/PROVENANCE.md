# Captcher bundled font provenance

Retrieved 2026-09-25 from Google Fonts CSS API and its fonts.gstatic.com binary CDN.
Outfit and JetBrains Mono are unmodified, actual variable WOFF2 Latin subsets, not synthetic font data. Google Fonts returns the same variable binary for multiple requested weights; weight-specific legacy-style filenames intentionally retain those exact upstream bytes. CSS selects each declared weight, rather than relying on the binary default (Outfit Thin 100 / JetBrains Mono 400).
Sora binaries are preserved byte-for-byte from the existing app for legacy custom profiles.
All three families are distributed under SIL OFL 1.1; the complete copyright/license texts are alongside these files.

CSS query: https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap

## Downloaded files

- `jetbrains-mono-latin-400.woff2`
  Source: https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2
  SHA-256: `83c005d49d8a6a50474c73a5a36ac0468076e9c4a29da7bdb14995d80560a5be`
- `jetbrains-mono-latin-500.woff2`
  Source: https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2
  SHA-256: `83c005d49d8a6a50474c73a5a36ac0468076e9c4a29da7bdb14995d80560a5be`
- `jetbrains-mono-latin-700.woff2`
  Source: https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2
  SHA-256: `83c005d49d8a6a50474c73a5a36ac0468076e9c4a29da7bdb14995d80560a5be`
- `outfit-latin-400.woff2`
  Source: https://fonts.gstatic.com/s/outfit/v15/QGYvz_MVcBeNP4NJtEtq.woff2
  SHA-256: `6c18d579fd87c3776be068b762cbc83fde3acb543d49eabd3ade842eb987e887`
- `outfit-latin-500.woff2`
  Source: https://fonts.gstatic.com/s/outfit/v15/QGYvz_MVcBeNP4NJtEtq.woff2
  SHA-256: `6c18d579fd87c3776be068b762cbc83fde3acb543d49eabd3ade842eb987e887`
- `outfit-latin-700.woff2`
  Source: https://fonts.gstatic.com/s/outfit/v15/QGYvz_MVcBeNP4NJtEtq.woff2
  SHA-256: `6c18d579fd87c3776be068b762cbc83fde3acb543d49eabd3ade842eb987e887`
- `outfit-latin-800.woff2`
  Source: https://fonts.gstatic.com/s/outfit/v15/QGYvz_MVcBeNP4NJtEtq.woff2
  SHA-256: `6c18d579fd87c3776be068b762cbc83fde3acb543d49eabd3ade842eb987e887`
- `outfit-latin-900.woff2`
  Source: https://fonts.gstatic.com/s/outfit/v15/QGYvz_MVcBeNP4NJtEtq.woff2
  SHA-256: `6c18d579fd87c3776be068b762cbc83fde3acb543d49eabd3ade842eb987e887`
- `outfit-OFL.txt`
  Source: https://raw.githubusercontent.com/google/fonts/main/ofl/outfit/OFL.txt
  SHA-256: `c676351bf8576b9aba743cd5eaa8c0e7ee0d51f805d720447b4df4ddb6a2e416`
- `jetbrains-mono-OFL.txt`
  Source: https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/OFL.txt
  SHA-256: `b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d`
- `sora-OFL.txt`
  Source: https://raw.githubusercontent.com/google/fonts/main/ofl/sora/OFL.txt
  SHA-256: `ba0b9729c9428ba79a0459ab8ec575791b51509dbec213e383d0316d37fec299`

## DM Sans (added for Brand Guide v1, 2026-09-25)

Interface and body family. Google Fonts' CSS API was unreachable from the build environment, so the binary was taken from the npm package `@fontsource-variable/dm-sans@5.3.0` (OFL-1.1), which republishes the unmodified upstream Google Fonts build (`DMSans[opsz,wght].ttf`, googlefonts/dm-fonts) as Latin-subset WOFF2.

- `dm-sans-latin.woff2`
  Source: `@fontsource-variable/dm-sans@5.3.0` → `files/dm-sans-latin-opsz-normal.woff2` (axes opsz 9–40, wght 100–1000)
  SHA-256: `ca72d2bcea8f4daa783dbdfa2d9b46068c3ce38168e05918fb867aa453b4f890`
- `dm-sans-OFL.txt`
  Source: same package, `LICENSE`
  SHA-256: `6fbd040a29c2037a765dfb9f2561e9965b5c95c6dcb5dce516089d63d5f17af7`

Declared once in `fonts.css` with `font-weight: 100 1000`, so exports inline one copy.

## Retained Sora SHA-256

- `sora-latin-400.woff2`: `98141d7eaa6e9ee35e0827d279018cd33c3e59c6650541ec7de1c692efd93942`
- `sora-latin-500.woff2`: `edd010ebf2f4940b52d8b98296a499c0a783d838e427fa301c67f80959c1f441`
- `sora-latin-600.woff2`: `fa9ab76f30510ad92153c6d6d72d0508884b85e9f0148abdfa963231b2a4845a`
- `sora-latin-700.woff2`: `c3337a673c9bbbfe433091d3831714dd9a257951f16f41eb3fc650ec593cfac2`
- `sora-latin-800.woff2`: `b3058cd014993defb5c446d9ccdeba7f994c4b7f0ff2b50b2b213944af0f176e`

## SVG and raster artwork provenance

Visual geometry comes from the user-supplied Brand Guide v1 package (`../Assets/`: `captcher-mark.svg`, `Brand Guide.dc.html`, `Captcher Logo.dc.html`), not from a stock icon. These proprietary brand assets do not inherit the font OFL license. The previous Viewfinder artwork is superseded; filenames are unchanged because saved brand profiles and `export._own_asset` address them by name.

The mark is four Ink frame bars and five stepping squares on a 526-unit grid: Coral `#FF6A5C`, Sky `#29B6FF`, Indigo `#3B49C6`, Mint `#3ECFA2`, Coral `#FF6A5C` (recoloured 2026-09-25 to remove purple: the supplied mark's second square was Violet `#8B5CF6`; its centre was Sky and its last square Indigo). On the Indigo tile (favicons, app icon, embed launcher) the Indigo centre square is white. Only the frame changes with the ground.

- `logo-mark.svg`: white frame, for dark grounds.
- `logo-mark-light.svg`: Ink `#14151A` frame, for light grounds.
- `logo-mark-mono.svg`: single-colour Ink (frame and squares), for one-colour reproduction.
- `logo-horizontal.svg` (dark grounds) / `logo-horizontal-light.svg` (light grounds): the Brand Guide lockup (88px mark, 24px gap, 64px wordmark). "Captcher" is Outfit Black 900 glyph outlines extracted with FontTools `SVGPathPen` from the bundled variable WOFF2 instanced at wght 900, tracking -0.035em, so the SVG needs no font when used as an image.
- `app-icon.svg`, `logo.svg`, `favicon.svg`: the Brand Guide's Indigo tile — white frame, fifth square white so it reads on Indigo.
- `templates/_logo.svg`: inline chrome mark; the frame takes `__STROKE__` (currentColor by default) so it follows the theme, squares keep brand colour.
- PNGs rendered with Playwright Chromium: `favicon-16.png` from a 16px-specific tile with a tighter inset (the mark fills more of the tile at that size), `favicon-32.png` from `favicon.svg`, `favicon-180.png` and `favicon-512.png` from `app-icon.svg`. `favicon.ico` embeds 16/32/48px renders.
- Generator scripts were build-time only; artwork does not need them to display.

UI v2 (2026-09-25): `fonts.css` now declares Outfit once, `font-weight: 100 900`, from `outfit-latin-900.woff2` (the same variable bytes as every file above). The 400/500/700/800 copies are no longer referenced by the stylesheet.
