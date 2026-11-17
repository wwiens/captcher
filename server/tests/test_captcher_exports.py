"""Captcher chrome and offline export compatibility (no live services)."""
import io
import json
import zipfile
import re
import socket

import pytest

from captcher import branding
from captcher import export


def test_builtin_identity_and_white_label_baseline():
    profile = branding.my_brand_profile()
    assert profile['brand_name'] == 'Captcher'
    assert profile['id'] == 'shadow-capture'
    assert profile['read_only'] is True
    assert profile['fonts'] == {'heading': 'Outfit', 'body': 'DM Sans', 'webfont_url': ''}
    colors = profile['colors']
    assert colors['primary'] == '#3B49C6'      # Indigo: You try
    assert colors['accent'] == '#0A6FA8'       # Sky (deep): Show me
    assert colors['background'] == '#F2F3F7'   # Mist
    assert colors['text'] == '#14151A'         # Ink
    assert colors['success'] != colors['accent']
    # Every slot that carries text clears AA on the ground it lands on.
    for key in ('text', 'muted', 'accent', 'primary', 'success', 'error'):
        assert export._contrast(colors[key], colors['background']) >= 4.5, key
        assert export._contrast(colors[key], colors['surface']) >= 4.5, key
    for field in ('logo_light_url', 'logo_dark_url', 'favicon_url'):
        assert profile[field].startswith('https://www.captcher.app/assets/')
        assert branding.DEFAULT_BRANDING[field] == ''
    assert branding.google_fonts_url('Outfit', 'Sora') == ''
    assert branding.google_fonts_url('JetBrains Mono', 'Outfit') == ''
    assert branding.google_fonts_url('Outfit', 'DM Sans') == ''


@pytest.fixture
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('network access forbidden')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(branding.urllib.request, 'urlopen', blocked)
    monkeypatch.setattr(branding, 'GOOGLE_FONTS_API_KEY', '')
    row = {'id': 'stable-walkthrough-id', 'slug': 'customer-task',
           'definition': {'title': 'Customer task', 'steps': [
               {'page': 'pages/1.html', 'title': 'Click', 'action': 'click'}]}}
    monkeypatch.setattr(export.store, 'get', lambda _: row)
    monkeypatch.setattr(export.store, 'capture_text', lambda *_:
                        '<html><head></head><body>Shadow Capture customer recording</body></html>')


@pytest.mark.parametrize('origin', ['shadowcapture.ai', 'captcher.app', 'www.captcher.app'])
def test_owned_assets_resolve_locally(origin, offline, tmp_path):
    profile = branding.my_brand_profile()
    for field, url in profile.items():
        if field.endswith('_url'):
            profile[field] = url.replace('www.captcher.app', origin)
    local = export._download_branding_images(profile, tmp_path / 'assets/images')
    for field in ('logo_light_url', 'logo_dark_url', 'favicon_url'):
        assert (tmp_path / local[field]).is_file()
        assert profile[field].startswith('https://')
    for bad in ['https://captcher.app.evil/assets/logo.svg',
                'https://captcher.app/assets/../logo.svg',
                'https://captcher.app/assets/logo.svg?remote=1']:
        assert export._own_asset(bad) is None


@pytest.mark.parametrize('player', ['player', 'player2'])
def test_single_file_owned_brand_is_self_contained(player, offline):
    profile = branding.my_brand_profile()
    html = export._build_single_file('stable-walkthrough-id', profile, False, 'full', player)
    data = json.loads(re.search(r'<script id="sc-brand-data" type="application/json">(.*?)</script>', html).group(1))
    for field in ('logo_light_url', 'logo_dark_url', 'favicon_url'):
        assert data[field].startswith('data:image/')
        assert profile[field].startswith('https://www.captcher.app/')
    assert 'Powered by Captcher' in html
    assert 'Shadow Capture customer recording' in html
    assert 'stable-walkthrough-id' in html


@pytest.mark.parametrize('player', ['player', 'player2'])
def test_player_chrome_uses_captcher_slots(player):
    html = export._read_static(player + '.html')
    css = export._read_static(player + '.css')
    assert '<title>Captcher</title>' in html
    assert '<span class="brand-word">Captcher</span>' in html
    preloads = re.findall(r'rel="preload" href="(/static/fonts/[^"]+)"', html)
    assert sorted(preloads) == ['/static/fonts/dm-sans-latin.woff2', '/static/fonts/outfit-latin-900.woff2']
    assert all((export.ROOT / path.lstrip('/')).is_file() for path in preloads)
    assert '--font-body: "DM Sans"' in css and '--font-head: "Outfit"' in css
    for key, value in branding.DEFAULT_BRANDING['colors'].items():
        slot = {'background': 'bg', 'primary_hover': 'primary-hover'}.get(key, key)
        assert re.search(r'--' + slot + r':\s*' + value + ';', css)
    assert 'brand-logo-slot' in html
    assert '#mode-show.active' in css and '#mode-try.active' in css
    show = re.search(r'#mode-show.active\s*\{([^}]+)', css).group(1)
    assert 'background: var(--surface)' in show
    assert "M202 202h122v122H202z" in css  # the mark's centre square, drawn in CSS
    assert 'mask:' in css                  # frame masked to currentColor
    assert 'background: #fff' in css  # recorded screen is not brand-tinted


