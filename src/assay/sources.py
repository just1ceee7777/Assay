"""Read-only adapters for the telemetry Assay investigates.

Each adapter returns plain data and never interprets it: deciding what a result means is
the job of the checks. Every method takes the query it runs, so the caller can store the
query next to the observation for reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from assay.http import DEFAULT_TIMEOUT, SourceError, get_json


def _ts(moment: datetime) -> str:
    return f"{moment.timestamp():.3f}"


def from_unix(value: float | str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


@dataclass(frozen=True)
class Sample:
    """One labelled value: a Prometheus vector element or a Loki metric result."""

    labels: dict[str, str]
    ts: datetime
    value: float


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    points: list[tuple[datetime, float]]


@dataclass(frozen=True)
class LogLine:
    labels: dict[str, str]
    ts: datetime
    line: str


def _expect_success(data: Any, what: str) -> Any:
    if not isinstance(data, dict) or data.get("status") != "success":
        raise SourceError(f"{what}: unexpected response")
    return data["data"]


def _vector(data: Any) -> list[Sample]:
    return [
        Sample(dict(r["metric"]), from_unix(r["value"][0]), float(r["value"][1]))
        for r in data["result"]
    ]


class Prometheus:
    name = "prometheus"

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url
        self.timeout = timeout

    def query(self, expr: str, at: datetime) -> list[Sample]:
        data = get_json(self.url, "/api/v1/query", {"query": expr, "time": _ts(at)}, self.timeout)
        return _vector(_expect_success(data, "prometheus query"))

    def query_range(self, expr: str, start: datetime, end: datetime, step: float) -> list[Series]:
        params = {"query": expr, "start": _ts(start), "end": _ts(end), "step": str(step)}
        data = _expect_success(
            get_json(self.url, "/api/v1/query_range", params, self.timeout),
            "prometheus query_range",
        )
        return [
            Series(dict(r["metric"]), [(from_unix(t), float(v)) for t, v in r["values"]])
            for r in data["result"]
        ]


class Loki:
    name = "loki"

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url
        self.timeout = timeout

    def query(self, expr: str, at: datetime) -> list[Sample]:
        """Instant metric query, e.g. sum(count_over_time({...}[5m]))."""
        data = _expect_success(
            get_json(
                self.url, "/loki/api/v1/query", {"query": expr, "time": _ts(at)}, self.timeout
            ),
            "loki query",
        )
        return _vector(data)

    def lines(self, expr: str, start: datetime, end: datetime, limit: int) -> list[LogLine]:
        params = {
            "query": expr,
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": str(limit),
            "direction": "forward",
        }
        data = _expect_success(
            get_json(self.url, "/loki/api/v1/query_range", params, self.timeout),
            "loki query_range",
        )
        result = [
            LogLine(dict(stream["stream"]), from_unix(int(ns) / 1e9), line)
            for stream in data["result"]
            for ns, line in stream["values"]
        ]
        return sorted(result, key=lambda entry: entry.ts)[:limit]
