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
# Longer than any sensible `for:` duration of an alerting rule.
PENDING_LOOKBACK = timedelta(minutes=30)


@dataclass(frozen=True)
class FiringInterval:
    alertname: str
    labels: dict[str, str]
    first_seen: datetime
    last_seen: datetime
    query: str
    # When the rule's condition first held, before its `for:` delay made it fire.
    # This is the onset of the symptoms as the alert defines them.
    pending_since: datetime | None = None

    @property
    def symptoms_since(self) -> datetime:
        return self.pending_since or self.first_seen


def alert_query(alertname: str, state: str = "firing") -> str:
    return f'ALERTS{{alertname="{alertname}", alertstate="{state}"}}'


def _episode_start(points: list[datetime], end: datetime) -> datetime | None:
    """Start of the run of points that ends at or just before `end`."""
    before = sorted(ts for ts in points if ts <= end)
    if not before or end - before[-1] > EPISODE_GAP:
        return None
    first = before[-1]
    for ts in reversed(before[:-1]):
        if first - ts > EPISODE_GAP:
            break
        first = ts
    return first


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
    first_ts = _episode_start([ts for ts, _ in points], last_ts) or last_ts
    clean = {k: v for k, v in labels.items() if k not in {"__name__", "alertstate"}}

    pending = prometheus.query_range(
        alert_query(alertname, "pending"), first_ts - PENDING_LOOKBACK, first_ts, step
    )
    pending_points = [ts for s in pending for ts, value in s.points if value == 1]
    pending_since = _episode_start(pending_points, first_ts)
    return FiringInterval(alertname, clean, first_ts, last_ts, query, pending_since)
