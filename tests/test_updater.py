"""updater.py — version comparison and the release check.

Version ordering is what decides whether a device replaces its own running
code, so the parametrised ordering tests are deliberately exhaustive around
the pre-release boundary: rc.10 must beat rc.9, and 2.0.1 must beat every
2.0.1-rc.N.

Nothing here touches the network — ``_get_json`` is stubbed with the shapes
the GitHub releases API actually returns.
"""

import json
import tarfile
import urllib.error
from pathlib import Path

import pytest

from glucocube import updater
from glucocube.updater import (
    BETA,
    STABLE,
    _release_state,
    _sort_key,
    is_newer,
    is_prerelease,
    parse_version,
)


def release(tag, *, prerelease=False, draft=False, body="", name="", url=None):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft,
            "body": body, "name": name,
            "html_url": url or f"https://example.invalid/{tag}"}


# ------------------------------------------------------- parse_version ----

@pytest.mark.parametrize("text, expected", [
    ("v1.2.3", ((1, 2, 3), 1, 0, 0)),
    ("1.2.3", ((1, 2, 3), 1, 0, 0)),
    ("v2.0", ((2, 0), 1, 0, 0)),
    ("v2", ((2,), 1, 0, 0)),
    ("v1.2.3-rc.2", ((1, 2, 3), 0, 3, 2)),
    ("v1.2.3rc1", ((1, 2, 3), 0, 3, 1)),
    ("v1.2.3-beta2", ((1, 2, 3), 0, 2, 2)),
    ("V1.2.3-RC.4", ((1, 2, 3), 0, 3, 4)),
    ("  v1.2.3  ", ((1, 2, 3), 1, 0, 0)),
])
def test_parse_version_understands_the_tags_we_publish(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("junk", [
    "", None, "latest", "v", "nightly-2024-01-01", "v1.2.3+build7", "release-1",
])
def test_parse_version_refuses_what_it_cannot_order(junk):
    assert parse_version(junk) is None


def test_padding_makes_two_and_three_part_versions_comparable():
    assert _sort_key(parse_version("v2.0"), 3) == _sort_key(parse_version("v2.0.0"), 3)


# ------------------------------------------------------------ is_newer ----

@pytest.mark.parametrize("candidate, current", [
    ("v2.0.1", "v2.0.0"),
    ("v2.1.0", "v2.0.9"),
    ("v3.0.0", "v2.9.9"),
    ("v2.0.1", "v2.0.1-rc.1"),       # the release beats its own rehearsals
    ("v2.0.1-rc.2", "v2.0.1-rc.1"),
    ("v2.0.1-rc.10", "v2.0.1-rc.9"),  # not string order
    ("v2.0.1-rc.1", "v2.0.0"),
    ("v2.0.1-rc.1", "v2.0.1-beta.3"),  # rc outranks beta
])
def test_is_newer_is_true_for_an_upgrade(candidate, current):
    assert is_newer(candidate, current)


@pytest.mark.parametrize("candidate, current", [
    ("v2.0.0", "v2.0.0"),
    ("v2.0", "v2.0.0"),
    ("v2.0.0", "v2.0"),
    ("v2.0.0", "v2.0.1"),
    ("v2.0.1-rc.1", "v2.0.1"),
    ("v2.0.1-rc.1", "v2.0.1-rc.2"),
    ("v1.9.9", "v2.0.0"),
])
def test_is_newer_is_false_for_the_same_or_older(candidate, current):
    assert not is_newer(candidate, current)


@pytest.mark.parametrize("candidate, current", [
    ("latest", "v2.0.0"),
    ("v2.0.0", "some-branch"),
    ("", "v2.0.0"),
    ("v2.0.0", ""),
])
def test_an_unreadable_version_never_looks_like_an_upgrade(candidate, current):
    """The safe direction for something that replaces the running code."""
    assert not is_newer(candidate, current)


@pytest.mark.parametrize("version, expected", [
    ("v2.0.1-rc.1", True), ("v2.0.1-beta2", True), ("v2.0.1", False),
    ("v2.0", False), ("nonsense", False),
])
def test_is_prerelease(version, expected):
    assert is_prerelease(version) is expected


def test_the_running_version_is_one_the_updater_can_read():
    """A device stamped with an unreadable version never updates again."""
    assert parse_version(updater.current_version()) is not None


# ------------------------------------------------------- release state ----

def test_release_state_strips_the_leading_v():
    state = _release_state(release("v2.1.0"))
    assert state["latest_tag"] == "v2.1.0"
    assert state["latest"] == "2.1.0"
    assert state["forced"] is False


@pytest.mark.parametrize("field, text", [
    ("body", "Fixes the thing. [force-update]"),
    ("name", "2.1.1 [FORCE-UPDATE]"),
])
def test_the_force_marker_is_found_in_notes_or_title(field, text):
    assert _release_state(release("v2.1.1", **{field: text}))["forced"] is True


def test_release_state_falls_back_to_the_releases_page():
    state = _release_state({"tag_name": "v2.1.0"})
    assert state["url"] == updater.RELEASES_URL


# ------------------------------------------------------- fetch_latest ----

def stub_listing(monkeypatch, listing, latest=None):
    calls = []

    def fake_get_json(url):
        calls.append(url)
        if url == updater.API_RELEASES:
            if isinstance(listing, Exception):
                raise listing
            return listing
        if isinstance(latest, Exception):
            raise latest
        return latest or {}

    monkeypatch.setattr(updater, "_get_json", fake_get_json)
    return calls


def test_stable_ignores_pre_releases_and_drafts(monkeypatch):
    stub_listing(monkeypatch, [
        release("v2.2.0-rc.1", prerelease=True),
        release("v2.3.0", draft=True),
        release("v2.1.0"),
    ])
    assert updater.fetch_latest(STABLE)["latest_tag"] == "v2.1.0"


def test_beta_offers_the_pre_release(monkeypatch):
    stub_listing(monkeypatch, [
        release("v2.2.0-rc.1", prerelease=True),
        release("v2.1.0"),
    ])
    assert updater.fetch_latest(BETA)["latest_tag"] == "v2.2.0-rc.1"


def test_the_newest_release_is_by_version_not_by_publication_order(monkeypatch):
    """A patch to an old line, published later, is not an upgrade."""
    stub_listing(monkeypatch, [
        release("v1.9.5"),      # published most recently
        release("v2.1.0"),
    ])
    assert updater.fetch_latest(STABLE)["latest_tag"] == "v2.1.0"


def test_releases_with_unreadable_tags_are_skipped(monkeypatch):
    stub_listing(monkeypatch, [release("nightly"), release("v2.1.0")])
    assert updater.fetch_latest(STABLE)["latest_tag"] == "v2.1.0"


def test_stable_falls_back_to_the_latest_endpoint(monkeypatch):
    calls = stub_listing(monkeypatch, RuntimeError("listing down"),
                         latest=release("v2.1.0"))
    assert updater.fetch_latest(STABLE)["latest_tag"] == "v2.1.0"
    assert updater.API_LATEST in calls


def test_beta_has_no_fallback_and_says_so(monkeypatch):
    stub_listing(monkeypatch, RuntimeError("listing down"))
    with pytest.raises(RuntimeError, match="listing down"):
        updater.fetch_latest(BETA)


def test_beta_with_nothing_published_reports_nothing(monkeypatch):
    """Not the stable release wearing a beta label."""
    stub_listing(monkeypatch, [], latest=release("v2.1.0"))
    state = updater.fetch_latest(BETA)
    assert state["latest_tag"] == ""
    assert state["latest"] == ""


# -------------------------------------------------------------- check ----

def test_check_records_an_available_update(store, monkeypatch):
    stub_listing(monkeypatch, [release("v99.0.0")])
    state = updater.check(store, STABLE)
    assert state["available"] is True
    assert state["latest_tag"] == "v99.0.0"
    assert store.get_params(updater.PARAMS_KEY)["available"] is True


def test_check_records_being_up_to_date(store, monkeypatch):
    stub_listing(monkeypatch, [release(f"v{updater.current_version()}")])
    assert updater.check(store, STABLE)["available"] is False


def test_check_offers_the_way_back_from_a_pre_release(store, monkeypatch):
    """Standard channel + a device on an rc = step back onto the release."""
    monkeypatch.setattr(updater, "current_version", lambda: "2.1.0-rc.3")
    stub_listing(monkeypatch, [release("v2.0.9")])
    state = updater.check(store, STABLE)
    assert state["available"] is True
    assert state["rejoin"] is True


def test_check_does_not_offer_a_step_back_to_a_beta_device(store, monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "2.1.0-rc.3")
    stub_listing(monkeypatch, [release("v2.1.0-rc.3", prerelease=True)])
    assert updater.check(store, BETA)["available"] is False


def test_check_survives_the_network_being_down(store, monkeypatch):
    stub_listing(monkeypatch,
                 urllib.error.URLError("Name or service not known"))
    state = updater.check(store, BETA)
    assert state["available"] is False
    assert "error" in state
    assert store.get_params(updater.PARAMS_KEY)["error"]


def test_a_cleared_error_does_not_linger(store, monkeypatch):
    """The state is replaced, not merged — stale errors would never clear."""
    stub_listing(monkeypatch, urllib.error.URLError("down"))
    updater.check(store, STABLE)
    stub_listing(monkeypatch, [release("v99.0.0")])
    assert "error" not in store.get_params(updater.PARAMS_KEY)


def test_check_normalizes_the_channel(store, monkeypatch):
    stub_listing(monkeypatch, [release("v99.0.0")])
    assert updater.check(store, "NONSENSE")["channel"] == STABLE


def test_check_and_maybe_force_installs_a_forced_release(store, monkeypatch):
    applied = []
    stub_listing(monkeypatch, [release("v99.0.0", body="[force-update]")])
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])
    state = updater.check_and_maybe_force(store, STABLE)
    assert applied == ["v99.0.0"]
    assert state["forcing"] is True


