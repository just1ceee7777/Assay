from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# path -> (status, body); the query string is ignored when matching.
Routes = dict[str, tuple[int, bytes]]


class FakeHandler(BaseHTTPRequestHandler):
    routes: Routes = {}
    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        self.seen.append(self.path)
        path = self.path.split("?", 1)[0]
        status, body = self.routes.get(path, (404, b"not found"))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[tuple[str, Routes]]:
    routes: Routes = {}
    handler = type("Handler", (FakeHandler,), {"routes": routes, "seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", routes
    server.shutdown()
