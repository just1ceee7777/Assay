from __future__ import annotations

import json

from conftest import Routes

from assay.cli import main
from assay.doctor import check_ready, check_target_up


def _query_result(*values: str) -> bytes:
    result = [{"metric": {"instance": f"app-{i}"}, "value": [0, v]} for i, v in enumerate(values)]
    return json.dumps({"status": "success", "data": {"result": result}}).encode()


def test_ready_ok(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/-/ready"] = (200, b"Ready")
    assert check_ready("prometheus", f"{url}/-/ready").ok


def test_ready_not_200(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/ready"] = (503, b"Ingester not ready: waiting for 15s after being ready\n")
    result = check_ready("loki", f"{url}/ready")
    assert not result.ok
    assert result.detail == "HTTP 503: Ingester not ready: waiting for 15s after being ready"


def test_ready_not_200_empty_body(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/ready"] = (503, b"")
    assert check_ready("loki", f"{url}/ready").detail == "HTTP 503"


def test_ready_unreachable() -> None:
    result = check_ready("loki", "http://127.0.0.1:1/ready", timeout=0.5)
    assert not result.ok
    assert "unreachable" in result.detail


def test_target_up(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result("1"))
    assert check_target_up(url, "shop").ok


def test_target_down_is_reported(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result("1", "0"))
    result = check_target_up(url, "shop")
    assert not result.ok
    assert "app-1" in result.detail


def test_no_targets(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (200, _query_result())
    assert not check_target_up(url, "shop").ok


def test_cli_doctor_exit_codes(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/-/ready"] = (200, b"Ready")
    routes["/ready"] = (200, b"ready")
    routes["/api/v1/query"] = (200, _query_result("1"))
    assert main(["doctor", "--prometheus", url, "--loki", url]) == 0

    routes["/ready"] = (503, b"starting")
    assert main(["doctor", "--prometheus", url, "--loki", url]) == 1
