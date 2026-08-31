"""Just enough multipart to accept one picture.

The settings site has never taken a file, and `cgi.FieldStorage` — the
obvious answer — was removed in Python 3.13 (PEP 594). The device is on
3.11 today and a Trixie image is 3.13, so writing against `cgi` would be
writing something that stops working on the next image.

Adding a dependency for this is worse: the whole application runs on
pygame and qrcode, both from apt, and `install.sh` copies a directory onto
the Pi. So: a small reader, for exactly the shape a browser sends when a
form with one file input is posted.

Deliberately not general. No nested multipart, no base64 transfer
encoding, no continuation headers — none of which a browser emits for a
form post, and each of which is a way to be wrong about something nobody
is sending.
"""

import re

# The first bytes of the two formats a display can actually draw. The
# declared content type is not consulted: a browser will happily call a
# PNG image/jpeg because somebody renamed the file, and an attacker will
# call anything anything.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

_DISPOSITION = re.compile(rb'name="([^"]*)"')
_FILENAME = re.compile(rb'filename="([^"]*)"')


class TooLarge(ValueError):
    """The body is bigger than this device is willing to hold in memory."""


def boundary_of(content_type: str) -> bytes | None:
    """The delimiter out of a Content-Type header, or None if it is not one."""
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "boundary" and value:
            return value.strip('"').encode()
    return None


def parse(body: bytes, boundary: bytes) -> dict:
    """Fields by name. A file part is (filename, bytes); a plain one is str.

    Tolerant in one direction only: a part it cannot make sense of is
    skipped rather than raising, because one malformed part should not
    lose the rest of somebody's form. Anything it does return, it
    understood.
    """
    fields: dict = {}
    marker = b"--" + boundary
    for chunk in body.split(marker):
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        # The trailing CRLF belongs to the delimiter, not to the value.
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        disposition = b""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                disposition = line
                break
        name_match = _DISPOSITION.search(disposition)
        if not name_match:
            continue
        name = name_match.group(1).decode("utf-8", "replace")
        filename = _FILENAME.search(disposition)
        if filename:
            fields[name] = (filename.group(1).decode("utf-8", "replace"),
                            payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields


def read_body(handler, limit: int) -> bytes:
    """Read the request body, refusing one too big to hold.

    The socket is drained either way. This server speaks HTTP/1.1 with
    keep-alive, so a body left half-read is not one request refused — it
    is every subsequent request on that connection reading somebody
    else's bytes as its own request line.
    """
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    remaining = length
    chunks = []
    kept = 0
    while remaining > 0:
        block = handler.rfile.read(min(65536, remaining))
        if not block:
            break
        remaining -= len(block)
        if kept <= limit:
            chunks.append(block)
            kept += len(block)
    if length > limit:
        raise TooLarge(f"that file is larger than {limit // (1024 * 1024)} MB")
    return b"".join(chunks)


def looks_like_image(data: bytes) -> str:
    """"png", "jpeg", or "" — read out of the bytes, never off the label."""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpeg"
    return ""
