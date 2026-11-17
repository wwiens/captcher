"""Render real standalone exports with browser networking disabled."""
import copy
import re

import pytest
from playwright.sync_api import sync_playwright

from captcher import branding
from captcher import export
from tests import fixtures


@pytest.mark.parametrize('player', ['player', 'player2'])
@pytest.mark.parametrize('background,text', [('#F4F4EF', '#0B0B0A'),
                                              ('#FFFFFF', '#0B0B0A'),
                                              ('#0B0B0A', '#F4F4EF')])


def test_offline_customer_export_is_readable_and_self_contained(monkeypatch, tmp_path, player, background, text):
    definition = fixtures.walkthrough_def('offline-fixture')
    row = {'id': 'offline-fixture', 'slug': 'offline-fixture', 'definition': definition}
    monkeypatch.setattr(export.store, 'get', lambda _: row)
    monkeypatch.setattr(export.store, 'capture_text', lambda *_: fixtures.page_html('offline'))
    brand = branding.my_brand_profile()
    brand['brand_name'] = 'Customer training'
    brand['colors'].update(background=background, surface=background, text=text)
    original = copy.deepcopy(brand)
    html = export._build_single_file('offline-fixture', brand, False, 'full', player)
    assert brand == original
    target = tmp_path / 'index.html'
    target.write_text(html, encoding='utf-8')
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(offline=True)
        requests = []
        context.on('request', lambda request: requests.append(request.url)
                   if request.url.startswith(('http:', 'https:')) else None)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(target.as_uri())
        page.locator('#intro-modal:not(.hidden)').wait_for(state='visible')
        page.evaluate('document.fonts.ready')
        # Body in DM Sans, headings in Outfit — both from the inlined files.
        assert page.evaluate('document.fonts.check("400 16px \'DM Sans\'")')
        assert page.evaluate('document.fonts.check("700 16px Outfit")')
        logos = page.locator('img.brand-logo')
        assert logos.count() > 0
        for image in logos.all():
            assert image.evaluate('(el) => el.complete && el.naturalWidth > 0')
            assert image.get_attribute('src').startswith('data:image/')
        color = page.locator('#intro-track').evaluate('(el) => getComputedStyle(el).color')
        channels = [int(x) for x in re.findall(r'\d+', color)[:3]]
        color_hex = '#' + ''.join(f'{channel:02X}' for channel in channels)
        assert export._contrast(color_hex, background) >= 4.5
        page.locator('#intro-modal').get_by_role('button', name='You try', exact=True).click()
        page.frame_locator('#sim').locator('tr.clickable').wait_for(state='visible')
        page.frame_locator('#sim').locator('tr.clickable').click()
        page.wait_for_function("document.querySelector('#panel-title').textContent.includes('Step 1 title')")
        assert not requests, requests
        assert not errors, errors
        browser.close()
