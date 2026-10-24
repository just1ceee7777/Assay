from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Routes

from assay.alerts import last_firing
from assay.http import SourceError
from assay.sources import Loki, Prometheus

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _ok(data: object) -> bytes:
    return json.dumps({"status": "success", "data": data}).encode()


def _matrix(*series: tuple[dict[str, str], list[tuple[datetime, str]]]) -> bytes:
    result = [
        {"metric": labels, "values": [[ts.timestamp(), v] for ts, v in points]}
        for labels, points in series
    ]
    return _ok({"resultType": "matrix", "result": result})


def test_prometheus_vector(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (
        200,
        _ok({"result": [{"metric": {"job": "shop"}, "value": [T0.timestamp(), "1"]}]}),
    )
    [sample] = Prometheus(url).query('up{job="shop"}', T0)
    assert sample.labels == {"job": "shop"}
    assert sample.ts == T0
    assert sample.value == 1.0


def test_prometheus_error_is_source_error(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query"] = (400, b'{"status":"error","error":"parse error"}')
    with pytest.raises(SourceError, match="HTTP 400"):
        Prometheus(url).query("up{", T0)


def test_unreachable_is_source_error() -> None:
    with pytest.raises(SourceError, match="unreachable"):
        Prometheus("http://127.0.0.1:1", timeout=0.5).query("up", T0)


def test_loki_lines_sorted_and_limited(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    ns = int(T0.timestamp() * 1e9)
    streams = [
        {"stream": {"service": "app"}, "values": [[str(ns + 2_000_000_000), "late"]]},
        {"stream": {"service": "app"}, "values": [[str(ns), "early"], [str(ns + 1), "mid"]]},
    ]
    routes["/loki/api/v1/query_range"] = (200, _ok({"result": streams}))
    lines = Loki(url).lines('{service="app"}', T0, T0 + timedelta(minutes=1), limit=2)
    assert [entry.line for entry in lines] == ["early", "mid"]


def test_last_firing_picks_latest_episode(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    labels = {"__name__": "ALERTS", "alertname": "X", "alertstate": "firing", "service": "shop"}
    old = [(T0 + timedelta(seconds=s), "1") for s in range(0, 60, 5)]
    new = [(T0 + timedelta(minutes=10, seconds=s), "1") for s in range(0, 120, 5)]
    routes["/api/v1/query_range"] = (200, _matrix((labels, old + new)))

    interval = last_firing(Prometheus(url), "X", T0 + timedelta(minutes=15), timedelta(hours=1))
    assert interval is not None
    assert interval.first_seen == T0 + timedelta(minutes=10)
    assert interval.last_seen == T0 + timedelta(minutes=11, seconds=55)
    assert interval.labels == {"alertname": "X", "service": "shop"}


def test_last_firing_none(fake_server: tuple[str, Routes]) -> None:
    url, routes = fake_server
    routes["/api/v1/query_range"] = (200, _matrix())
    assert last_firing(Prometheus(url), "X", T0, timedelta(hours=1)) is None