def test_check_and_maybe_force_leaves_an_ordinary_release_alone(store, monkeypatch):
    applied = []
    stub_listing(monkeypatch, [release("v99.0.0")])
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])
    updater.check_and_maybe_force(store, STABLE)
    assert applied == []


def test_a_failed_forced_install_is_recorded(store, monkeypatch):
    stub_listing(monkeypatch, [release("v99.0.0", body="[force-update]")])
    monkeypatch.setattr(updater, "apply_update", lambda tag: (False, "disk full"))
    state = updater.check_and_maybe_force(store, STABLE)
    assert state["forcing"] is False
    assert state["error"] == "disk full"


def test_switching_channel_installs_that_channels_release(store, monkeypatch):
    """Including backwards, off a pre-release onto the last full one."""
    applied = []
    monkeypatch.setattr(updater, "current_version", lambda: "2.1.0-rc.3")
    stub_listing(monkeypatch, [release("v2.0.9")])
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])
    state = updater.check_and_switch(store, STABLE)
    assert applied == ["v2.0.9"]
    assert state["switching"] is True


def test_switching_to_a_channel_with_nothing_new_installs_nothing(store, monkeypatch):
    applied = []
    stub_listing(monkeypatch, [release(f"v{updater.current_version()}")])
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])
    updater.check_and_switch(store, STABLE)
    assert applied == []


