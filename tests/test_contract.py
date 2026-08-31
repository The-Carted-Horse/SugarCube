"""The one set of numbers both devices lay the dashboard out from.

GlucoCube ships a Raspberry Pi image and ESP32-S3 firmware from this one
repository. ``glucocube/contract.py`` holds every constant that decides what
a person sees, and ``firmware/tools/gen_contract.py`` turns it into the C the
firmware compiles against. These tests are what stops the two drifting: the
generated header has to be in step with the Python, and the Python display
has to actually use it rather than keeping a second copy of the numbers.
"""

import ast
import json
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


# --------------------------------------------------- the release pipeline ----

WORKFLOWS = ROOT / ".github" / "workflows"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


@pytest.mark.parametrize("workflow", ["ci.yml", "build-image.yml"])
def test_every_board_is_built(workflow):
    """A profile the workflow does not name silently stops being published.

    The matrices are written out rather than generated, because GitHub
    Actions cannot read a Python module to build one — so this is what
    notices when a board is added to the contract and nowhere else.
    """
    text = workflow_text(workflow)
    for board in contract.ESP32_BOARDS:
        assert board["id"] in text, (
            f"{workflow} never builds {board['id']}; add it to the board matrix"
        )


def test_the_release_names_both_products():
    """The updater on each device looks for an asset by name."""
    text = workflow_text("build-image.yml")
    assert "glucocube-$BOARD-$VERSION.bin" in text, "no OTA image is published"
    assert "glucocube-$BOARD-$VERSION-factory.bin" in text, (
        "no factory image is published, so a blank board cannot be flashed"
    )
    assert "artifacts/glucocube-image/*" in text, "the Pi image is not attached"


def test_the_firmware_and_the_image_share_one_version():
    """Two builds that work out their own version numbers will disagree."""
    text = workflow_text("build-image.yml")
    assert "needs.version.outputs.version" in text
    # Both builds have to consume the shared version job rather than
    # recomputing one; the image stamps _version.py and the firmware gets
    # GC_VERSION, and they must be the same string.
    assert "-DGC_VERSION=" in text
    assert "__version__ = " in text


def test_the_web_installer_covers_every_board():
    page = (ROOT / "docs" / "flash" / "index.html").read_text()
    for board in contract.ESP32_BOARDS:
        assert board["id"] in page, (
            f"{board['id']} is missing from the web installer"
        )
        assert board["name"] in page


def test_the_manifest_writer_matches_the_release_names(tmp_path):
    """The installer flashes what the release actually publishes."""
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/scripts/write_manifests.py"),
         "--version", "9.9.9", "--tag", "v9.9.9", "--out", str(tmp_path)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for board in contract.ESP32_BOARDS:
        manifest = json.loads(
            (tmp_path / f"manifest-{board['id']}.json").read_text())
        path = manifest["builds"][0]["parts"][0]["path"]
        assert path.endswith(
            f"glucocube-{board['id']}-9.9.9-factory.bin"), path
        assert manifest["builds"][0]["parts"][0]["offset"] == 0, (
            "a factory image is written from the start of flash"
        )


# ------------------------------------------------------- the geometry it makes ----

PANEL_W, PANEL_H = 800, 480


def derived(people: int) -> dict:
    """The pixel sizes the layout fractions come to on an 800x480 panel.

    Both products compute these the same way — Python's ``int(h * f)`` and
    C's ``(int)(h * f)`` both truncate — so this is one table describing two
    screens. It is written out rather than derived in the assertion so that
    a change to a fraction has to be looked at, not just re-run.
    """
    layout = contract.LAYOUT
    footer_h = max(layout["footer_h_px"], int(PANEL_H * layout["footer_h"]))
    h = PANEL_H - footer_h
    px = max(layout["footer_px_min"], int(footer_h * layout["footer_px_h"]))
    return {
        "footer_h": footer_h,
        "panel_h": h,
        "panel_w": PANEL_W // people,
        "name": int(h * layout["name_px_h"]),
        "badge": int(h * layout["badge_px_h"]),
        "reading": int(h * layout["num_px_h"]),
        "delta": int(h * layout["delta_px_h"]),
        "chart_top": int(h * layout["chart_top_h"]),
        "chart_h": int(h * layout["chart_height_h"]),
        "stats_y": int(h * layout["stats_label_y_h"]),
        "stat_value": int(h * layout["stats_value_px_h"]),
        "footer_px": px,
        "toggle_w": max(layout["toggle_w_px"], px * layout["toggle_w_ratio"]),
        "toggle_h": max(layout["toggle_h_px"],
                        footer_h * layout["toggle_h_ratio"]),
        "qr_w": max(layout["qr_w_px"], px * layout["qr_w_ratio"]),
    }


def test_the_dashboard_is_the_same_size_on_both_panels():
    """The Pi's 7" and the ESP32's 5" are both 800x480, so this is one
    drawing at one size — not two that resemble each other."""
    assert derived(1) == {
        "footer_h": 34, "panel_h": 446, "panel_w": 800,
        "name": 23, "badge": 14, "reading": 147, "delta": 35,
        "chart_top": 236, "chart_h": 89, "stats_y": 363, "stat_value": 36,
        "footer_px": 11, "toggle_w": 121, "toggle_h": 68, "qr_w": 110,
    }


