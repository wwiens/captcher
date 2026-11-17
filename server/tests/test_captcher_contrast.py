"""Cross-surface customer brand contrast must survive the captcher rebrand."""
from captcher import export


def test_signal_on_dark_customer_background_uses_readable_eyebrow():
    accent = '#C6FF3D'
    background = '#0B0B0A'
    result = export.apply_branding('<html><head></head><body></body></html>', {
        'brand_name': 'Customer', 'colors': {'accent': accent, 'background': background}})
    assert f'.eyebrow{{color:{export._readable(accent, background)};}}' in result


def test_signal_on_paper_keeps_the_brand_deep_step():
    result = export.apply_branding('<html><head></head><body></body></html>', {
        'colors': {'accent': '#C6FF3D', 'background': '#F4F4EF'}})
    assert '.eyebrow{color:#4F6B00;}' in result


def test_a_dark_accent_on_a_dark_customer_background_is_lightened_not_blackened():
    # The default accent is sky-deep; a profile started from the
    # defaults and given a dark background used to get a #000000 eyebrow.
    result = export.apply_branding('<html><head></head><body></body></html>', {
        'colors': {'accent': '#0A6FA8', 'background': '#0B0B0A'}})
    colour = export._readable('#0A6FA8', '#0B0B0A')
    assert colour != '#000000'
    assert export._contrast(colour, '#0B0B0A') >= 4.5
    assert f'.eyebrow{{color:{colour};}}' in result


def test_light_grounds_keep_the_darkening_they_always_had():
    # Saved customer profiles on light grounds must export exactly as before.
    assert export._readable('#E0921F', '#FAF7F2') == export._darken('#E0921F', 0.35)
