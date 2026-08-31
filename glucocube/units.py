"""What a reading is shown as.

Everything inside this application is mg/dL: what the pollers store, what
the thresholds are compared against, what the forecast works in, and what
GlucoCore sends on the wire whatever a display is set to. One unit, one
comparison, nothing to get wrong on a device with no test suite in front
of it.

This is the other end of that — the last step before a number reaches a
screen, and the first step after one is typed into a form. Most of the
world reads glucose in mmol/L, and a wall display that can only say 6.7
as "121" is a wall display most of the world cannot use.
"""

from . import contract

MGDL = contract.UNITS_MGDL
MMOL = contract.UNITS_MMOL

# The divisor every CGM app uses. The exact molar figure is 18.0182, and
# nobody displays it: matching what the pump app on the same shelf says
# matters more than the third decimal place.
PER_MMOL = contract.MGDL_PER_MMOL

# What people write when they mean mmol/L, including what a config file
# edited by hand is likely to contain.
_MMOL_SPELLINGS = {s.lower() for s in contract.MMOL_SPELLINGS} | {MMOL.lower()}


def normalize(units) -> str:
    """Either unit, from whatever was written down. mg/dL when unsure.

    Unsure means mg/dL because that is what the numbers already are: a
    misread setting then shows the truth in the wrong unit, rather than a
    number divided by eighteen for no reason.
    """
    text = str(units or "").strip().lower().replace(" ", "")
    return MMOL if text in _MMOL_SPELLINGS else MGDL


def is_mmol(units) -> bool:
    return normalize(units) == MMOL


def to_display(mgdl, units):
    """A stored reading in the unit it is to be read in."""
    if mgdl is None:
        return None
    return float(mgdl) / PER_MMOL if is_mmol(units) else float(mgdl)


def from_display(value, units) -> float:
    """A number somebody typed, back into what everything else uses.

    Rounded to whole mg/dL: a threshold is compared against readings that
    are whole mg/dL, and 4.0 mmol/L is 72 rather than 72.0728, which is
    what a config file should say.
    """
    number = float(value)
    return round(number * PER_MMOL) if is_mmol(units) else number


def fmt(mgdl, units, *, blank: str = "---") -> str:
    """A reading, as it should appear: 121, or 6.7."""
    shown = to_display(mgdl, units)
    if shown is None:
        return blank
    return f"{shown:.1f}" if is_mmol(units) else f"{shown:.0f}"


def fmt_delta(delta_mgdl, units, *, blank: str = "") -> str:
    """A change since the last reading, always signed."""
    if delta_mgdl is None:
        return blank
    shown = to_display(delta_mgdl, units)
    return f"{shown:+.1f}" if is_mmol(units) else f"{shown:+.0f}"


def label(units) -> str:
    """The unit as a caption beside a number."""
    return "MMOL/L" if is_mmol(units) else "MG/DL"


def step(units) -> str:
    """What a number field may be nudged by, in this unit."""
    return "0.1" if is_mmol(units) else "1"


def fmt_field(mgdl, units) -> str:
    """A threshold in a form field: no trailing zeroes in mg/dL."""
    shown = to_display(mgdl, units)
    if shown is None:
        return ""
    return f"{shown:.1f}" if is_mmol(units) else f"{shown:g}"
