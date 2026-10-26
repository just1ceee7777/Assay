"""Catalog of checks. A check is a prediction of a hypothesis plus the query that tests it.

Rules every check follows:
- it only reads: one or a few queries against Prometheus or Loki;
- it returns HELD or FAILED only with findings that show why, so both supporting and
  contradicting evidence point at data;
- when the data it needs is absent it returns NO_DATA and says what is missing, instead
  of treating "no data" as "no problem".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from assay.domain import CheckResult, LinkType
from assay.sources import LogLine, Sample, Series

STEP_SECONDS = 15.0
# cAdvisor housekeeping plus scrape interval: a container unseen for longer is not running.
UNSEEN_AFTER_SECONDS = 30.0
SAMPLE_LINES = 3


class MetricSource(Protocol):
    def query(self, expr: str, at: datetime) -> list[Sample]: ...
    def query_range(
        self, expr: str, start: datetime, end: datetime, step: float
    ) -> list[Series]: ...


class LogSource(Protocol):
    def query(self, expr: str, at: datetime) -> list[Sample]: ...
    def lines(self, expr: str, start: datetime, end: datetime, limit: int) -> list[LogLine]: ...


@dataclass(frozen=True)
class Profile:
    """What Assay knows about the observed system. Defaults describe the lab."""

    app: str = "app"
    job: str = "shop"
    dependencies: tuple[str, ...] = ("redis", "postgres")
    dependency_errors_metric: str = "shop_dependency_errors_total"
    dependency_label: str = "dependency"
    app_logs: str = '{service="app"}'
    container_label: str = "container_label_com_docker_compose_service"
    # A gauge labelled with the running version, e.g. shop_build_info{version="1.2.3"} 1.
    build_info_metric: str = "shop_build_info"
    version_label: str = "version"


# A cause may show up a little before the symptoms do (detection and rate() windows lag).
SYMPTOM_LEAD = timedelta(seconds=60)


def _seconds(start: datetime, end: datetime) -> str:
    return f"{max(1, int((end - start).total_seconds()))}s"


@dataclass(frozen=True)
class Context:
    """The incident window, plus the period when the symptoms were present.

    Checks that ask "was something broken?" look at the focus period: the symptoms plus a
    short lead. Something that broke and recovered well before the symptoms is context,
    not a cause. Without a known symptom period, the focus is the whole window.
    """

    metrics: MetricSource
    logs: LogSource
    profile: Profile
    start: datetime
    end: datetime
    symptoms_start: datetime | None = None
    symptoms_end: datetime | None = None

    @property
    def range(self) -> str:
        return _seconds(self.start, self.end)

    @property
    def focus_start(self) -> datetime:
        if self.symptoms_start is None:
            return self.start
        return max(self.start, self.symptoms_start - SYMPTOM_LEAD)

    @property
    def focus_end(self) -> datetime:
        return min(self.end, self.symptoms_end) if self.symptoms_end else self.end

    @property
    def focus_range(self) -> str:
        return _seconds(self.focus_start, self.focus_end)

    @property
    def focus_name(self) -> str:
        """How report text names the focus period, so it never claims more than was checked."""
        return "around the symptoms" if self.symptoms_start else "in the window"


@dataclass
class Finding:
    """An observation before it gets an id."""

    source: str
    component: str
    ts: datetime
    kind: str
    payload: dict[str, Any]
    query: str


@dataclass
class Outcome:
    result: CheckResult
    detail: str
    query: str
    findings: list[Finding] = field(default_factory=list)


@dataclass(frozen=True)
class CheckSpec:
    name: str
    description: str
    expected_if_true: str
    link_type: LinkType
    strength: float
    run: Callable[[Context, str], Outcome]


def _value(samples: list[Sample]) -> float | None:
    return samples[0].value if samples else None


def _fmt(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S")


# --- dependency checks ---------------------------------------------------------------


def dependency_errors(ctx: Context, dep: str) -> Outcome:
    p = ctx.profile
    selector = f'{p.dependency_errors_metric}{{{p.dependency_label}="{dep}"}}'
    query = f"sum(increase({selector}[{ctx.focus_range}]))"
    if not ctx.metrics.query(
        f"count(last_over_time({p.dependency_errors_metric}[{ctx.focus_range}]))", ctx.focus_end
    ):
        return Outcome(
            CheckResult.NO_DATA,
            f"the application exports no {p.dependency_errors_metric} metric",
            query,
        )
    # prometheus_client creates a labelled series on first use, so a missing series
    # for this dependency means no errors were counted.
    total = _value(ctx.metrics.query(query, ctx.focus_end)) or 0.0
    payload: dict[str, Any] = {"increase": round(total, 1)}
    onset_query = f"sum(rate({selector}[30s])) > 0"
    if total >= 1:
        series = ctx.metrics.query_range(onset_query, ctx.focus_start, ctx.focus_end, STEP_SECONDS)
        points = sorted(ts for s in series for ts, _ in s.points)
        if points:
            payload["first_seen"] = points[0].isoformat()
            payload["last_seen"] = points[-1].isoformat()
    finding = Finding("prometheus", dep, ctx.focus_end, "dependency_errors", payload, query)
    if total >= 1:
        when = (
            f", from {_fmt(datetime.fromisoformat(payload['first_seen']))}"
            if "first_seen" in payload
            else ""
        )
        return Outcome(
            CheckResult.HELD,
            f"the application counted {total:.0f} errors talking to {dep}{when}",
            query,
            [finding],
        )
    return Outcome(
        CheckResult.FAILED, f"the application counted no errors talking to {dep}", query, [finding]
    )


def dependency_error_logs(ctx: Context, dep: str) -> Outcome:
    p = ctx.profile
    selector = f'{p.app_logs} | json | msg="dependency_error" | {p.dependency_label}="{dep}"'
    query = f"sum(count_over_time({selector} [{ctx.focus_range}]))"
    if not ctx.logs.query(f"sum(count_over_time({p.app_logs} [{ctx.focus_range}]))", ctx.focus_end):
        return Outcome(
            CheckResult.NO_DATA, f"there are no application logs {ctx.focus_name}", query
        )
    count = _value(ctx.logs.query(query, ctx.focus_end)) or 0.0
    findings = [
        Finding("loki", dep, ctx.focus_end, "log_count", {"count": int(count)}, query),
    ]
    if count < 1:
        return Outcome(
            CheckResult.FAILED,
            f"application logs have no dependency errors for {dep}",
            query,
            findings,
        )
    for entry in ctx.logs.lines(selector, ctx.focus_start, ctx.focus_end, SAMPLE_LINES):
        findings.append(Finding("loki", dep, entry.ts, "log_line", {"line": entry.line}, selector))
    return Outcome(
        CheckResult.HELD,
        f"application logs have {int(count)} dependency error lines for {dep}",
        query,
        findings,
    )


def container_not_running(ctx: Context, service: str) -> Outcome:
    """cAdvisor stops refreshing container_last_seen when a container is not running."""
    label = ctx.profile.container_label
    query = f'time() - max(container_last_seen{{{label}="{service}"}})'
    series = ctx.metrics.query_range(query, ctx.focus_start, ctx.focus_end, STEP_SECONDS)
    if not series:
        return Outcome(
            CheckResult.NO_DATA, f"cAdvisor has no data for the {service} container", query
        )
    points = dict(series[0].points)
    grid = _grid(ctx.focus_start, ctx.focus_end)
    # A missing point means the series went away, which also means "not seen".
    unseen = [ts for ts in grid if points.get(ts, UNSEEN_AFTER_SECONDS + 1) > UNSEEN_AFTER_SECONDS]
    if not unseen:
        seen = {"max_unseen_seconds": round(max(points.values()), 1)}
        finding = Finding("prometheus", service, ctx.focus_end, "container_seen", seen, query)
        return Outcome(
            CheckResult.FAILED,
            f"the {service} container was running {ctx.focus_name}",
            query,
            [finding],
        )
    payload: dict[str, Any] = {
        "unseen_from": unseen[0].isoformat(),
        "unseen_to": unseen[-1].isoformat(),
        "unseen_points": len(unseen),
        "step_seconds": STEP_SECONDS,
    }
    finding = Finding("prometheus", service, unseen[0], "container_unseen", payload, query)
    return Outcome(
        CheckResult.HELD,
        f"the {service} container was not running from {_fmt(unseen[0])} to {_fmt(unseen[-1])}",
        query,
        [finding],
    )


def _grid(start: datetime, end: datetime) -> list[datetime]:
    """Timestamps a range query is evaluated at: start + k * step (start in whole seconds)."""
    grid: list[datetime] = []
    ts = start
    while ts <= end:
        grid.append(ts)
        ts += timedelta(seconds=STEP_SECONDS)
    return grid


# --- application checks --------------------------------------------------------------


def target_down(ctx: Context, app: str) -> Outcome:
    query = f'min(min_over_time(up{{job="{ctx.profile.job}"}}[{ctx.focus_range}]))'
    value = _value(ctx.metrics.query(query, ctx.focus_end))
    if value is None:
        return Outcome(
            CheckResult.NO_DATA, f"Prometheus has no scrape data for job {ctx.profile.job}", query
        )
    finding = Finding("prometheus", app, ctx.focus_end, "scrape_up_min", {"min_up": value}, query)
    if value < 1:
        return Outcome(
            CheckResult.HELD,
            f"Prometheus failed to scrape {app} {ctx.focus_name}",
            query,
            [finding],
        )
    return Outcome(
        CheckResult.FAILED,
        f"Prometheus scraped {app} successfully {ctx.focus_name}",
        query,
        [finding],
    )


def container_restarted(ctx: Context, app: str) -> Outcome:
    label = ctx.profile.container_label
    query = (
        f'changes(max(container_start_time_seconds{{{label}="{app}"}})'
        f"[{ctx.focus_range}:{int(STEP_SECONDS)}s])"
    )
    value = _value(ctx.metrics.query(query, ctx.focus_end))
    if value is None:
        return Outcome(
            CheckResult.NO_DATA, f"cAdvisor has no start time for the {app} container", query
        )
    finding = Finding(
        "prometheus", app, ctx.focus_end, "container_starts", {"changes": value}, query
    )
    if value >= 1:
        return Outcome(
            CheckResult.HELD, f"the {app} container restarted {value:.0f} time(s)", query, [finding]
        )
    return Outcome(
        CheckResult.FAILED,
        f"the {app} container did not restart {ctx.focus_name}",
        query,
        [finding],
    )


# How far before the window to look, to know which version was already running.
RELEASE_LOOKBEHIND = timedelta(minutes=10)
# A defect shipped in a release shows up as soon as the new version serves traffic.
# A version that ran cleanly for longer than this before the symptoms is unlikely to be
# the cause. Slow-acting defects (a leak, a full disk) are the known blind spot.
RELEASE_EFFECT_WITHIN = timedelta(seconds=60)


@dataclass(frozen=True)
class _Releases:
    query: str
    exported: bool
    first_seen: dict[str, datetime]

    def latest_between(self, start: datetime, end: datetime) -> tuple[datetime, str] | None:
        started = sorted((ts, v) for v, ts in self.first_seen.items() if start <= ts <= end)
        return started[-1] if started else None


def _releases(ctx: Context) -> _Releases:
    p = ctx.profile
    query = p.build_info_metric
    series = ctx.metrics.query_range(query, ctx.start - RELEASE_LOOKBEHIND, ctx.end, STEP_SECONDS)
    first_seen = {
        s.labels.get(p.version_label, "?"): min(ts for ts, _ in s.points)
        for s in series
        if s.points
    }
    return _Releases(query, bool(series), first_seen)


def _release_cutoff(ctx: Context) -> datetime:
    """A release after the symptoms began cannot have caused them."""
    return ctx.symptoms_start or ctx.end


def change_markers(ctx: Context, app: str) -> Outcome:
    """A release shows up as a build_info series with a version label not seen before."""
    releases = _releases(ctx)
    query = releases.query
    if not releases.exported:
        return Outcome(
            CheckResult.NO_DATA,
            f"the application exports no {ctx.profile.build_info_metric} metric, "
            "so releases are invisible",
            query,
        )
    latest = releases.latest_between(ctx.start, _release_cutoff(ctx))
    if latest is None:
        running = ", ".join(sorted(releases.first_seen))
        versions = {"versions": sorted(releases.first_seen)}
        finding = Finding("prometheus", app, ctx.end, "no_release", versions, query)
        return Outcome(
            CheckResult.FAILED,
            f"no new version of {app} was released before the symptoms (running: {running})",
            query,
            [finding],
        )
    ts, version = latest
    previous = sorted(v for v, seen in releases.first_seen.items() if seen < ts and v != version)
    payload = {"version": version, "started": ts.isoformat(), "previous": previous}
    finding = Finding("prometheus", app, ts, "release", payload, query)
    return Outcome(
        CheckResult.HELD,
        f"version {version} of {app} was released at {_fmt(ts)}",
        query,
        [finding],
    )


def release_effect_immediate(ctx: Context, app: str) -> Outcome:
    """If the release broke things, the symptoms start right after it, not minutes later."""
    releases = _releases(ctx)
    query = releases.query
    if ctx.symptoms_start is None:
        return Outcome(
            CheckResult.NO_DATA, "the start of the symptoms is unknown (no alert)", query
        )
    latest = releases.latest_between(ctx.start, ctx.symptoms_start)
    if latest is None:
        return Outcome(
            CheckResult.NO_DATA, f"no release of {app} before the symptoms to time", query
        )
    ts, version = latest
    gap = ctx.symptoms_start - ts
    payload = {
        "version": version,
        "released": ts.isoformat(),
        "symptoms_start": ctx.symptoms_start.isoformat(),
        "gap_seconds": int(gap.total_seconds()),
    }
    finding = Finding("prometheus", app, ctx.symptoms_start, "release_gap", payload, query)
    seconds = int(gap.total_seconds())
    if gap <= RELEASE_EFFECT_WITHIN:
        return Outcome(
            CheckResult.HELD,
            f"the symptoms began {seconds} s after version {version} of {app} was released",
            query,
            [finding],
        )
    return Outcome(
        CheckResult.FAILED,
        f"version {version} of {app} ran for {seconds} s without symptoms before they began",
        query,
        [finding],
    )


# --- catalog -------------------------------------------------------------------------

DEPENDENCY_ERRORS = CheckSpec(
    "dependency_errors",
    "The application counts errors talking to the dependency",
    "errors to the dependency rise in the incident window",
    LinkType.CORRELATION,
    1.0,
    dependency_errors,
)
DEPENDENCY_ERROR_LOGS = CheckSpec(
    "dependency_error_logs",
    "Application logs report errors talking to the dependency",
    "dependency error lines appear in the incident window",
    # Same code path as the metric above, so it adds less independent weight.
    LinkType.CORRELATION,
    0.5,
    dependency_error_logs,
)
CONTAINER_NOT_RUNNING = CheckSpec(
    "container_not_running",
    "The container is not running",
    "cAdvisor stops seeing the container during the incident window",
    LinkType.TEMPORAL,
    1.0,
    container_not_running,
)
TARGET_DOWN = CheckSpec(
    "target_down",
    "Prometheus cannot scrape the application",
    "the scrape target is down around the symptoms",
    LinkType.TEMPORAL,
    1.0,
    target_down,
)
CONTAINER_RESTARTED = CheckSpec(
    "container_restarted",
    "The application container restarted",
    "the container start time changes around the symptoms",
    LinkType.TEMPORAL,
    0.5,
    container_restarted,
)
CHANGE_MARKERS = CheckSpec(
    "change_markers",
    "A deploy or configuration change happened shortly before the incident",
    "a change marker for the component precedes the symptoms",
    LinkType.TEMPORAL,
    # That a release happened is weak evidence on its own: releases are frequent.
    0.5,
    change_markers,
)
RELEASE_EFFECT_IMMEDIATE = CheckSpec(
    "release_effect_immediate",
    "The symptoms start right after the release",
    f"the symptoms begin within {int(RELEASE_EFFECT_WITHIN.total_seconds())} s of the release",
    LinkType.TEMPORAL,
    0.5,
    release_effect_immediate,
)


@dataclass(frozen=True)
class Cause:
    cause_type: str
    checks: tuple[CheckSpec, ...]


DEPENDENCY_UNAVAILABLE = Cause(
    "dependency_unavailable", (CONTAINER_NOT_RUNNING, DEPENDENCY_ERRORS, DEPENDENCY_ERROR_LOGS)
)
APP_UNAVAILABLE = Cause(
    "app_unavailable", (TARGET_DOWN, CONTAINER_NOT_RUNNING, CONTAINER_RESTARTED)
)
RECENT_CHANGE = Cause("recent_change", (CHANGE_MARKERS, RELEASE_EFFECT_IMMEDIATE))


def candidates(profile: Profile) -> list[tuple[Cause, str]]:
    """Every (cause, component) pair worth checking. The order is only for stable ids."""
    pairs: list[tuple[Cause, str]] = [(DEPENDENCY_UNAVAILABLE, dep) for dep in profile.dependencies]
    pairs.append((APP_UNAVAILABLE, profile.app))
    pairs.append((RECENT_CHANGE, profile.app))
    return pairs
