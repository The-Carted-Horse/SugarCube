"""wallpaper.py — the art behind a person, and the caches that make it free.

Two things are being protected here. One is the frame rate: a 7" Pi cannot
decode and rescale a photograph every second, so the scaled surface is
cached and the test says so. The other is that nothing about a picture may
ever reach the draw loop as an exception — a corrupt file, a missing one, a
service that is down all have to end in a flat background.
"""

import struct
import zlib

import pytest

from glucocube import wallpaper
from helpers import FakeResponse, RecordingOpener

pygame = pytest.importorskip("pygame")

ID = "a" * 32
OTHER = "b" * 32


def png_bytes(width=64, height=40):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    rows = b"".join(b"\x00" + bytes([(x * 4) % 256 for x in range(width)
                                     for _ in range(3)])
                    for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


class Person:
    def __init__(self, art=""):
        self.wallpaper = art


class Screen:
    def __init__(self, art=""):
        self.wallpaper = art


@pytest.fixture
def database(tmp_path):
    return str(tmp_path / "glucocube.db")


# ------------------------------------------------------------ the grammar --

@pytest.mark.parametrize("value, ok", [
    ("a" * 32, True), ("A" * 32, False), ("a" * 31, False),
    ("", False), ("bundled:reeds", False), ("none", False),
])
def test_only_a_32_hex_name_is_something_to_fetch(value, ok):
    assert wallpaper.is_id(value) is ok


def test_a_person_with_art_of_their_own_keeps_it():
    assert wallpaper.resolve(Screen("bundled:tide"),
                             Person("bundled:reeds")) == "bundled:reeds"


def test_a_person_with_none_falls_through_to_the_display():
    assert wallpaper.resolve(Screen("bundled:tide"), Person()) == "bundled:tide"


def test_none_is_not_the_same_as_unset():
    """The whole reason the two are distinct.

    Somebody who has chosen a picture for themselves in GlucoCore appears
    on displays they do not own, and whoever owns the wall needs a way to
    say "not behind them, on mine" that is not "unset".
    """
    assert wallpaper.resolve(Screen("bundled:tide"), Person("none")) == "none"


def test_nothing_anywhere_is_nothing():
    assert wallpaper.resolve(Screen(), Person()) == ""


# ----------------------------------------------------------- the fetching --

def test_a_background_is_fetched_once_and_then_revalidated(store, database,
                                                           monkeypatch):
    calls = RecordingOpener({
        "/wallpapers/": FakeResponse(png_bytes(), headers={"ETag": '"v1"'})})
    monkeypatch.setattr("urllib.request.urlopen", calls)

    assert wallpaper.ensure(store, database, "tok", ID) is True
    assert wallpaper.cached_path(database, ID).exists()
    assert store.get_params(wallpaper.ETAG_KEY)[ID] == '"v1"'

    # Second time: the ETag goes out, and a 304 means no bytes come back.
    import urllib.error
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({
        "/wallpapers/": urllib.error.HTTPError(
            "u", 304, "Not Modified", {}, None)}))
    assert wallpaper.ensure(store, database, "tok", ID) is True
    assert calls.requests[0].headers.get("If-none-match") is None


def test_the_etag_goes_back_out_on_the_second_ask(store, database, monkeypatch):
    """Without this a display re-pulls two megabytes on every config push."""
    store.set_params(wallpaper.ETAG_KEY, {ID: '"v1"'})
    wallpaper.cached_path(database, ID).parent.mkdir(parents=True)
    wallpaper.cached_path(database, ID).write_bytes(png_bytes())
    calls = RecordingOpener({"/wallpapers/": FakeResponse(b"", status=200)})
    monkeypatch.setattr("urllib.request.urlopen", calls)
    wallpaper.ensure(store, database, "tok", ID)
    assert calls.requests[0].headers["If-none-match"] == '"v1"'


def test_a_service_that_is_down_is_not_an_exception_on_the_wall(store,
                                                               database,
                                                               monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener(
        {"/wallpapers/": OSError("no route to host")}))
    assert wallpaper.ensure(store, database, "tok", ID) is False
    assert not wallpaper.cached_path(database, ID).exists()


def test_a_display_with_no_token_asks_for_nothing(store, database):
    assert wallpaper.ensure(store, database, "", ID) is False