def test_embed_keeps_public_api_with_new_default_identity():
    js = export._read_static('embed.js')
    assert '"DM Sans"' in js
    assert 'primary: "#3B49C6"' in js and 'launcher: "#3B49C6"' in js
    assert 'viewBox="0 0 526 526"' in js and '#C6FF3D' not in js
    assert 'window.CaptcherHelp = api' in js
    assert 'data-captcher-help' in js
    assert 'data-sc-src' in js
    assert 'Shadow Capture' not in (export.ROOT / 'embed-demo.html').read_text(encoding='utf-8')


@pytest.mark.parametrize('player', ['player', 'player2'])
def test_fonts_and_folder_export_offline(player, offline, tmp_path):
    css = export._fonts_css_inlined()
    for family in ('DM Sans', 'Outfit', 'Sora', 'JetBrains Mono'):
        assert "font-family: '" + family + "'" in css
    assert 'data:font/woff2;base64,' in css
    assert 'url(\'/static/' not in css
    out = export.build_export_folder('stable-walkthrough-id', tmp_path / player,
                                     branding.my_brand_profile(), player=player)
    html = (out / 'index.html').read_text(encoding='utf-8')
    for path in re.findall(r'(?:href|src)="(assets/[^"]+)"', html):
        assert (out / path).is_file()
    font_css = (out / 'assets/fonts/fonts.css').read_text(encoding='utf-8')
    for path in re.findall(r"url\('([^']+)'\)", font_css):
        assert (out / 'assets/fonts' / path).is_file()
    assert 'https://www.captcher.app/assets/' not in html
    assert '<title>Customer task</title>' in html


@pytest.mark.parametrize('player', ['player', 'player2'])
def test_every_export_is_titled_after_its_walkthrough_not_the_brand(player, offline, monkeypatch):
    # A folder or SCORM export used to sit in its tab as "Captcher": the brand
    # applier overwrote the title the player had set.
    html = export._build_single_file('stable-walkthrough-id', branding.my_brand_profile(),
                                     False, 'full', player)
    assert '<title>Customer task</title>' in html
    assert 'document.title=b.brand_name' not in html
    row = export.store.get('stable-walkthrough-id')
    row['definition']['title'] = 'Fish & <Chips>'
    html = export._build_single_file('stable-walkthrough-id', None, False, 'full', player)
    assert '<title>Fish &amp; &lt;Chips&gt;</title>' in html


@pytest.mark.parametrize('version', ['1.2', '2004'])
def test_scorm_and_zip_retain_identity_offline(version, offline):
    from captcher import scorm
    brand = branding.my_brand_profile()
    package = scorm.build_export_scorm_zip('stable-walkthrough-id', brand, version=version)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = archive.read('imsmanifest.xml').decode()
        html = archive.read('index.html').decode()
        assert 'MANIFEST-stable-walkthrough-id' in manifest
        assert 'window.RC_SCORM_VERSION=' + json.dumps(version) in html
        assert 'assets/scorm-api.js' in archive.namelist()
        assert 'Captcher' in html
        assert '<title>Customer task</title>' in html
    with zipfile.ZipFile(io.BytesIO(export.build_export_zip('stable-walkthrough-id', brand))) as archive:
        assert 'customer-task/index.html' in archive.namelist()


def test_saved_profile_values_remain_unchanged(offline):
    profile = {'brand_name': 'Customer', 'tagline': 'Their training',
               'fonts': {'heading': 'Sora', 'body': 'Sora'},
               'colors': {'primary': '#123456', 'accent': '#987654', 'success': '#008877'},
               'logo_dark_url': 'https://customer.invalid/logo.svg', 'show_attribution': False}
    original = json.loads(json.dumps(profile))
    merged = branding._merge_brand(profile)
    assert profile == original
    for key in ('brand_name', 'tagline', 'logo_dark_url', 'show_attribution'):
        assert merged[key] == profile[key]
    for key, value in profile['colors'].items():
        assert merged['colors'][key] == value
    html = export._build_single_file('stable-walkthrough-id', merged, False, 'full', 'player2')
    assert '--primary:#123456;' in html
    assert '--font-head:"Sora", "Outfit"' in html
    assert 'https://customer.invalid/logo.svg' in html
    assert '"show_attribution": false' in html or '"show_attribution":false' in html



