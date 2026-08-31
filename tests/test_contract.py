"""The one set of numbers both devices lay the dashboard out from.

GlucoCube ships a Raspberry Pi image and ESP32-S3 firmware from this one
repository. ``glucocube/contract.py`` holds every constant that decides what
a person sees, and ``firmware/tools/gen_contract.py`` turns it into the C the
firmware compiles against. These tests are what stops the two drifting: the
generated header has to be in step with the Python, and the Python display
has to actually use it rather than keeping a second copy of the numbers.
"""

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from glucocube import contract

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "firmware" / "tools" / "gen_contract.py"
HEADER = ROOT / "firmware/components/gc_contract/include/gc_contract.h"
SOURCE = ROOT / "firmware/components/gc_contract/gc_contract.c"


# ------------------------------------------------------------ generated ----

def test_the_generated_c_is_in_step_with_the_python():
    """Edit contract.py, regenerate. This is the check that says so."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "The firmware's copy of the contract is stale. Run:\n"
        "    python3 firmware/tools/gen_contract.py"
    )


def test_the_generated_files_are_committed():
    assert HEADER.exists(), f"{HEADER} is missing"
    assert SOURCE.exists(), f"{SOURCE} is missing"


def header_defines() -> dict[str, str]:
    text = HEADER.read_text()
    return dict(re.findall(r"^#define\s+(GC_\w+)\s+(.+?)\s*$", text, re.MULTILINE))


def as_number(literal: str) -> float:
    return float(literal.rstrip("f"))


@pytest.mark.parametrize("name,value", sorted(contract.LAYOUT.items()))
def test_every_layout_number_reaches_the_firmware(name, value):
    defines = header_defines()
    key = f"GC_L_{name.upper()}"
    assert key in defines, f"{name} never made it into the header"
    assert as_number(defines[key]) == pytest.approx(float(value))


@pytest.mark.parametrize("role", contract.PALETTE_ROLES)
def test_every_colour_reaches_the_firmware(role):
    source = SOURCE.read_text()
    for theme, colors in contract.PALETTES.items():
        red, green, blue = colors[role]
        literal = "0x%02X%02X%02X" % (red, green, blue)
        expected = f"[GC_C_{role.upper()}] = {literal},"
        block = source.split(f"[GC_THEME_{theme.upper()}] = {{")[1]
        assert expected in block.split("},")[0], (
            f"{theme}/{role} is not {literal} in the generated table"
        )


@pytest.mark.parametrize("name,expected", [
    ("GC_LOW_DEFAULT", contract.THRESHOLD_DEFAULTS["low"]),
    ("GC_HIGH_DEFAULT", contract.THRESHOLD_DEFAULTS["high"]),
    ("GC_URGENT_LOW_DEFAULT", contract.THRESHOLD_DEFAULTS["urgent_low"]),
    ("GC_URGENT_HIGH_DEFAULT", contract.THRESHOLD_DEFAULTS["urgent_high"]),
    ("GC_STALE_MINUTES_DEFAULT", contract.STALE_MINUTES_DEFAULT),
    ("GC_CLAMP_LO", contract.CLAMP_LO),
    ("GC_CLAMP_HI", contract.CLAMP_HI),
    ("GC_MIN_5M_CARBIMPACT", contract.MIN_5M_CARBIMPACT),
    ("GC_UAM_DECAY_STEPS", contract.UAM_DECAY_STEPS),
    ("GC_STEP_MIN", contract.STEP_MIN),
    ("GC_CHART_HISTORY_MINUTES", contract.CHART_HISTORY_MINUTES),
    ("GC_CHART_FORECAST_MINUTES", contract.CHART_FORECAST_MINUTES),
    ("GC_CONE_RATE_DEVICE", contract.CONE_RATE_DEVICE),
    ("GC_CONE_RATE_ESTIMATE", contract.CONE_RATE_ESTIMATE),
    ("GC_HORIZON_FAR", contract.HORIZONS[-1]),
])
def test_the_numbers_that_change_a_reading_match(name, expected):
    assert as_number(header_defines()[name]) == pytest.approx(float(expected))


# ----------------------------------------------------- the Python side ----

def test_the_display_draws_from_the_contract():
    """The palettes on screen are the contract's, not a second copy."""
    pygame = pytest.importorskip("pygame")
    del pygame
    from glucocube import display

    for theme, colors in contract.PALETTES.items():
        palette = display.THEMES[theme]
        for role, rgb in colors.items():
            assert getattr(palette, role) == rgb, f"{theme}/{role} has drifted"


def test_the_palette_roles_are_the_dataclass_fields():
    pytest.importorskip("pygame")
    from glucocube import display

    fields = set(display.Palette.__dataclass_fields__) - {"name"}
    assert fields == set(contract.PALETTE_ROLES)
    for colors in contract.PALETTES.values():
        assert set(colors) == set(contract.PALETTE_ROLES)


def test_no_module_keeps_its_own_copy_of_a_contract_number():
    """A literal that also lives in the contract is a fork waiting to happen."""
    watched = {
        "glucocube/oref.py": ["39, 401", "8.0", "0.25, 4.0"],
        "glucocube/predict.py": ["(30, 60, 90, 120)"],
        "glucocube/updater.py": ['"[force-update]"',
                                 '"The-Carted-Horse/SugarCube"'],
    }
    for name, literals in watched.items():
        text = (ROOT / name).read_text()
        for literal in literals:
            assert literal not in text, (
                f"{name} still spells out {literal}; it belongs in contract.py"
            )


def test_the_contract_imports_nothing():
    """It is data. An import here is a dependency on both devices."""
    tree = ast.parse((ROOT / "glucocube" / "contract.py").read_text())
    imports = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports == []


def test_every_board_profile_is_complete():
    """A board the release names has to say what it is."""
    required = {"id", "name", "chip", "width", "height", "flash_mb", "psram_mb"}
    ids = set()
    for board in contract.ESP32_BOARDS:
        assert required <= set(board), f"{board.get('id')} is missing fields"
        assert re.fullmatch(r"[a-z0-9-]+", board["id"]), (
            f"{board['id']} has to be safe in a release asset name"
        )
        assert (ROOT / "firmware" / "boards" / board["id"]).is_dir(), (
            f"no firmware/boards/{board['id']}/ for this profile"
        )
        ids.add(board["id"])
    assert len(ids) == len(contract.ESP32_BOARDS), "duplicate board id"


# ------------------------------------------------------- the firmware port ----

def test_the_golden_vectors_are_in_step_with_the_python():
    """The C forecast is checked against these; stale ones check nothing."""
    generator = ROOT / "firmware" / "host_test" / "gen_vectors.py"
    result = subprocess.run(
        [sys.executable, str(generator), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "The forecast changed but the golden vectors did not. Run:\n"
        "    python3 firmware/host_test/gen_vectors.py"
    )


@pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None,
                    reason="no host C compiler")
def test_the_firmware_forecast_matches_the_python():
    """Builds gc_oref/gc_predict/gc_store with a host compiler and runs them
    over the golden vectors. This is the check that says the Raspberry Pi and
    the ESP32 show the same person the same number."""
    result = subprocess.run(
        ["make", "-s", "-C", str(ROOT / "firmware" / "host_test"), "run"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failures" in result.stdout, result.stdout
