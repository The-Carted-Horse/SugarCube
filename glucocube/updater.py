"""Self-update from GitHub releases.

A background thread asks the GitHub API for the newest release on the
device's channel every few hours and records the answer in the store
(params key "__updates"), where the settings page, web dashboard, and
display footer surface it. Updates are applied from the settings page —
except releases whose notes contain the marker ``[force-update]``, which
install themselves automatically.

Two channels, chosen on the settings page and stored in config.json:

  - ``stable`` — full releases only, the same set GitHub calls "latest";
  - ``beta``   — pre-releases as well, so testers get them first.

Switching channel installs that channel's newest release straight away,
which for beta -> stable means stepping *back* onto the last full
release. That is the point: the channel says which releases this device
runs, not merely which ones it is told about.

Applying an update swaps the ``glucocube`` package in place:
  - a git checkout (install.sh installs) fetches and checks out the tag;
  - anything else (the SD-card image) downloads the release tarball,
    syntax-checks it, and atomically swaps the package directory, keeping
    the previous version next to it as ``glucocube.prev``.
Either way the process then exits so systemd restarts it on the new code
(the same restart-on-exit pattern the settings page uses).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__, synclog
from . import config as config_mod

log = logging.getLogger("glucocube.updater")

# The product is GlucoCube; the repository it lives in is still called
# SugarCube. That mismatch is deliberate — "correcting" it here points the
# update check at a repository that does not exist, and it fails quietly.
REPO = "The-Carted-Horse/SugarCube"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TARBALL_URL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
FORCE_MARKER = "[force-update]"
PARAMS_KEY = "__updates"

STABLE = "stable"
BETA = "beta"

_apply_lock = threading.Lock()


def current_version() -> str:
    return __version__


# vX.Y.Z, optionally followed by a pre-release label: v2.1.0-rc.1,
# v2.1.0-beta2, v2.1.0rc1 all parse. Anything else is left alone —
# an unrecognised version simply never compares as newer, which is the
# safe direction for something that replaces the running code.
_VERSION_RE = re.compile(
    r"v?(?P<nums>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<label>alpha|beta|rc|pre)[-_.]?(?P<n>\d+)?)?",
    re.IGNORECASE,
)
# Ordering within one release number, lowest first. A release with no
# label at all outranks every pre-release of the same number, which is
# what the RELEASE rank below encodes.
_PRE_RANKS = {"alpha": 0, "pre": 1, "beta": 2, "rc": 3}
_PRERELEASE, _RELEASE = 0, 1


def parse_version(s: str) -> tuple | None:
    """'v1.2.3' -> ((1, 2, 3), 1, 0, 0); 'v1.2.3-rc.2' -> ((1, 2, 3), 0, 3, 2).

    The tail is the rank: a pre-release carries the same numbers as the
    finished version but sorts below it, so a device running 2.0.1-rc.2 is
    offered the real 2.0.1 when it lands. None when the string is not a
    version this device can reason about at all.
    """
    m = _VERSION_RE.fullmatch((s or "").strip())
    if not m:
        return None
    nums = tuple(int(part) for part in m.group("nums").split("."))
    label = (m.group("label") or "").lower()
    if not label:
        return (nums, _RELEASE, 0, 0)
    return (nums, _PRERELEASE, _PRE_RANKS.get(label, 0),
            int(m.group("n") or 0))


def _sort_key(parsed: tuple, width: int = 4) -> tuple:
    """Comparable form, with (1, 0) and (1, 0, 0) padded to the same shape."""
    nums, stage, rank, number = parsed
    nums = tuple(nums) + (0,) * max(0, width - len(nums))
    return (nums, stage, rank, number)


def is_prerelease(version: str) -> bool:
    parsed = parse_version(version)
    return bool(parsed) and parsed[1] == _PRERELEASE


def is_newer(candidate: str, current: str) -> bool:
    cand, cur = parse_version(candidate), parse_version(current)
    if cand is None or cur is None:
        return False
    width = max(len(cand[0]), len(cur[0]))
    return _sort_key(cand, width) > _sort_key(cur, width)


def _get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"GlucoCube/{current_version()}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _release_state(release: dict) -> dict:
    notes = release.get("body") or ""
    name = release.get("name") or ""
    tag = release.get("tag_name") or ""
    return {
        "latest_tag": tag,
        "latest": tag.lstrip("v"),
        "prerelease": bool(release.get("prerelease")),
        "forced": FORCE_MARKER in (notes + " " + name).lower(),
        "url": release.get("html_url") or RELEASES_URL,
    }


def fetch_latest(channel: str = STABLE) -> dict:
    """The newest release this channel offers.

    Newest by version number rather than by publication date: a patch to
    an older line, published after a newer one, must not look like an
    upgrade. The stable channel falls back to /releases/latest — the one
    endpoint that is guaranteed to skip pre-releases — if the listing is
    unavailable; the beta channel has no such fallback and says so.
    """
    channel = config_mod.normalize_channel(channel)
    releases = None
    try:
        releases = _get_json(API_RELEASES)
    except Exception as exc:  # noqa: BLE001 - fall back below, or re-raise
        if channel == BETA:
            raise
        log.info("Release listing unavailable (%s); using /releases/latest", exc)
    if isinstance(releases, list):
        usable = [
            (parse_version(r.get("tag_name") or ""), r)
            for r in releases
            if not r.get("draft")
            and (channel == BETA or not r.get("prerelease"))
        ]
        usable = [(v, r) for v, r in usable if v]
        if usable:
            return _release_state(max(usable, key=lambda pair: _sort_key(pair[0]))[1])
        if channel == BETA:
            # Nothing on this channel at all: say so plainly rather than
            # quietly reporting the stable release as the beta one.
            return {"latest_tag": "", "latest": "", "prerelease": False,
                    "forced": False, "url": RELEASES_URL}
    return _release_state(_get_json(API_LATEST))


def check(store, channel: str = STABLE) -> dict:
    """Run one update check and persist the outcome for the UIs."""
    channel = config_mod.normalize_channel(channel)
    state = {
        "current": current_version(),
        "channel": channel,
        "checked_at": int(time.time() * 1000),
        "available": False,
    }
    try:
        latest = fetch_latest(channel)
        state.update(latest)
        state["available"] = is_newer(latest["latest"], state["current"])
        if (channel == STABLE and not state["available"]
                and is_prerelease(state["current"])
                and latest["latest"]
                and latest["latest"] != state["current"]):
            # A device left on a pre-release after moving to the standard
            # channel is not "up to date": the release it should be
            # running is the newest full one, even though its number is
            # lower. Offer it, and let the page call it a step back.
            state["available"] = True
            state["rejoin"] = True
    except Exception as exc:  # noqa: BLE001 - network errors are routine
        state["error"] = str(exc)
        log.info("Update check failed: %s", exc)
    if state["available"]:
        log.info("Update available on %s: %s -> %s%s", channel,
                 state["current"], state["latest"],
                 " (forced)" if state.get("forced") else "")
    # Replace, don't merge: "no longer available" and "error cleared"
    # must actually clear.
    store.replace_params(PARAMS_KEY, state)
    return state


def _stamp_version(pkg_dir: Path, version: str) -> None:
    (pkg_dir / "_version.py").write_text(f'__version__ = "{version}"\n')


def _apply_git(root: Path, tag: str) -> None:
    for args in (["fetch", "--tags", "--force", "origin"],
                 ["checkout", "-f", tag]):
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {proc.stderr.strip()}")


def _apply_tarball(root: Path, pkg: Path, tag: str) -> None:
    # Everything stays under root so the final renames never cross
    # filesystems (tmpfs /tmp would break os.replace).
    workdir = Path(tempfile.mkdtemp(prefix=".update-", dir=root))
    try:
        url = TARBALL_URL.format(tag=tag)
        request = urllib.request.Request(
            url, headers={"User-Agent": f"GlucoCube/{current_version()}"})
        tar_path = workdir / "release.tar.gz"
        with urllib.request.urlopen(request, timeout=120) as response, \
                open(tar_path, "wb") as out:
            shutil.copyfileobj(response, out)
        with tarfile.open(tar_path) as tar:
            try:
                tar.extractall(workdir, filter="data")
            except TypeError:
                # Python < 3.11.4 has no filter= yet; the archive comes
                # from our own release, so plain extraction is acceptable.
                tar.extractall(workdir)
        candidates = list(workdir.glob("*/glucocube/__init__.py"))
        if not candidates:
            raise RuntimeError("release tarball has no glucocube package")
        new_pkg = candidates[0].parent
        _stamp_version(new_pkg, tag.lstrip("v"))
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(new_pkg)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"new version failed a syntax check: {proc.stdout.strip()}")
        prev = root / "glucocube.prev"
        if prev.exists():
            shutil.rmtree(prev)
        os.replace(pkg, prev)
        try:
            os.replace(new_pkg, pkg)
        except BaseException:
            os.replace(prev, pkg)   # put the running version back
            raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def apply_update(tag: str) -> tuple[bool, str]:
    """Install the given release tag, then exit so systemd restarts us."""
    if not _apply_lock.acquire(blocking=False):
        return False, "an update is already in progress"
    pkg = Path(__file__).resolve().parent
    root = pkg.parent
    log.info("Applying update %s in %s", tag, root)
    synclog.add("update", "system", f"installing {tag}")
    try:
        if (root / ".git").is_dir() and shutil.which("git"):
            _apply_git(root, tag)
            _stamp_version(pkg, tag.lstrip("v"))
        else:
            _apply_tarball(root, pkg, tag)
    except Exception as exc:  # noqa: BLE001 - report, keep running
        _apply_lock.release()
        log.warning("Update to %s failed: %s", tag, exc)
        synclog.add("update", "system", f"update to {tag} failed: {exc}",
                    ok=False)
        return False, str(exc)
    log.info("Update %s installed; restarting", tag)
    synclog.add("update", "system", f"{tag} installed; restarting")
    # Exit after the caller's HTTP response has flushed; systemd restarts
    # the service on the new code (Restart=always). The lock stays held so
    # nothing can start a second swap during the shutdown window.
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return True, tag.lstrip("v")


def check_and_maybe_force(store, channel: str = STABLE) -> dict:
    """One check; forced releases install themselves immediately."""
    state = check(store, channel)
    if state.get("available") and state.get("forced"):
        ok, detail = apply_update(state["latest_tag"])
        state["forcing"] = ok
        if not ok:
            state["error"] = detail
        store.replace_params(PARAMS_KEY, state)
    return state


def check_and_switch(store, channel: str) -> dict:
    """Check a channel just chosen, and install what it offers.

    Changing channel is a request to run that channel's releases, so this
    does not wait to be asked twice — including when the move is
    backwards, from a pre-release onto the last full release.
    """
    state = check(store, channel)
    if state.get("available") and state.get("latest_tag"):
        ok, detail = apply_update(state["latest_tag"])
        state["switching"] = ok
        if not ok:
            state["error"] = detail
        store.replace_params(PARAMS_KEY, state)
    return state


BOOT_FAILS = Path(__file__).resolve().parent.parent / ".boot-fails"
BOOT_OK_SECONDS = 90


def mark_boot_ok_later() -> None:
    """Clear the failed-boot counter once the app has survived startup.

    The systemd unit's ExecStartPre guard increments the counter on every
    start; three strikes restore ``glucocube.prev``. Clearing it after
    ~90s of healthy runtime is what makes a good boot "count".
    """
    def _ok():
        try:
            BOOT_FAILS.write_text("0\n")
        except OSError:
            pass
    timer = threading.Timer(BOOT_OK_SECONDS, _ok)
    timer.daemon = True
    timer.start()


class UpdateChecker(threading.Thread):
    """Checks for releases every few hours; forced ones self-install."""

    CHECK_SECONDS = 6 * 3600
    FIRST_CHECK_DELAY = 60      # let boot/network settle first

    def __init__(self, store, config=None):
        super().__init__(name="update-checker", daemon=True)
        self.store = store
        # The live Config object, so a channel changed on the settings
        # page is picked up by the next check rather than at the next
        # boot. None (tests, callers that predate channels) means stable.
        self.config = config
        self._stopping = threading.Event()

    @property
    def channel(self) -> str:
        return config_mod.normalize_channel(
            getattr(self.config, "update_channel", STABLE))

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        self._stopping.wait(self.FIRST_CHECK_DELAY)
        while not self._stopping.is_set():
            try:
                check_and_maybe_force(self.store, self.channel)
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.warning("Update checker error: %s", exc)
            self._stopping.wait(self.CHECK_SECONDS)
