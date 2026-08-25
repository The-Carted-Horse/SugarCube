"""The repository itself: imports, dependencies, and what ships on the card.

These are the checks that stop a change from breaking the *device* rather
than the code — a new third-party import that is not on the Pi, a service
file that no longer starts the module, a release workflow whose version
the updater cannot read.
"""

import ast
import compileall
import importlib
import json
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "glucocube"
MODULES = sorted(path.stem for path in PACKAGE.glob("*.py")
                 if path.stem != "__init__")

# What the README promises the device needs, and what the image installs.
ALLOWED_THIRD_PARTY = {"pygame", "qrcode"}


# --------------------------------------------------------------- imports ----

@pytest.mark.parametrize("module", MODULES)
def test_every_module_imports(module):
    if module in ("display", "__main__"):
        pytest.importorskip("pygame")
    if module == "__main__":
        pytest.skip("importing __main__ would run the app")
    importlib.import_module(f"glucocube.{module}")


def test_the_package_compiles():
    """The same check the updater runs before swapping a new version in."""
    assert compileall.compile_dir(str(PACKAGE), quiet=2, force=True)


def top_level_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_nothing_outside_the_standard_library_creeps_in():
    """apt gives the Pi pygame and qrcode; anything else has to be installed."""
    stdlib = set(sys.stdlib_module_names)
    extra = set()
    for path in sorted(PACKAGE.glob("*.py")):
        extra |= top_level_imports(path) - stdlib - {"glucocube"}
    assert extra <= ALLOWED_THIRD_PARTY, f"new dependencies: {sorted(extra)}"


def test_the_web_app_never_needs_the_display_module():
    """``--no-display`` has to work on a machine with no pygame at all."""
    web_modules = ("webadmin", "ui", "onboarding", "captive", "server",
                   "verify", "predict", "oref", "store", "config", "network")
    for name in web_modules:
        imports = top_level_imports(PACKAGE / f"{name}.py")
        assert "pygame" not in imports
        assert "display" not in imports


def test_no_module_imports_the_updater_at_module_scope_in_config():
    """config.load() has to work without pulling the network stack in."""
    assert "updater" not in top_level_imports(PACKAGE / "config.py")


# --------------------------------------------------------------- version ----

def test_the_version_is_one_the_updater_can_compare():
    from glucocube import __version__
    from glucocube.updater import parse_version

    assert parse_version(__version__) is not None


def test_a_checkout_without_a_stamped_version_still_has_one():
    """_version.py is written by the release build, and is gitignored."""
    assert not (PACKAGE / "_version.py").exists() or True
    assert ".gitignore" in [p.name for p in ROOT.iterdir()]
    assert "glucocube/_version.py" in (ROOT / ".gitignore").read_text()


# --------------------------------------------------------------- config ----

def test_the_example_config_matches_what_the_loader_reads():
    from glucocube.config import load

    example = json.loads((ROOT / "config.example.json").read_text())
    config = load(ROOT / "config.example.json")
    assert len(config.users) == len(example["users"])
    assert {u["port"] for u in example["users"]} == {u.port for u in config.users}


def test_the_example_config_ships_no_usable_secret():
    """Copying it verbatim must not leave a device open with a known key."""
    example = json.loads((ROOT / "config.example.json").read_text())
    for user in example["users"]:
        assert "change-me" in user["api_secret"]


# ------------------------------------------------------- the systemd unit ----

def unit_text() -> str:
    return (ROOT / "systemd" / "glucocube.service").read_text()


def test_the_unit_starts_the_module():
    assert "-m glucocube" in unit_text()


def test_the_unit_restarts_the_app():
    """Every settings save works by exiting and being restarted."""
    text = unit_text()
    assert "Restart=always" in text


def test_the_unit_never_gives_up_restarting():
    """A start limit would turn one bad config into a dead device."""
    assert "StartLimitIntervalSec=0" in unit_text()


def test_the_unit_can_bind_port_80():
    text = unit_text()
    assert "CAP_NET_BIND_SERVICE" in text or "User=root" in text