def test_a_file_already_there_with_no_etag_is_left_alone(store, database):
    """An upload from the settings page. Nothing to revalidate against."""
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(png_bytes())
    assert wallpaper.ensure(store, database, "tok", ID) is True


# ------------------------------------------------------------- the sweep ---

def test_only_the_art_this_display_names_is_wanted():
    screen, people = Screen(ID), [Person("bundled:reeds"), Person(OTHER)]
    assert wallpaper.wanted(screen, people) == {ID, OTHER}


def test_bundled_art_and_none_cost_the_server_nothing():
    assert wallpaper.wanted(Screen("none"), [Person("bundled:tide")]) == set()


def test_pictures_nothing_names_any_more_are_dropped(database):
    directory = wallpaper.cache_dir(database)
    directory.mkdir(parents=True)
    for name in (ID, OTHER):
        (directory / name).write_bytes(b"x")
    (directory / "notes.txt").write_text("left alone")
    assert wallpaper.sweep(database, {ID}) == 1
    assert (directory / ID).exists()
    assert not (directory / OTHER).exists()
    # Only things shaped like a background are its business.
    assert (directory / "notes.txt").exists()


def test_sweeping_a_cache_that_was_never_made_is_fine(database):
    assert wallpaper.sweep(database, set()) == 0


# ------------------------------------------------------------- the drawing --

@pytest.fixture
def surfaces(database):
    pygame.init()
    pygame.display.set_mode((320, 200))
    yield wallpaper.Surfaces(database)
    pygame.quit()


@pytest.mark.parametrize("name", sorted(wallpaper.BUNDLED))
def test_every_bundled_name_draws_something(surfaces, name):
    """The picker offers these, so every one has to resolve to a picture."""
    surface = surfaces.get(f"bundled:{name}", (320, 200))
    assert surface is not None and surface.get_size() == (320, 200)
    # A ramp, not a flat fill: the top and the bottom differ.
    assert surface.get_at((160, 2))[:3] != surface.get_at((160, 197))[:3]


def test_a_bundled_name_this_device_does_not_have_is_a_flat_panel(surfaces):
    assert surfaces.get("bundled:nonesuch", (320, 200)) is None


@pytest.mark.parametrize("name", sorted(wallpaper.PHOTOS))
def test_every_bundled_photo_draws_something(surfaces, name):
    """The shipped photographs answer the same contract as the drawn art."""
    surface = surfaces.get(f"bundled:{name}", (320, 200))
    assert surface is not None and surface.get_size() == (320, 200)


def test_the_photos_are_offered_by_name():
    """Every photo is in the picker's list, under a readable label."""
    names = wallpaper.bundled_names()
    for name in wallpaper.PHOTOS:
        assert name in names
    assert wallpaper.bundled_label("mountain-night") == "Mountain night"
    assert wallpaper.bundled_label("reeds") == "Reeds"


@pytest.mark.parametrize("value", ["", "none", "x" * 32])
def test_nothing_to_draw_draws_nothing(surfaces, value):
    assert surfaces.get(value, (320, 200)) is None


def test_a_picture_is_scaled_to_cover_the_panel(surfaces, database):
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    # Deliberately the wrong shape: covering crops, it never letterboxes.
    path.write_bytes(png_bytes(100, 20))
    surface = surfaces.get(ID, (320, 200))
    assert surface.get_size() == (320, 200)


def test_a_corrupt_file_is_a_flat_panel_rather_than_a_crash(surfaces, database):
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"truncated")
    assert surfaces.get(ID, (320, 200)) is None


def test_a_decoded_picture_is_kept(surfaces, database):
    """The cache the frame rate depends on."""
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(png_bytes())
    first = surfaces.get(ID, (320, 200))
    assert surfaces.get(ID, (320, 200)) is first


def test_a_failure_is_cached_too(surfaces, database):
    """A file that will not decode will not decode next frame either.

    Retrying it every second is how one broken picture becomes a display
    that stutters.
    """
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not an image")
    assert surfaces.get(ID, (320, 200)) is None
    path.write_bytes(png_bytes())
    assert surfaces.get(ID, (320, 200)) is None
    surfaces.clear()
    assert surfaces.get(ID, (320, 200)) is not None


def test_each_size_is_scaled_separately(surfaces, database):
    path = wallpaper.cached_path(database, ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(png_bytes())
    assert surfaces.get(ID, (320, 200)).get_size() == (320, 200)
    assert surfaces.get(ID, (160, 100)).get_size() == (160, 100)
