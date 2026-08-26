"""units.py — the last step before a number reaches a screen.

Everything inside the device is mg/dL. These are the conversions at the
edges, and the property that matters most is that they are each other's
inverse: a threshold typed in mmol/L, stored in mg/dL and rendered back
must be the number that was typed, or somebody's urgent low drifts every
time they open the page.
"""

import pytest

from glucocube import units


# --------------------------------------------------------- which unit ----

@pytest.mark.parametrize("given", [
    "mmol/L", "mmol/l", "MMOL/L", " mmol ", "mmol", "mm", "MMOLL",
])
def test_the_ways_people_write_mmol(given):
    assert units.normalize(given) == units.MMOL
    assert units.is_mmol(given) is True


@pytest.mark.parametrize("given", [
    "mg/dL", "mg/dl", "MG/DL", "", None, "nonsense", 5, "mgdl",
])
def test_everything_else_is_mgdl(given):
    """Unsure means mg/dL: that is what the stored numbers already are."""
    assert units.normalize(given) == units.MGDL
    assert units.is_mmol(given) is False


# ------------------------------------------------------------ reading ----

@pytest.mark.parametrize("mgdl, expected", [
    (121, "121"), (70, "70"), (5.4, "5"), (0, "0"), (400, "400"),
])
def test_mgdl_reads_as_a_whole_number(mgdl, expected):
    assert units.fmt(mgdl, "mg/dL") == expected


@pytest.mark.parametrize("mgdl, expected", [
    (121, "6.7"), (72, "4.0"), (180, "10.0"), (55, "3.1"), (400, "22.2"),
])
def test_mmol_reads_to_one_decimal(mgdl, expected):
    assert units.fmt(mgdl, "mmol/L") == expected


def test_a_reading_that_has_not_arrived_reads_as_nothing():
    assert units.fmt(None, "mg/dL") == "---"
    assert units.fmt(None, "mmol/L") == "---"
    assert units.fmt(None, "mmol/L", blank="--") == "--"


@pytest.mark.parametrize("units_name, delta, expected", [
    ("mg/dL", 3, "+3"), ("mg/dL", -12, "-12"), ("mg/dL", 0, "+0"),
    ("mmol/L", 3, "+0.2"), ("mmol/L", -12, "-0.7"), ("mmol/L", 0, "+0.0"),
])
def test_a_change_is_always_signed(units_name, delta, expected):
    assert units.fmt_delta(delta, units_name) == expected


def test_no_change_yet_is_not_a_zero():
    """A first reading has nothing to compare against, which is not "+0"."""
    assert units.fmt_delta(None, "mg/dL") == ""


def test_the_caption_beside_the_number():
    assert units.label("mg/dL") == "MG/DL"
    assert units.label("mmol/L") == "MMOL/L"


# --------------------------------------------------------- typing one in ----

@pytest.mark.parametrize("typed, expected", [
    (4.0, 72), (10.0, 180), (3.1, 56), (22.2, 400),
])
def test_a_number_typed_in_mmol_is_stored_as_whole_mgdl(typed, expected):
    """A threshold is compared against readings that are whole mg/dL."""
    assert units.from_display(typed, "mmol/L") == expected


def test_a_number_typed_in_mgdl_is_left_alone():
    assert units.from_display(70, "mg/dL") == 70
    assert units.from_display("180", "mg/dL") == 180


@pytest.mark.parametrize("typed", [
    "3.9", "4.0", "5.5", "6.7", "7.8", "10.0", "13.3", "2.2", "22.2",
])
def test_a_threshold_survives_the_round_trip(typed):
    """Type it, store it, render it: the same number comes back."""
    stored = units.from_display(float(typed), "mmol/L")
    assert units.fmt_field(stored, "mmol/L") == typed


@pytest.mark.parametrize("mgdl", [70, 180, 55, 250, 39, 400])
def test_an_mgdl_threshold_survives_it_too(mgdl):
    stored = units.from_display(mgdl, "mg/dL")
    assert units.fmt_field(stored, "mg/dL") == str(mgdl)


def test_a_form_field_does_not_show_a_pointless_decimal():
    """70.0 in a box invites somebody to wonder what the .0 is for."""
    assert units.fmt_field(70.0, "mg/dL") == "70"
    assert units.fmt_field(70, "mg/dL") == "70"
    # In mmol/L the decimal is the point, so it stays.
    assert units.fmt_field(72, "mmol/L") == "4.0"


def test_an_empty_threshold_stays_empty():
    """Blank means "use the shared range", not zero."""
    assert units.fmt_field(None, "mmol/L") == ""


def test_what_a_number_field_may_be_nudged_by():
    assert units.step("mg/dL") == "1"
    assert units.step("mmol/L") == "0.1"


# ------------------------------------------------------------ the ratio ----

def test_the_divisor_is_the_one_every_other_app_uses():
    """A reading shown as 5.6 here and 5.6 in the pump app is the point."""
    assert units.PER_MMOL == 18.0


def test_conversion_is_linear_so_a_chart_keeps_its_shape():
    """The display plots mg/dL and only labels the axis in the reader's unit."""
    a, b = units.to_display(100, "mmol/L"), units.to_display(200, "mmol/L")
    assert b == pytest.approx(a * 2)
