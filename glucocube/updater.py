"""Self-update from GitHub releases.

A background thread asks the GitHub API for the latest release every few
hours and records the answer in the store (params key "__updates"), where
the settings page, web dashboard, and display footer surface it. Updates
are applied from the settings page — except releases whose notes contain
the marker ``[force-update]``, which install themselves automatically.

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

log = logging.getLogger("glucocube.updater")

# The product is GlucoCube; the repository it lives in is still called
# SugarCube. That mismatch is deliberate — "correcting" it here points the
# update check at a repository that does not exist, and it fails quietly.
REPO = "The-Carted-Horse/SugarCube"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
TARBALL_URL = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
FORCE_MARKER = "[force-update]"
PARAMS_KEY = "__updates"

_apply_lock = threading.Lock()


def current_version() -> str:
    return __version__


def parse_version(s: str) -> tuple | None:
    """'v1.2.3' -> (1, 2, 3); None when it isn't a plain version."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (s or "").strip())
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    cand, cur = parse_version(candidate), parse_version(current)
    if cand is None or cur is None:
        return False
    # (1, 0) == (1, 0, 0)
    length = max(len(cand), len(cur))
    cand += (0,) * (length - len(cand))
    cur += (0,) * (length - len(cur))
    return cand > cur


def fetch_latest() -> dict:
    """Latest (non-draft, non-prerelease) release from the GitHub API."""
    request = urllib.request.Request(
        API_LATEST,
        headers={
            "User-Agent": f"GlucoCube/{current_version()}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.load(response)
    notes = release.get("body") or ""
    name = release.get("name") or ""
    tag = release.get("tag_name") or ""
    return {
        "latest_tag": tag,
        "latest": tag.lstrip("v"),
        "forced": FORCE_MARKER in (notes + " " + name).lower(),
        "url": release.get("html_url") or f"https://github.com/{REPO}/releases",
    }


def check(store) -> dict:
    """Run one update check and persist the outcome for the UIs."""
    state = {
        "current": current_version(),
        "checked_at": int(time.time() * 1000),
        "available": False,
    }
    try:
        latest = fetch_latest()
        state.update(latest)
        state["available"] = is_newer(latest["latest"], state["current"])
    except Exception as exc:  # noqa: BLE001 - network errors are routine
        state["error"] = str(exc)
        log.info("Update check failed: %s", exc)
    if state["available"]:
        log.info("Update available: %s -> %s%s", state["current"],
                 state["latest"], " (forced)" if state["forced"] else "")
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


def check_and_maybe_force(store) -> dict:
    """One check; forced releases install themselves immediately."""
    state = check(store)
    if state.get("available") and state.get("forced"):
        ok, detail = apply_update(state["latest_tag"])
        state["forcing"] = ok
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

    def __init__(self, store):
        super().__init__(name="update-checker", daemon=True)
        self.store = store
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._stop.wait(self.FIRST_CHECK_DELAY)
        while not self._stop.is_set():
            try:
                check_and_maybe_force(self.store)
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.warning("Update checker error: %s", exc)
            self._stop.wait(self.CHECK_SECONDS)
