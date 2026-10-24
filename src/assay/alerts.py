"""Find when an alert fired, from Prometheus' own ALERTS series.

Prometheus keeps the state of every alerting rule as a time series, so the firing interval
of an alert can be read back after it has resolved. That makes an investigation
reproducible: the same alert always gives the same window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from assay.sources import Prometheus

# Points further apart than this belong to separate firing episodes.
EPISODE_GAP = timedelta(seconds=60)
MAX_POINTS = 10_000


@dataclass(frozen=True)
class FiringInterval:
    alertname: str
    labels: dict[str, str]
    first_seen: datetime
    last_seen: datetime
    query: str


def alert_query(alertname: str) -> str:
    return f'ALERTS{{alertname="{alertname}", alertstate="firing"}}'


def last_firing(
    prometheus: Prometheus, alertname: str, now: datetime, lookback: timedelta
) -> FiringInterval | None:
    """The most recent firing episode of `alertname` within `lookback` of `now`."""
    start = now - lookback
    step = max(5.0, lookback.total_seconds() / MAX_POINTS)
    query = alert_query(alertname)
    series = prometheus.query_range(query, start, now, step)
    points = sorted((ts, s.labels) for s in series for ts, value in s.points if value == 1)
    if not points:
        return None

    last_ts, labels = points[-1]
    first_ts = last_ts
    for ts, _ in reversed(points[:-1]):
        if first_ts - ts > EPISODE_GAP:
            break
        first_ts = ts
    clean = {k: v for k, v in labels.items() if k not in {"__name__", "alertstate"}}
    return FiringInterval(alertname, clean, first_ts, last_ts, query)