# --------------------------------------------------- UpdateChecker ----

class FakeConfig:
    def __init__(self, channel):
        self.update_channel = channel


@pytest.mark.parametrize("channel, expected", [
    ("beta", BETA), ("stable", STABLE), ("nonsense", STABLE), (None, STABLE),
])
def test_the_checker_reads_the_channel_live(store, channel, expected):
    """Changing it on the settings page must not need a reboot."""
    checker = updater.UpdateChecker(store, FakeConfig(channel))
    assert checker.channel == expected


def test_the_checker_defaults_to_stable_without_a_config(store):
    assert updater.UpdateChecker(store).channel == STABLE


# ------------------------------------------------- applying a tarball ----

def build_release_tarball(tmp_path: Path, source: Path, version: str) -> Path:
    """A stand-in for GitHub's source tarball: one top-level directory."""
    tar_path = tmp_path / "release.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(source, arcname=f"SugarCube-{version}/glucocube")
    return tar_path


class FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size=-1):
        data, self._data = self._data, b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_root(tmp_path: Path) -> Path:
    """A pretend install: root/glucocube/{__init__,marker}.py."""
    root = tmp_path / "install"
    pkg = root / "glucocube"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "2.0.0"\n')
    (pkg / "marker.py").write_text("WHICH = 'old'\n")
    return root


def test_a_tarball_update_swaps_the_package_and_keeps_the_old_one(
        tmp_path, monkeypatch):
    root = install_root(tmp_path)
    new_pkg = tmp_path / "new" / "glucocube"
    new_pkg.mkdir(parents=True)
    (new_pkg / "__init__.py").write_text('__version__ = "0.0.0"\n')
    (new_pkg / "marker.py").write_text("WHICH = 'new'\n")
    tarball = build_release_tarball(tmp_path, new_pkg, "2.1.0")

    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(tarball.read_bytes()))
    updater._apply_tarball(root, root / "glucocube", "v2.1.0")

    assert (root / "glucocube" / "marker.py").read_text() == "WHICH = 'new'\n"
    assert (root / "glucocube.prev" / "marker.py").read_text() == "WHICH = 'old'\n"
    assert '"2.1.0"' in (root / "glucocube" / "_version.py").read_text()


