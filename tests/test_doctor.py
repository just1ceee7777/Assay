from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from assay.cli import main
from assay.doctor import check_ready, check_target_up


class FakeHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        status, body = self.routes.get(path, (404, b"not found"))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[tuple[str, dict[str, tuple[int, bytes]]]]:
    routes: dict[str, tuple[int, bytes]] = {}
    handler = type("Handler", (FakeHandler,), {"routes": routes})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", routes
    server.shutdown()


def _query_result(*values: str) -> bytes:
    result = [{"metric": {"instance": f"app-{i}"}, "value": [0, v]} for i, v in enumerate(values)]
    return json.dumps({"status": "success", "data": {"result": result}}).encode()


def test_ready_ok(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/-/ready"] = (200, b"Ready")
    assert check_ready("prometheus", f"{url}/-/ready").ok


def test_ready_not_200(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/ready"] = (503, b"starting")
    result = check_ready("loki", f"{url}/ready")
    assert not result.ok
    assert result.detail == "HTTP 503"


def test_ready_unreachable() -> None:
    result = check_ready("loki", "http://127.0.0.1:1/ready", timeout=0.5)
    assert not result.ok
    assert "unreachable" in result.detail


def test_target_up(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result("1"))
    assert check_target_up(url, "shop").ok


def test_target_down_is_reported(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result("1", "0"))
    result = check_target_up(url, "shop")
    assert not result.ok
    assert "app-1" in result.detail


def test_no_targets(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result())
    assert not check_target_up(url, "shop").ok


def test_cli_doctor_exit_codes(fake_server: tuple[str, dict[str, tuple[int, bytes]]]) -> None:
    url, routes = fake_server
    routes["/-/ready"] = (200, b"Ready")
    routes["/ready"] = (200, b"ready")
    routes["/api/v1/query"] = (200, _query_result("1"))
    assert main(["doctor", "--prometheus", url, "--loki", url]) == 0

    routes["/ready"] = (503, b"starting")
    assert main(["doctor", "--prometheus", url, "--loki", url]) == 1
