"""Source-level contracts for captcher chrome, independent of services."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "captcher"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_captcher_palette_geometry_and_type():
    """Brand Guide v1 palette, UI v2 geometry: Indigo leads; light is :root and
    dark is derived; controls and cards are soft and nothing but the logo is a
    square."""
    css = read("static/captcher.css")
    for token, value in {"indigo": "#3B49C6", "sky": "#29B6FF", "sky-deep": "#0A6FA8",
                         "mint": "#3ECFA2", "coral": "#FF6A5C", "ink": "#14151A",
                         "slate": "#4B4F5A", "fog": "#E6E7EC", "mist": "#F2F3F7",
                         "paper": "#FAFAF9"}.items():
        assert re.search(rf"--{token}:\s*{value};", css), token
    assert re.search(r'--font-sans:\s*"DM Sans"', css)
    assert re.search(r'--font-display:\s*"Outfit"', css)
    assert "--line: 1px;" in css
    assert "--radius-control: 12px;" in css
    assert "--radius-container: 18px;" in css
    assert "--radius-pill: 999px;" in css
    # Outfit semibold headings, not black.
    assert "--weight-heading: 600;" in css
    assert re.search(r"h1 \{[^}]*font-weight: var\(--weight-heading\)", css)
    # No squares outside the mark: the nav uses line icons, a live capture is a
    # coral dot, status dots are round and the stepping-squares motif is gone.
    assert ".nav-link::before" not in css and ".nav-icon" in css
    assert ".rec-square" not in css
    assert re.search(r"\.rec-dot \{[^}]*border-radius: 50%", css)
    assert re.search(r"\.dot \{[^}]*border-radius: 50%", css)
    assert ".motif-squares" not in css
    assert re.search(r'\.input\[type="search"\] \{[^}]*border-radius: var\(--radius-pill\)', css)
    assert re.search(r"\.chip \{[^}]*border-radius: var\(--radius-pill\)", css)
    assert "border-left: 4px" not in css
    assert '[data-sc-theme="dark"]' in css and '[data-sc-theme="light"]' not in css
    assert "color-scheme: light" in css and "color-scheme: dark" in css
    assert "--primary: var(--indigo);" in css
    assert "--showme: var(--sky-deep);" in css and "--youtry: var(--indigo);" in css
    # No hard offset shadows or lift-on-hover from the previous identity.
    assert "translate(-2px, -2px)" not in css
    assert not re.search(r"\b\d+px \d+px 0 ", css)
    assert "prefers-reduced-motion: reduce" in css


def test_templates_present_title_case_identity_and_the_mark():
    for path in (ROOT / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        # Recorder 0.3.0 is Captcher Recorder, so no template names the old
        # brand any more — not even the former transitional instruction lines.
        assert "Shadow Capture" not in text, path.name
        if "<title>" in text:
            assert "Captcher</title>" in text, path.name
        # Lowercase "captcher" is allowed only as part of an identifier: a URL,
        # an attribute, the captcher.css file name, or a `captcher:` event name
        # shared with the recorder.
        identifiers = text.replace("captcher.app", "").replace("captcher.css", "")
        assert not re.search(r">[^<]*(?<![\w/.@-])captcher(?![\w/:-])[^<]*<", identifiers), path.name
    toolbar = read("templates/_toolbar.html")
    assert 'aria-label="Captcher home"' in toolbar
    assert '<span class="brand-word">Captcher</span>' in toolbar
    assert 'class="site-header app-sidebar"' in toolbar
    logo = read("templates/_logo.svg")
    assert 'viewBox="0 0 526 526"' in logo
    assert logo.count('fill="__STROKE__"') == 4  # the frame keeps the logo_svg caller contract
    for colour in ("#FF6A5C", "#29B6FF", "#3B49C6", "#3ECFA2"):
        assert colour in logo
    from captcher import server
    assert 'currentColor' in server.logo_svg()
    theme = read("templates/_theme-head.html")
    assert '"#F2F3F7" : "#0F1117"' in theme
    assert 'var KEY = "sc-theme"' in theme
    assert 'window.scTheme' in theme
    assert '"sc:theme"' in theme
    assert 'captcher:capture-present' in read("templates/_extension-panel.html")
    assert 'dataset.captcherCapture' in read("templates/_extension-panel.html")


def test_brand_preview_bundles_dm_sans_without_losing_legacy_fonts():
    text = read("templates/branding.html")
    fonts = re.search(r'var INLINED_FONTS = (.*?);', text).group(1)
    for family in ('"dm sans": 1', '"outfit": 1', '"sora": 1', '"jetbrains mono": 1'):
        assert family in fonts
    assert '--b-head:"Outfit"; --b-body:"DM Sans"' in text
    assert 'c.fonts.heading || "Outfit"' in text
    assert 'c.fonts.body || "DM Sans"' in text
    # The pre-v1 lime accent keeps its deep step on the old Paper ground only, so
    # profiles duplicated from the old built-in one preview as they export.
    assert 'var PLATFORM_ACCENT = "#C6FF3D", PLATFORM_ACCENT_TEXT = "#4F6B00"' in text
    assert '(c.colors.background || "").toUpperCase() === "#F4F4EF"' in text
    assert 'Powered by Captcher' in text


def test_catalog_cards_use_soft_interaction_states():
    text = read("templates/_catalog-script.html")
    assert 'border-radius:var(--radius-container)' in text
    assert '.card:hover { border-color:var(--border-strong); box-shadow:var(--shadow-soft); }' in text
    assert 'box-shadow:var(--focus-ring)' in text
    assert 'translate(-2px, -2px)' not in text and 'border-width:3px' not in text
    assert 'id="cat-record"' not in read("templates/catalog.html")


def test_appearance_and_computed_chrome_in_chromium():
    from playwright.sync_api import sync_playwright

    html = ('<!doctype html><html><head><meta name="theme-color">'
            + read("templates/_theme-head.html")
            + '<style>' + read("static/captcher.css") + '</style></head>'
            + '<body><a class="brand-lockup"><span class="brand-word">Captcher</span></a>'
            + '<button class="button button--primary">Publish</button>'
            + '<button class="button" disabled>Disabled</button>'
            + '<input class="input" aria-label="Title"></body></html>')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(color_scheme="dark")
        page.route("http://captcher.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto("http://captcher.test/")
        # Light is the default even when the OS prefers dark: nothing stored yet.
        assert page.evaluate("scTheme.get()") == "light"
        assert page.locator("html").get_attribute("data-sc-theme") == "light"
        assert page.locator("body").evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(242, 243, 247)"
        primary = page.locator(".button--primary")
        for choice, expected in [("dark", "rgb(15, 17, 23)"), ("light", "rgb(242, 243, 247)")]:
            page.evaluate("value => scTheme.set(value)", choice)
            assert page.evaluate("localStorage.getItem('sc-theme')") == choice
            assert page.locator("body").evaluate("e => getComputedStyle(e).backgroundColor") == expected
            assert primary.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(59, 73, 198)"
            assert primary.evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"
            assert primary.evaluate("e => getComputedStyle(e).borderTopLeftRadius") == "12px"
        page.reload()
        assert page.evaluate("scTheme.get()") == "light"
        page.evaluate("scTheme.set('system')")
        page.wait_for_function("document.documentElement.dataset.scTheme === 'dark'")
        page.emulate_media(color_scheme="light")
        page.wait_for_function("document.documentElement.dataset.scTheme === 'light'")
        assert "DM Sans" in primary.evaluate("e => getComputedStyle(e).fontFamily")
        page.locator("input").focus()
        page.wait_for_timeout(250)  # the ring transitions in
        assert "59, 73, 198" in page.locator("input").evaluate("e => getComputedStyle(e).boxShadow")
        browser.close()
