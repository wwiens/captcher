"""Offline brand asset contracts; no network needed to run these tests."""
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

from PIL import Image

STATIC = Path(__file__).resolve().parents[1] / "captcher" / 'static'


def test_brand_fonts_are_real_self_hosted_woff2_with_licenses():
    css = (STATIC / 'fonts/fonts.css').read_text(encoding='utf-8')
    assert '@import' not in css
    # UI v2: Outfit is one variable file declared once with a weight range, so
    # the 600 headings resolve to a real 600 and an export inlines it once.
    outfit = [block for block in css.split('}') if "font-family: 'Outfit'" in block]
    assert len(outfit) == 1
    assert 'font-weight: 100 900;' in outfit[0]
    assert "url('/static/fonts/outfit-latin-900.woff2')" in outfit[0]
    assert (STATIC / 'fonts/outfit-latin-900.woff2').read_bytes()[:4] == b'wOF2'
    assert 'SIL OPEN FONT LICENSE' in (STATIC / 'fonts/outfit-OFL.txt').read_text(encoding='utf-8')
    for family, stem, weights in [('JetBrains Mono', 'jetbrains-mono', (400, 500, 700)),
                                   ('Sora', 'sora', (400, 500, 600, 700, 800))]:
        for weight in weights:
            name = f'{stem}-latin-{weight}.woff2'
            path = STATIC / 'fonts' / name
            assert path.exists(), f'Missing self-hosted {name}'
            data = path.read_bytes()
            assert data[:4] == b'wOF2'
            assert struct.unpack('>I', data[8:12])[0] == len(data)
            assert len(data) > 5000
            assert any(f"font-family: '{family}'" in block and f'font-weight: {weight};' in block
                       and f"url('/static/fonts/{name}')" in block
                       for block in css.split('}'))
        license_text = (STATIC / 'fonts' / f'{stem}-OFL.txt').read_text(encoding='utf-8')
        assert 'SIL OPEN FONT LICENSE' in license_text
    # DM Sans is one variable file (opsz + wght) declar# Brand Guide v1 mark: four frame bars and five stepping squares on a 526 grid.
FRAME = {('230', '0', '296', '50'), ('476', '0', '50', '270'),
         ('0', '256', '50', '270'), ('0', '476', '296', '50')}
# No purple: Coral, Sky, Indigo, Mint, Coral (2026-09-25).
SQUARES = [(('0', '0', '72', '72'), '#FF6A5C'), (('92', '92', '90', '90'), '#29B6FF'),
           (('202', '202', '122', '122'), '#3B49C6'), (('344', '344', '90', '90'), '#3ECFA2'),
           (('454', '454', '72', '72'), '#FF6A5C')]
NS = {'s': 'http://www.w3.org/2000/svg'}


def _rects(svg):
    return {(r.get('x'), r.get('y'), r.get('width'), r.get('height')): r.get('fill')
            for r in svg.findall('.//s:rect', NS)}


def test_logos_use_the_supplied_mark_and_an_outlined_outfit_wordmark():
    for name, frame in (('logo-mark.svg', '#FFFFFF'), ('logo-mark-light.svg', '#14151A'),
                        ('logo-horizontal.svg', '#FFFFFF'), ('logo-horizontal-light.svg', '#14151A')):
        svg = ET.parse(STATIC / name).getroot()
        rects = _rects(svg)
        assert {k for k, v in rects.items() if v == frame} >= FRAME, name
        # The squares are never recoloured.
        for geometry, colour in SQUARES:
            assert rects.get(geometry) == colour, (name, geometry)
        assert svg.get('aria-label') == 'Captcher'
        assert 'Shadow' not in (STATIC / name).read_text(encoding='utf-8')
        if 'horizontal' in name:
            # Outlines make img elements and portable exports independent of font loading.
            assert not svg.findall('.//s:text', NS)
            word = svg.find(".//s:path[@id='wordmark']", NS)
            assert word is not None and word.get('fill') == frame
            assert word.get('data-font') == 'Outfit'
            assert word.get('data-weight') == '900'
            assert word.get('data-tracking') == '-0.035em'
    mono = _rects(ET.parse(STATIC / 'logo-mark-mono.svg').getroot())
    assert set(mono.values()) == {'#14151A'} and len(mono) == 9


def test_outlined_wordmark_uses_black_not_variable_font_default():
    # Optional build-time check: runtime asset tests above need only Pillow.
    import pytest
    ttlib = pytest.importorskip('fontTools.ttLib')
    svgpen = pytest.importorskip('fontTools.pens.svgPathPen')
    transformpen = pytest.importorskip('fontTools.pens.transformPen')
    instancer = pytest.importorskip('fontTools.varLib.instancer')
    font = instancer.instantiateVariableFont(
        ttlib.TTFont(STATIC / 'fonts/outfit-latin-900.woff2'), {'wght': 900})
    glyphs = font.getGlyphSet()
    scale = 64 / font['head'].unitsPerEm
    pen = svgpen.SVGPathPen(glyphs)
    glyphs[font.getBestCmap()[ord('C')]].draw(
        transformpen.TransformPen(pen, (scale, 0, 0, -scale, 0, 0)))
    for name in ('logo-horizontal.svg', 'logo-horizontal-light.svg'):
        word = ET.parse(STATIC / name).getroot().find(".//s:path[@id='wordmark']", NS)
        assert word.get('d').startswith(pen.getCommands())