def test_a_tarball_that_does_not_compile_is_refused(tmp_path, monkeypatch):
    """The syntax check is the last thing between a bad release and a brick."""
    root = install_root(tmp_path)
    broken = tmp_path / "broken" / "glucocube"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("def oops(:\n")
    tarball = build_release_tarball(tmp_path, broken, "2.1.0")

    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(tarball.read_bytes()))
    with pytest.raises(RuntimeError, match="syntax check"):
        updater._apply_tarball(root, root / "glucocube", "v2.1.0")

    assert (root / "glucocube" / "marker.py").read_text() == "WHICH = 'old'\n"


def test_a_tarball_without_the_package_is_refused(tmp_path, monkeypatch):
    root = install_root(tmp_path)
    other = tmp_path / "other" / "somethingelse"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("")
    tar_path = tmp_path / "release.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(other, arcname="SugarCube-2.1.0/somethingelse")

    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(tar_path.read_bytes()))
    with pytest.raises(RuntimeError, match="no glucocube package"):
        updater._apply_tarball(root, root / "glucocube", "v2.1.0")

    assert (root / "glucocube" / "marker.py").exists()


def test_no_temporary_directory_is_left_behind(tmp_path, monkeypatch):
    root = install_root(tmp_path)
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(b"not a tarball"))
    with pytest.raises(Exception):
        updater._apply_tarball(root, root / "glucocube", "v2.1.0")
    assert not list(root.glob(".update-*"))


def test_apply_update_refuses_to_run_twice_at_once(monkeypatch):
    assert updater._apply_lock.acquire(blocking=False)
    try:
        ok, message = updater.apply_update("v2.1.0")
        assert ok is False
        assert "already in progress" in message
    finally:
        updater._apply_lock.release()


def test_the_repo_the_device_checks_matches_the_release_workflow():
    """"Correcting" the repo name here points updates at a 404."""
    assert updater.REPO == "The-Carted-Horse/SugarCube"
    assert updater.API_LATEST.endswith("/releases/latest")
    assert json.dumps(updater.TARBALL_URL).count("{tag}") == 1


# ------------------------------------------------- applying from git ----

def test_a_git_checkout_is_updated_with_git(tmp_path, monkeypatch):
    """install.sh installs a checkout; the tarball path is for the image."""
    root = install_root(tmp_path)
    (root / ".git").mkdir()
    calls = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda args, **kwargs: (calls.append(args), Done())[1])

    updater._apply_git(root, "v2.1.0")

    assert [args[3] for args in calls] == ["fetch", "checkout"]
    assert calls[-1][-1] == "v2.1.0"


def test_a_failed_git_checkout_is_reported(tmp_path, monkeypatch):
    root = install_root(tmp_path)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "error: pathspec 'v9.9.9' did not match"

    monkeypatch.setattr(updater.subprocess, "run", lambda args, **kw: Failed())
    with pytest.raises(RuntimeError, match="did not match"):
        updater._apply_git(root, "v9.9.9")


def test_the_version_stamp_is_what_the_next_boot_reads(tmp_path):
    pkg = tmp_path / "glucocube"
    pkg.mkdir()
    updater._stamp_version(pkg, "2.1.0")
    assert (pkg / "_version.py").read_text().strip() == '__version__ = "2.1.0"'


# --------------------------------------------------------- boot guard ----

def test_a_healthy_start_clears_the_failed_boot_counter(tmp_path, monkeypatch):
    """Three failed starts restore glucocube.prev; a good boot has to count."""
    counter = tmp_path / ".boot-fails"
    counter.write_text("2\n")
    monkeypatch.setattr(updater, "BOOT_FAILS", counter)

    timers = []
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda delay, fn: timers.append((delay, fn)) or
                        type("T", (), {"start": lambda self: None,
                                       "daemon": True})())
    updater.mark_boot_ok_later()

    delay, callback = timers[0]
    assert delay == updater.BOOT_OK_SECONDS
    callback()
    assert counter.read_text().strip() == "0"


def test_an_unwritable_counter_does_not_crash_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "BOOT_FAILS",
                        tmp_path / "no-such-directory" / ".boot-fails")
    timers = []
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda delay, fn: timers.append(fn) or
                        type("T", (), {"start": lambda self: None,
                                       "daemon": True})())
    updater.mark_boot_ok_later()
    timers[0]()          # must not raise
