from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assay.checks import STEP_SECONDS, Context, Profile, container_not_running, dependency_errors
from assay.domain import CheckResult, HypothesisStatus, Incident, Relation, Trigger
from assay.investigate import investigate
from assay.report import Claim, build_report, to_markdown, validate
from assay.sources import LogLine, Sample, Series

START = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=6)


def _grid() -> list[datetime]:
    out, ts = [], START
    while ts <= END:
        out.append(ts)
        ts += timedelta(seconds=STEP_SECONDS)
    return out


class FakeMetrics:
    """Answers queries by the first registered substring they contain."""

    def __init__(self) -> None:
        self.instant: dict[str, float | None] = {}
        self.ranges: dict[str, list[Series]] = {}

    def query(self, expr: str, at: datetime) -> list[Sample]:
        for key, value in self.instant.items():
            if key in expr:
                return [] if value is None else [Sample({}, at, value)]
        return []

    def query_range(self, expr: str, start: datetime, end: datetime, step: float) -> list[Series]:
        for key, value in self.ranges.items():
            if key in expr:
                return value
        return []


class FakeLogs:
    def __init__(self, counts: dict[str, float]) -> None:
        self.counts = counts

    def query(self, expr: str, at: datetime) -> list[Sample]:
        for key, value in self.counts.items():
            if key in expr:
                return [Sample({}, at, value)]
        return []

    def lines(self, expr: str, start: datetime, end: datetime, limit: int) -> list[LogLine]:
        return [LogLine({}, start, '{"msg": "dependency_error"}')][:limit]


def seen(unseen: tuple[datetime, datetime] | None = None) -> list[Series]:
    points = []
    for ts in _grid():
        lag = 100.0 if unseen and unseen[0] <= ts <= unseen[1] else 5.0
        points.append((ts, lag))
    return [Series({}, points)]


def redis_down_lab() -> tuple[FakeMetrics, FakeLogs]:
    m = FakeMetrics()
    m.instant = {
        "count(last_over_time(shop_dependency_errors_total": 1,
        'dependency="redis"': 3000,
        'dependency="postgres"': None,  # series never created: no errors counted
        "min_over_time(up": 1,
        "changes(": 0,
    }
    outage = (START + timedelta(minutes=2), START + timedelta(minutes=5))
    m.ranges = {
        'service="redis"': seen(outage),
        'service="postgres"': seen(),
        'service="app"': seen(),
        "rate(shop_dependency_errors_total": [Series({}, [(outage[0], 10.0)])],
    }
    logs = FakeLogs({'dependency="redis"': 3000, 'dependency="postgres"': 0, "": 5000})
    return m, logs


def incident() -> Incident:
    return Incident("inc-test", Trigger.MANUAL, START, END)


def test_redis_down_ranks_redis_first_and_report_is_valid() -> None:
    metrics, logs = redis_down_lab()
    inv = investigate(incident(), metrics, logs, now=END)

    top = inv.hypotheses[0]
    assert (top.cause_type, top.component, top.status) == (
        "dependency_unavailable",
        "redis",
        HypothesisStatus.SUPPORTED,
    )
    statuses = {(h.cause_type, h.component): h.status for h in inv.hypotheses}
    assert statuses[("dependency_unavailable", "postgres")] is HypothesisStatus.REFUTED
    assert statuses[("app_unavailable", "app")] is HypothesisStatus.REFUTED
    assert statuses[("recent_change", "app")] is HypothesisStatus.OPEN

    report = build_report(inv)
    assert report.valid, report.errors
    assert report.conclusion == "cause"
    assert all(claim.evidence_ids for claim in report.claims)
    assert "change markers" in report.missing[0]
    assert "dependency_unavailable (redis)" in to_markdown(report)


def test_no_data_is_not_evidence() -> None:
    m = FakeMetrics()  # Prometheus knows nothing
    inv = investigate(incident(), m, FakeLogs({}), now=END)
    assert not inv.evidence
    assert all(c.result is CheckResult.NO_DATA for c in inv.checks)
    report = build_report(inv)
    assert report.valid
    assert report.conclusion == "insufficient_data"
    assert not report.claims
    assert any("no data for the redis container" in item for item in report.missing)


def test_validator_rejects_unbacked_and_mismatched_claims() -> None:
    metrics, logs = redis_down_lab()
    report = build_report(investigate(incident(), metrics, logs, now=END))
    contradicting = next(
        e for e in report.investigation.evidence if e.relation is Relation.CONTRADICTS
    )
    report.claims += [
        Claim("X1", "made up", Relation.SUPPORTS, []),
        Claim("X2", "made up", Relation.SUPPORTS, ["E999"]),
        Claim("X3", "twisted", Relation.SUPPORTS, [contradicting.id]),
    ]
    errors = validate(report)
    assert any(e.startswith("X1: no evidence") for e in errors)
    assert any(e.startswith("X2: evidence E999 does not exist") for e in errors)
    assert any(e.startswith("X3:") and "contradicts" in e for e in errors)


def test_container_gap_reports_when() -> None:
    metrics, logs = redis_down_lab()
    ctx = Context(metrics, logs, Profile(), START, END)
    outcome = container_not_running(ctx, "redis")
    assert outcome.result is CheckResult.HELD
    assert outcome.findings[0].payload["unseen_from"] == (START + timedelta(minutes=2)).isoformat()
    assert container_not_running(ctx, "postgres").result is CheckResult.FAILED


def test_missing_metric_is_no_data_not_zero() -> None:
    metrics, logs = redis_down_lab()
    metrics.instant["count(last_over_time(shop_dependency_errors_total"] = None
    ctx = Context(metrics, logs, Profile(), START, END)
    assert dependency_errors(ctx, "postgres").result is CheckResult.NO_DATA