def test_favicons_are_the_indigo_tile_at_every_size():
    for name in ('favicon.svg', 'app-icon.svg', 'logo.svg'):
        svg = ET.parse(STATIC / name).getroot()
        assert svg.find('s:rect', NS).get('fill') == '#3B49C6'   # the tile
        rects = _rects(svg)
        assert {k for k, v in rects.items() if v == '#FFFFFF'} >= FRAME
        assert rects[('202', '202', '122', '122')] == '#FFFFFF'  # Indigo centre reads on Indigo
        assert rects[('92', '92', '90', '90')] == '#29B6FF'
        assert rects[('454', '454', '72', '72')] == '#FF6A5C'
    for size in (16, 32, 180, 512):
        with Image.open(STATIC / f'favicon-{size}.png') as image:
            assert image.size == (size, size)
            rgba = image.convert('RGBA')
            assert rgba.getpixel((size // 2, size // 2)) == (255, 255, 255, 255)  # white centre
            if size >= 32:
                assert rgba.getpixel((size // 2, 1)) == (59, 73, 198, 255)     # Indigo tile
                assert rgba.getpixel((0, 0))[3] == 0                           # rounded corner
            else:
                near_indigo = sum(1 for px in rgba.getdata()
                                  if abs(px[0] - 59) < 30 and abs(px[1] - 73) < 30 and abs(px[2] - 198) < 30)
                assert near_indigo > 16 * 16 * .35
    with Image.open(STATIC / 'favicon.ico') as ico:
        assert {(16, 16), (32, 32), (48, 48)} <= ico.ico.sizes()
        assert ico.ico.getimage((16, 16)).convert('RGBA').tobytes() == Image.open(STATIC / 'favicon-16.png').convert('RGBA').tobytes()


def test_no_purple_anywhere():
    """Warren's rule (2026-09-25): no purple, ever. Show me is Sky; the mark's
    squares are Coral, Sky, Indigo, Mint, Coral. Scans every colour literal in
    the served chrome, players, emails and templates for a violet/purple hue."""
    import colorsys
    import re
    root = STATIC.parent
    files = [p for d in ('static', 'templates', 'emails') for p in (root / d).rglob('*')
             if p.suffix in ('.css', '.js', '.html', '.svg') and 'fonts' not in p.parts]
    files += [root / 'branding.py', root / 'export.py']
    offenders = []
    for path in files:
        text = re.sub(r'<metadata>.*?</metadata>', '', path.read_text(encoding='utf-8'), flags=re.S)
        colours = [m.group(1) for m in re.finditer(r'(?:#|%23)([0-9a-fA-F]{6})\b', text)]
        colours += [''.join(f'{int(v):02x}' for v in m.groups())
                    for m in re.finditer(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', text)]
        for hexcode in colours:
            r, g, b = (int(hexcode[i:i + 2], 16) / 255 for i in (0, 2, 4))
            hue, light, sat = colorsys.rgb_to_hls(r, g, b)
            if 245 <= hue * 360 <= 330 and sat > 0.2 and 0.05 < light < 0.99:
                offenders.append(f'{path.name}: #{hexcode}')
    assert not offenders, offenders


def test_getting_started_images_exist_and_are_sized_to_match():
    """Each screenshot the guide names exists, and its width/height attributes
    are half its pixel size (they are taken at 2x). A replaced screenshot takes
    a new filename: /static/ PNGs are cached as immutable."""
    import re
    import struct
    root = STATIC.parent
    page = (root / "templates" / "getting-started.html").read_text(encoding="utf-8")
    imgs = re.findall(r'<img src="/static/([^"]+\.png)" width="(\d+)" height="(\d+)"', page)
    assert len(imgs) >= 5   # one per guide step
    for rel, w, h in imgs:
        data = (STATIC / rel).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", rel
        pw, ph = struct.unpack(">II", data[16:24])
        if rel.startswith("getting-started/"):
            assert (int(w), int(h)) == (pw // 2, ph // 2), rel
        else:
            assert int(w) * ph == int(h) * pw, rel   # same shape, whatever the scale
    named = {rel.split("/")[-1] for rel, _, _ in imgs}
    on_disk = {p.name for p in (STATIC / "getting-started").glob("*.png")}
    assert on_disk <= named, f"unreferenced screenshots: {on_disk - named}"
