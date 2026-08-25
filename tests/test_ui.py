"""ui.py — the shared HTML components.

The device serves these pages to a phone that has no internet (it is on
the setup hotspot), so the tests check the two properties that keeps
true: every page is self-contained, and every value that came from a
person is escaped before it reaches the markup.
"""

import re

import pytest

from glucocube import ui

XSS = '<script>alert("x")</script>'


# ------------------------------------------------------------------ esc ----

@pytest.mark.parametrize("raw, expected", [
    ("<b>", "&lt;b&gt;"),
    ('"quoted"', "&quot;quoted&quot;"),
    ("a & b", "a &amp; b"),
    ("it's", "it&#x27;s"),
    (None, ""),
    (42, "42"),
])
def test_values_are_escaped_for_attributes_and_text(raw, expected):
    assert ui.esc(raw) == expected


# ----------------------------------------------------------------- page ----

def test_a_page_is_a_complete_document():
    html = ui.page("Title", "<p>body</p>")
    assert html.startswith("<!DOCTYPE html>")
    assert "<title>Title</title>" in html
    assert "<p>body</p>" in html
    assert html.rstrip().endswith("</html>")


def test_a_page_carries_its_own_stylesheet_and_script():
    """No CDN: the phone loading this has no route to the internet."""
    html = ui.page("Title", "")
    assert "<style>" in html
    assert "http://" not in html.replace("http-equiv", "")
    assert "https://" not in html


def test_a_page_is_sized_for_a_phone():
    assert 'name="viewport"' in ui.page("Title", "")


def test_a_page_title_is_escaped():
    title = re.search(r"<title>(.*?)</title>", ui.page(XSS, "")).group(1)
    assert "<script>" not in title
    assert "&lt;script&gt;" in title


def test_a_meta_refresh_is_added_only_when_asked():
    assert "http-equiv" not in ui.page("T", "")
    assert 'content="5;url=/settings"' in ui.page("T", "", refresh="5;url=/settings")


def test_navigation_is_opt_in():
    assert "<nav>" not in ui.page("T", "")
    assert "<nav>" in ui.page("T", "", nav=True)


def test_a_sub_page_leads_with_the_way_back():
    html = ui.page("T", "", nav=True, back="/settings/people",
                   back_label="People")
    assert 'href="/settings/people"' in html
    assert "People" in html


# ------------------------------------------------------------ controls ----

def test_a_text_input_carries_its_name_and_value():
    html = ui.text_input("email", "cassidy@example.invalid")
    assert 'name="email"' in html
    assert 'value="cassidy@example.invalid"' in html


def test_an_input_value_cannot_break_out_of_the_attribute():
    html = ui.text_input("name", '"><script>alert(1)</script>')
    assert "<script>" not in html
    assert "&quot;&gt;" in html


def test_a_password_field_can_be_revealed():
    """Typing a Wi-Fi passphrase blind, then waiting to learn it was wrong."""
    html = ui.password_input("wifi_password", "hunter2")
    assert 'type="password"' in html
    assert 'class="reveal"' in html
    assert 'autocapitalize="none"' in html


def test_a_copy_field_is_read_only_and_copyable():
    html = ui.copy_input("api_secret", "abc123")
    assert "readonly" in html
    assert 'class="copy"' in html


def test_a_select_marks_the_current_value():
    html = ui.select("tz", [("", "None"), ("Europe/London", "Europe/London")],
                     selected="Europe/London")
    assert '<option value="Europe/London" selected>' in html
    assert '<option value="" selected>' not in html


def test_select_options_are_escaped():
    html = ui.select("x", [(XSS, XSS)])
    assert "<script>" not in html


def test_a_checkbox_reflects_its_state():
    assert " checked" in ui.checkbox("hidden", "Hidden network", True)
    assert " checked" not in ui.checkbox("hidden", "Hidden network", False)


def test_choice_cards_check_the_selected_option():
    html = ui.choice_cards("source", [
        ("push", "Push", "Trio uploads to this device"),
        ("tidepool", "Tidepool", "for twiist"),
    ], selected="tidepool")
    assert html.count('type="radio"') == 2
    assert '<input type="radio" name="source" value="tidepool" checked' in html


def test_a_conditional_group_is_hidden_server_side():
    """With JavaScript off — or before it runs — it must not flash into view."""
    shown = ui.group("source", "tidepool", "<p>creds</p>", current="tidepool")
    hidden = ui.group("source", "tidepool", "<p>creds</p>", current="push")
    assert " hidden" not in shown
    assert " hidden>" in hidden


