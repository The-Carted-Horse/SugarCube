"""multipart.py — accepting one picture, and nothing more than that.

The settings site has never taken a file, and the parser is hand-written
because `cgi` is gone in 3.13. That makes it the newest and least
battle-tested code path on the device, and the one an attacker reaches
first, so these lean on the edges rather than the happy path.
"""

import io

import pytest

from glucocube import multipart

BOUNDARY = b"----GlucoCubeTest"


def part(name, value, filename=None, content_type=None):
    head = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        head += f'; filename="{filename}"'
    head = head.encode()
    if content_type:
        head += b"\r\nContent-Type: " + content_type.encode()
    if isinstance(value, str):
        value = value.encode()
    return b"--" + BOUNDARY + b"\r\n" + head + b"\r\n\r\n" + value + b"\r\n"


def body(*parts):
    return b"".join(parts) + b"--" + BOUNDARY + b"--\r\n"


PNG = multipart.PNG_MAGIC + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
JPEG = multipart.JPEG_MAGIC + b"\xe0\x00\x10JFIF" + b"\x00" * 40


# ------------------------------------------------------------- boundary ----

@pytest.mark.parametrize("header, expected", [
    ('multipart/form-data; boundary=----X', b"----X"),
    ('multipart/form-data; boundary="----X"', b"----X"),
    ('MULTIPART/FORM-DATA; BOUNDARY=----X', b"----X"),
    ('multipart/form-data; charset=utf-8; boundary=abc', b"abc"),
])
def test_the_delimiter_comes_out_of_the_content_type(header, expected):
    assert multipart.boundary_of(header) == expected


@pytest.mark.parametrize("header", [
    "", None, "application/x-www-form-urlencoded",
    "multipart/form-data",              # no boundary at all
])
def test_anything_that_is_not_a_multipart_post_has_no_delimiter(header):
    assert multipart.boundary_of(header) is None


# ---------------------------------------------------------------- parse ----

def test_a_form_with_a_file_and_a_field_comes_apart():
    fields = multipart.parse(
        body(part("name", "Reeds"),
             part("image", PNG, filename="reeds.png", content_type="image/png")),
        BOUNDARY)
    assert fields["name"] == "Reeds"
    assert fields["image"] == ("reeds.png", PNG)


def test_bytes_survive_the_trip_exactly():
    """A JPEG is not text. Every byte that went in comes out."""
    awkward = bytes(range(256)) * 4
    fields = multipart.parse(
        body(part("image", awkward, filename="x.jpg")), BOUNDARY)
    assert fields["image"][1] == awkward


def test_the_delimiters_own_line_break_is_not_part_of_the_file():
    """Off-by-two here writes a corrupt image nobody can decode."""
    fields = multipart.parse(
        body(part("image", PNG, filename="x.png")), BOUNDARY)
    assert not fields["image"][1].endswith(b"\r\n")
    assert len(fields["image"][1]) == len(PNG)


def test_an_empty_file_input_is_a_part_with_no_bytes():
    """A form submitted with nothing chosen still sends the part."""
    fields = multipart.parse(
        body(part("image", b"", filename="")), BOUNDARY)
    assert fields["image"] == ("", b"")


def test_a_part_it_cannot_read_loses_only_itself():
    broken = b"--" + BOUNDARY + b"\r\nnonsense, no blank line"
    fields = multipart.parse(
        body(part("name", "Reeds")) + broken, BOUNDARY)
    assert fields["name"] == "Reeds"


def test_a_body_that_is_not_multipart_at_all_yields_nothing():
    assert multipart.parse(b"name=Reeds&image=x", BOUNDARY) == {}
    assert multipart.parse(b"", BOUNDARY) == {}


# --------------------------------------------------------------- sniffing --

def test_the_bytes_say_what_a_file_is_not_its_name():
    # A browser will call a PNG image/jpeg because somebody renamed it.
    assert multipart.looks_like_image(PNG) == "png"
    assert multipart.looks_like_image(JPEG) == "jpeg"


@pytest.mark.parametrize("data", [
    b"", b"GIF89a", b"#!/bin/sh\nrm -rf /\n", b"\x89PNG", b"\xff\xd8",
    b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
])
def test_nothing_else_is_an_image(data):
    assert multipart.looks_like_image(data) == ""


# ------------------------------------------------------------- read_body ----

class Handler:
    """Enough of a BaseHTTPRequestHandler for read_body."""

    def __init__(self, payload, length=None):
        self.rfile = io.BytesIO(payload)
        self.headers = {"Content-Length": str(
            len(payload) if length is None else length)}


def test_a_body_within_the_cap_is_returned_whole():
    assert multipart.read_body(Handler(b"x" * 500), 1000) == b"x" * 500


def test_no_body_is_no_bytes():
    assert multipart.read_body(Handler(b""), 1000) == b""


def test_a_body_over_the_cap_is_refused():
    with pytest.raises(multipart.TooLarge):
        multipart.read_body(Handler(b"x" * 5000), 1000)


def test_an_oversized_body_is_still_drained_off_the_socket():
    """Keep-alive is the reason.

    This server speaks HTTP/1.1, so a body left half-read is not one
    request refused — it is the next request on that connection reading
    the tail of this one as its own request line.
    """
    handler = Handler(b"x" * 5000)
    with pytest.raises(multipart.TooLarge):
        multipart.read_body(handler, 1000)
    assert handler.rfile.read() == b""


def test_a_truncated_body_does_not_hang():
    """Content-Length lies; the read still ends when the socket does."""
    handler = Handler(b"x" * 10, length=9999)
    with pytest.raises(multipart.TooLarge):
        multipart.read_body(handler, 1000)