def test_a_second_person_only_narrows_the_panel():
    """Splitting the screen changes the width, never the type sizes — the
    reading shrinks only when it would not fit, which draw_panel decides
    from the rendered width rather than from the number of people."""
    one, two = derived(1), derived(2)
    assert two["panel_w"] == 400
    for key in ("panel_h", "name", "reading", "chart_h", "stat_value"):
        assert one[key] == two[key], f"{key} changed with the person count"


def test_the_touch_targets_clear_a_fingertip():
    """44px is the smallest target a finger reliably hits.

    The toggle is taller than the footer on purpose, so it overhangs the
    bottom of the panel. A touch controller clamps to the panel, which
    leaves half of it — still over the floor, which is what matters.
    """
    geometry = derived(1)
    on_screen_h = PANEL_H - (geometry["footer_h"] + geometry["footer_h"] // 2
                             - geometry["toggle_h"] // 2)
    assert geometry["toggle_w"] >= 44
    assert on_screen_h >= 44, (
        f"the theme toggle is only {on_screen_h}px tall once the touch "
        "controller clamps it to the panel"
    )
    assert geometry["qr_w"] >= 44


def test_the_version_reaches_the_firmware_compiler():
    """A -D on the command line is a CMake variable, not a #define.

    The release workflow passes -DGC_VERSION; without the line below it
    would set a variable nothing reads, every build would call itself
    0.0.0, and no device would ever see an update — silently, because a
    firmware that thinks it is 0.0.0 still runs perfectly well.
    """
    text = (ROOT / "firmware" / "CMakeLists.txt").read_text()
    assert 'add_compile_definitions(GC_VERSION="${GC_VERSION}")' in text
    assert 'add_compile_definitions(GC_BOARD_ID="${GC_BOARD}")' in text


def test_the_release_build_checks_the_stamp_took():
    """And the workflow proves it on the artifact, not just in the source."""
    text = workflow_text("build-image.yml")
    assert "Check the version was stamped in" in text


# ------------------------------------------------- what each product serves ----

FIRMWARE_HTTPD = ROOT / "firmware/components/gc_httpd/gc_httpd.c"


def firmware_routes() -> set[str]:
    """The paths the firmware's web app registers."""
    text = FIRMWARE_HTTPD.read_text()
    table = text.split("static const httpd_uri_t ROUTES[]")[1].split("};")[0]
    routes = set(re.findall(r'\{"(/[^"]*)"', table))
    routes |= set(contract.CAPTIVE_PROBE_PATHS)
    return routes


# Paths both products answer. A phone with one bookmarked finds the same
# page on the other, so this list is an interface, not an implementation
# detail — adding to it on the Pi means adding it here too.
SHARED_ROUTES = {
    "/",
    "/setup",
    "/settings",
    "/settings/access",
    "/settings/clock",
    "/settings/network",
    "/settings/people",
    "/settings/person",
    "/settings/person/remove",
    "/settings/ranges",
    "/settings/updates",
    "/settings/updates/channel",
    "/settings/glucocore/unpair",
    "/log",
    "/api/dashboard.json",
    "/api/health.json",
    "/api/log.json",
    "/api/source/test",
    "/api/wifi.json",
    "/screen.png",
    "/update/check",
}

# What the Raspberry Pi serves and the firmware does not, and why. This is
# the honest answer to "what is lost on the ESP32" — kept here rather than
# only in prose, so that closing one of these gaps means deleting a line
# from a test rather than remembering to update a README.
PI_ONLY_ROUTES = {
    "/settings/screen": "no live view of the panel; see /screen.png",
    "/settings/weather": "weather belongs to ambient mode",
    "/settings/person/wallpaper": "wallpapers belong to ambient mode",
    "/settings/glucocore/signin": "pairing is by code or by scanning, not by "
                                  "handing a wall display an account password",
    "/settings/glucocore/register": "the display registers itself when it pairs",
    "/settings/glucocore/pair": "covered by /settings/pairing",
    "/settings/glucocore/cancel": "covered by /settings/pairing",
    "/api/pairing.json": "scan-to-pair is not wired up yet",
    "/display/theme": "the theme is a tap on the panel or the ranges page",
    "/fonts/": "the firmware's pages are set in the system font",
    "/wifi": "folded into /setup",
    "/wifi/rescan": "the scan refreshes itself; see /api/wifi.json",
    "/update/apply": "covered by POST /settings/updates",
}


@pytest.mark.parametrize("route", sorted(SHARED_ROUTES))
def test_the_firmware_serves_what_the_pi_does(route):
    assert route in firmware_routes(), (
        f"the Pi serves {route} and the firmware does not. Either add it to "
        f"gc_httpd's ROUTES, or move it to PI_ONLY_ROUTES with the reason."
    )


@pytest.mark.parametrize("route", sorted(PI_ONLY_ROUTES))
def test_what_the_firmware_does_not_serve_is_written_down(route):
    """A gap that has been closed should stop being described as a gap."""
    assert route not in firmware_routes(), (
        f"the firmware now serves {route}; take it out of PI_ONLY_ROUTES "
        f"and add it to SHARED_ROUTES."
    )


def test_the_captive_portal_answers_every_probe():
    """A probe the firmware does not answer is a phone that sits on the
    setup network waiting to be told what to do."""
    routes = firmware_routes()
    for path in contract.CAPTIVE_PROBE_PATHS:
        assert path in routes