def test_a_group_may_apply_to_several_values():
    html = ui.group("source", ["tidepool", "nightscout"], "<p>x</p>",
                    current="nightscout")
    assert 'data-when="tidepool nightscout"' in html
    assert " hidden" not in html


@pytest.mark.parametrize("percent, lit", [
    (0, 1), (29, 1), (30, 2), (54, 2), (55, 3), (77, 3), (78, 4), (100, 4),
])
def test_signal_strength_lights_the_right_number_of_bars(percent, lit):
    html = ui.signal_bars(percent)
    assert html.count('class="on"') == lit
    assert f'aria-label="{percent}% signal"' in html


# ----------------------------------------------------- network picker ----

NETWORKS = [{"ssid": "Home", "signal": 82, "secured": True},
            {"ssid": "Open Guest", "signal": 40, "secured": False}]


def test_every_network_is_a_tappable_option():
    html = ui.network_picker(NETWORKS)
    assert 'value="Home"' in html
    assert 'value="Open Guest"' in html
    assert 'value="__other__"' in html


def test_a_secured_network_shows_a_lock_and_an_open_one_does_not():
    html = ui.network_picker(NETWORKS)
    assert "&#128274;" in html
    assert ">open<" in html


def test_the_chosen_network_is_selected():
    html = ui.network_picker(NETWORKS, selected="Home")
    assert '<input type="radio" name="wifi_ssid" value="Home" checked' in html


def test_a_network_not_in_the_list_falls_to_other():
    """Out of range, or hidden — the manual field opens for it."""
    html = ui.network_picker(NETWORKS, selected="Elsewhere")
    assert '<input type="radio" name="wifi_ssid" value="__other__" checked' in html
    assert "wifi_other_ssid" in html


def test_a_hidden_network_keeps_its_checkbox_ticked():
    html = ui.network_picker(NETWORKS, other_ssid="Secret", hidden=True)
    assert 'value="Secret"' in html
    assert 'name="wifi_hidden"' in html
    assert " checked" in html


def test_a_network_name_with_markup_in_it_is_escaped():
    html = ui.network_picker([{"ssid": XSS, "signal": 50, "secured": True}])
    assert "<script>" not in html


def test_an_empty_scan_still_offers_other_network():
    """A device that cannot see anything must not become a dead end."""
    html = ui.network_picker([])
    assert 'value="__other__"' in html


# --------------------------------------------------------------- menus ----

def test_a_menu_row_is_a_whole_row_link_with_a_state_line():
    html = ui.menu_item("/settings/network", "Wi-Fi", "Home (82%)")
    assert 'href="/settings/network"' in html
    assert "Wi-Fi" in html and "Home (82%)" in html


def test_a_menu_row_can_carry_a_badge():
    html = ui.menu_item("/settings/updates", "Updates", "2.0.0",
                        badge="Update", badge_kind="warn")
    assert 'class="pill warn"' in html


def test_menu_text_is_escaped():
    assert "<script>" not in ui.menu_item("/x", XSS, XSS)


def test_facts_escape_labels_but_pass_value_markup_through():
    html = ui.facts([("Address", '<a href="/x">link</a>'), (XSS, "value")])
    assert '<a href="/x">link</a>' in html
    assert "<script>" not in html


def test_a_banner_names_its_kind():
    assert 'class="banner err"' in ui.banner("err", "Something went wrong")


def test_every_icon_is_valid_svg_markup():
    for name in ui.ICONS:
        svg = ui.icon(name)
        assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_an_unknown_icon_is_an_empty_shape_not_a_crash():
    assert ui.icon("no-such-icon").startswith("<svg")


def test_the_stylesheet_defines_both_themes():
    """The display and the web app both offer day and night."""
    assert "[data-theme=light]" in ui.STYLE
    assert "[data-theme=dark]" in ui.STYLE


def test_no_page_element_has_an_unbalanced_tag_count():
    """A cheap structural check across a realistic page."""
    html = ui.page("Settings", ui.menu([
        ui.menu_item("/settings/people", "People", "2 configured"),
        ui.menu_item("/settings/ranges", "Ranges", "70-180"),
    ]) + ui.banner("ok", "Saved"), nav=True)
    for tag in ("div", "span", "a"):
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        assert opens == closes, f"unbalanced <{tag}>"
