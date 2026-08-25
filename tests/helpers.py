"""Helpers shared by the tests that talk to a real socket.

Kept out of conftest.py so test modules can import them by name.
"""

import http.client
import json
import socket


def free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    """Tiny HTTP client for the servers the app starts."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = port
        self.host = host

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            payload = body
            if isinstance(body, (dict, list)):
                payload = json.dumps(body).encode()
                headers = {"Content-Type": "application/json", **(headers or {})}
            conn.request(method, path, payload, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body, **kwargs)

    def json(self, path, **kwargs):
        status, _headers, body = self.get(path, **kwargs)
        return status, json.loads(body)


class FakeResponse:
    """Enough of an ``http.client.HTTPResponse`` for ``urlopen`` callers."""

    def __init__(self, payload, headers=None, status=200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self._payload = payload
        self.headers = headers or {}
        self.status = status

    def read(self, size=-1):
        payload, self._payload = self._payload, b""
        return payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """A stand-in for ``urllib.request.urlopen``.

    ``routes`` maps a substring of the URL to a payload, an exception to
    raise, or a callable taking the request. Every call is recorded so a
    test can assert on the headers and query string that went out.
    """

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout=None, **kwargs):
        url = getattr(request, "full_url", request)
        self.requests.append(request)
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(request)
                return answer if isinstance(answer, FakeResponse) \
                    else FakeResponse(answer)
        raise AssertionError(f"no stubbed answer for {url}")

    @property
    def urls(self):
        return [getattr(r, "full_url", r) for r in self.requests]
