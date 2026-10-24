"""Run the catalog of checks over an incident window and rank hypotheses by their results.

Scoring is deliberately simple and explainable: a check that held adds its strength, a
check that failed subtracts it, a check without data changes nothing. A hypothesis is
never ranked by how plausible it sounds, only by what the checks found.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from assay.checks import Context, LogSource, MetricSource, Outcome, Profile, candidates
from assay.domain import (
    Check,
    CheckResult,
    Evidence,
    Hypothesis,
    HypothesisStatus,
    Incident,
    Observation,
    Relation,
)
from assay.http import SourceError


@dataclass
class Investigation:
    incident: Incident
    hypotheses: list[Hypothesis] = field(default_factory=list)  # ranked, best first
    checks: list[Check] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)


def whole_seconds(moment: datetime) -> datetime:
    return moment.replace(microsecond=0)


def investigate(
    incident: Incident,
    metrics: MetricSource,
    logs: LogSource,
    profile: Profile | None = None,
    now: datetime | None = None,
) -> Investigation:
    profile = profile or Profile()
    ctx = Context(
        metrics,
        logs,
        profile,
        whole_seconds(incident.window_start),
        whole_seconds(incident.window_end),
    )
    result = Investigation(incident)
    counters = {"H": 0, "C": 0, "E": 0, "O": 0}

    def next_id(prefix: str) -> str:
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]}"

    for cause, component in candidates(profile):
        hypothesis = Hypothesis(next_id("H"), cause.cause_type, component)
        result.hypotheses.append(hypothesis)
        for spec in cause.checks:
            outcome = _run(spec.run, ctx, component)
            check = Check(
                next_id("C"),
                hypothesis.id,
                spec.name,
                spec.description,
                outcome.query,
                spec.expected_if_true,
                outcome.result,
                outcome.detail,
                now or datetime.now(tz=UTC),
            )
            result.checks.append(check)
            if outcome.result is CheckResult.NO_DATA:
                continue
            observation_ids = []
            for finding in outcome.findings:
                observation = Observation(
                    next_id("O"),
                    finding.source,
                    finding.component,
                    finding.ts,
                    finding.kind,
                    finding.payload,
                    finding.query,
                )
                result.observations.append(observation)
                observation_ids.append(observation.id)
            relation = (
                Relation.SUPPORTS if outcome.result is CheckResult.HELD else Relation.CONTRADICTS
            )
            result.evidence.append(
                Evidence(
                    next_id("E"), check.id, observation_ids, relation, spec.link_type, spec.strength
                )
            )
        _score(hypothesis, result)

    result.hypotheses.sort(key=lambda h: (-h.score, int(h.id[1:])))
    return result


def _run(run: Callable[[Context, str], Outcome], ctx: Context, component: str) -> Outcome:
    try:
        return run(ctx, component)
    except SourceError as exc:
        return Outcome(CheckResult.NO_DATA, f"the data source failed: {exc}", "")


def _score(hypothesis: Hypothesis, result: Investigation) -> None:
    check_ids = {c.id for c in result.checks if c.hypothesis_id == hypothesis.id}
    own = [e for e in result.evidence if e.check_id in check_ids]
    supports = sum(e.strength for e in own if e.relation is Relation.SUPPORTS)
    contradicts = sum(e.strength for e in own if e.relation is Relation.CONTRADICTS)
    hypothesis.score = round(supports - contradicts, 3)
    if not own:
        hypothesis.status = HypothesisStatus.OPEN
    elif supports and not contradicts:
        hypothesis.status = HypothesisStatus.SUPPORTED
    elif contradicts and not supports:
        hypothesis.status = HypothesisStatus.REFUTED
    else:
        hypothesis.status = (
            HypothesisStatus.SUPPORTED if hypothesis.score > 0 else HypothesisStatus.WEAKENED
        )