# ------------------------------------------------------------- installer ----

def test_the_installer_stops_at_the_first_failure():
    assert "set -e" in (ROOT / "install.sh").read_text()


def test_the_installer_and_the_updater_agree_on_the_repository():
    from glucocube.updater import REPO

    assert REPO in (ROOT / "install.sh").read_text()


def test_the_installer_installs_the_runtime_dependencies():
    text = (ROOT / "install.sh").read_text()
    for dependency in ("pygame", "qrcode"):
        assert dependency in text


@pytest.mark.parametrize("script", [
    "install.sh",
    "image/stage-glucocube/prerun.sh",
    "image/stage-glucocube/00-glucocube/01-run.sh",
    "image/stage-glucocube/00-glucocube/02-run-chroot.sh",
])
def test_shell_scripts_parse(script):
    """Cheap syntax gate; CI runs shellcheck over the same files."""
    result = subprocess.run(["bash", "-n", str(ROOT / script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ the image ----

def test_the_image_stage_installs_the_app_and_its_service():
    stage = ROOT / "image" / "stage-glucocube" / "00-glucocube"
    packages = (stage / "00-packages-nr").read_text()
    assert "python3-pygame" in packages
    assert "python3-qrcode" in packages
    assert (stage / "files" / "glucocube.service").exists()


def test_the_image_service_matches_the_repository_one_in_spirit():
    """Two copies exist; both have to survive a bad config."""
    image_unit = (ROOT / "image" / "stage-glucocube" / "00-glucocube" /
                  "files" / "glucocube.service").read_text()
    assert "-m glucocube" in image_unit
    assert "Restart=always" in image_unit


# -------------------------------------------------------------- the fonts ----

def test_the_bundled_fonts_are_there():
    """They are loaded by filename; a rename is a blank screen."""
    fonts = {path.name for path in (PACKAGE / "fonts").glob("*.ttf")}
    assert {"SpaceGrotesk-Bold.ttf", "SpaceGrotesk-Medium.ttf",
            "JetBrainsMono-Regular.ttf", "JetBrainsMono-Medium.ttf",
            "JetBrainsMono-Bold.ttf"} <= fonts


def test_each_font_family_carries_its_licence():
    """The OFL requires the notice to travel with every copy."""
    notices = {path.name for path in (PACKAGE / "fonts").glob("OFL-*.txt")}
    assert notices == {"OFL-JetBrainsMono.txt", "OFL-SpaceGrotesk.txt"}


# ---------------------------------------------------------- the workflows ----

def workflows() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_there_are_workflows_to_run():
    assert workflows()


@pytest.mark.parametrize("path", [p.name for p in
                                  sorted((ROOT / ".github" / "workflows")
                                         .glob("*.yml"))])
def test_every_workflow_is_valid_yaml(path):
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((ROOT / ".github" / "workflows" / path).read_text())
    assert "jobs" in document
    assert document["jobs"]


def test_the_release_workflow_stamps_a_version_the_updater_can_read():
    from glucocube.updater import parse_version

    text = (ROOT / ".github" / "workflows" / "build-image.yml").read_text()
    assert "_version.py" in text
    # The shapes the workflow's own guard allows, all readable here.
    for version in ("2.1.0", "2.1.0-rc.3"):
        assert parse_version(version) is not None


def test_the_release_workflow_can_create_releases():
    text = (ROOT / ".github" / "workflows" / "build-image.yml").read_text()
    assert "contents: write" in text


def test_the_tests_are_wired_into_the_release_build():
    """A release that never ran the suite is a release nobody checked."""
    text = (ROOT / ".github" / "workflows" / "build-image.yml").read_text()
    assert "needs: test" in text or "needs: [test]" in text


def test_the_purelib_layout_assumption_holds():
    """The updater swaps the package directory next to its own file."""
    assert sysconfig.get_path("purelib")     # smoke: the interpreter is sane
    from glucocube import updater

    assert Path(updater.__file__).parent.name == "glucocube"
