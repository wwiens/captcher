"""Real Chromium checks of the rebranded application, using isolated fixtures."""
import os
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from captcher import auth
from captcher import server
from conftest import new_member
from tests import fixtures


def test_captcher_real_browser_pages_and_themes():
    owner = new_member()
    walkthrough = fixtures.make_walkthrough(owner)
    httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    screenshots = os.environ.get('CAPTCHER_TEST_SCREENSHOTS', '')
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            # No paid/network services may be reached from this browser run.
            context.route('**/*', lambda route: route.continue_()
                          if route.request.url.startswith(base + '/')
                          or route.request.url.startswith(('data:', 'blob:'))
                          else route.abort())
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            # The one page before an owner exists: the access-token unlock page.
            saved_token = auth.ACCESS_TOKEN
            auth.ACCESS_TOKEN = 'correct horse battery staple'
            try:
                response = page.goto(base + '/unlock')
                assert response.status == 200
                page.evaluate('document.fonts.ready')
                assert 'Captcher' in page.title()
                assert 'Shadow Capture' not in page.locator('body').inner_text()
                assert 'DM Sans' in page.locator('body').evaluate('(el) => getComputedStyle(el).fontFamily')
                assert 'Outfit' in page.locator('h1').evaluate('(el) => getComputedStyle(el).fontFamily')
            finally:
                auth.ACCESS_TOKEN = saved_token
            context.add_cookies([{'name': auth.SESSION_COOKIE,
                                 'value': auth.make_session(owner), 'url': base}])
            for name, route in (
                ('catalog', '/'), ('account', '/account'), ('branding', '/branding'),
                ('extension', '/extension'), ('getting-started', '/getting-started'),
                ('review', f'/review/{walkthrough["id"]}'),
                ('player', f'/player/{walkthrough["id"]}'),
                ('player1', f'/player1/{walkthrough["id"]}'),
            ):
                response = page.goto(base + route)
                assert response.status == 200, route
                page.evaluate('document.fonts.ready')
                assert not page.url.endswith('/unlock'), route
                assert '__TOOLBAR__' not in page.content()
                if name not in ('player', 'player1'):
                    for mode in ('light', 'dark'):
                        page.evaluate('(mode) => window.scTheme.set(mode)', mode)
                        assert page.locator('html').get_attribute('data-sc-theme') == mode
                        if screenshots:
                            folder = Path(screenshots)
                            folder.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(folder / f'{name}-{mode}.png'), full_page=True)
                    page.evaluate('window.scTheme.set("system")')
                    page.emulate_media(color_scheme='light')
                    assert page.locator('html').get_attribute('data-sc-theme') == 'light'
                else:
                    page.locator('#intro-modal').get_by_role('button', name='You try', exact=True).click()
                    captured = page.frame_locator('#sim').locator('tr.clickable')
                    captured.wait_for(state='visible')
                    # Brand governs chrome only: the captured page keeps its own
                    # font. The fixture sets none, so this is the browser default,
                    # whose name varies by platform ("Times" on macOS).
                    family = captured.evaluate('(el) => getComputedStyle(el).fontFamily')
                    assert not any(f in family for f in ('DM Sans', 'Outfit')), family
                    captured.click()
                    # Successful try-mode actions auto-advance by default.
                    page.wait_for_function("document.querySelector('#panel-title').textContent.includes('Step 1 title')")
                    page.frame_locator('#sim').locator('tr.clickable').wait_for(state='visible')
                    assert 'Step 1 title' in page.locator('#panel-title').inner_text()
                    if screenshots:
                        page.screenshot(path=str(Path(screenshots) / f'{name}-playing.png'), full_page=True)
                    page.locator('#mode-show').click()
                    assert 'active' in (page.locator('#mode-show').get_attribute('class') or '')
            page.goto(base + '/')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            if screenshots:
                page.screenshot(path=str(Path(screenshots) / 'catalog-mobile.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
