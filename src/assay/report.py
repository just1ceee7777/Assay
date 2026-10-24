"""Build a report from an investigation and refuse to publish it unless every claim is backed.

A report is derived data. Each claim carries the ids of the Evidence it rests on, and the
validator checks those ids against the investigation. Text that cannot cite evidence (for
example, what data is missing) goes into separate sections that make no claims.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from assay import __version__
from assay.domain import CheckResult, Evidence, Hypothesis, HypothesisStatus, Relation
from assay.investigate import Investigation


@dataclass
class Claim:
    id: str
    text: str
    relation: Relation
    evidence_ids: list[str]
    hypothesis_id: str | None = None


@dataclass
class Report:
    investigation: Investigation
    claims: list[Claim]
    conclusion: str  # "cause" | "undecided" | "insufficient_data"
    missing: list[str]  # what data would have helped, from checks without data
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def build_report(investigation: Investigation) -> Report:
    inv = investigation
    checks = {c.id: c for c in inv.checks}
    claims: list[Claim] = []

    def evidence_of(h: Hypothesis) -> list[Evidence]:
        return [e for e in inv.evidence if checks[e.check_id].hypothesis_id == h.id]

    for h in inv.hypotheses:
        for e in evidence_of(h):
            claims.append(
                Claim(f"S{len(claims) + 1}", checks[e.check_id].detail, e.relation, [e.id], h.id)
            )

    supported = [h for h in inv.hypotheses if h.status is HypothesisStatus.SUPPORTED]
    if not supported:
        conclusion = "insufficient_data"
    elif len(supported) > 1 and supported[0].score == supported[1].score:
        conclusion = "undecided"
    else:
        conclusion = "cause"
        top = supported[0]
        backing = [e.id for e in evidence_of(top) if e.relation is Relation.SUPPORTS]
        against = [e for e in evidence_of(top) if e.relation is Relation.CONTRADICTS]
        text = (
            f"Most likely cause: {top.cause_type} ({top.component}). "
            f"{len(backing)} check(s) support it, {len(against)} contradict it. "
            "Not confirmed: a cause is confirmed only by a verified fix."
        )
        claims.insert(0, Claim("S0", text, Relation.SUPPORTS, backing, top.id))

    missing = sorted({c.detail for c in inv.checks if c.result is CheckResult.NO_DATA})
    report = Report(inv, claims, conclusion, missing)
    report.errors = validate(report)
    return report


def validate(report: Report) -> list[str]:
    """Every claim cites existing evidence of the right relation, with existing observations."""
    inv = report.investigation
    evidence = {e.id: e for e in inv.evidence}
    observations = {o.id for o in inv.observations}
    errors: list[str] = []
    for claim in report.claims:
        if not claim.evidence_ids:
            errors.append(f"{claim.id}: no evidence cited")
        for eid in claim.evidence_ids:
            cited = evidence.get(eid)
            if cited is None:
                errors.append(f"{claim.id}: evidence {eid} does not exist")
                continue
            if cited.relation is not claim.relation:
                errors.append(
                    f"{claim.id}: evidence {eid} {cited.relation}, claim {claim.relation}"
                )
            if not cited.observation_ids:
                errors.append(f"{claim.id}: evidence {eid} has no observations")
            for oid in cited.observation_ids:
                if oid not in observations:
                    errors.append(f"{claim.id}: observation {oid} does not exist")
    return errors


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def to_dict(report: Report) -> dict[str, Any]:
    inv = report.investigation
    return {
        "assay_version": __version__,
        "valid": report.valid,
        "errors": report.errors,
        "conclusion": report.conclusion,
        "incident": _plain(dataclasses.asdict(inv.incident)),
        "hypotheses": [
            {"rank": i + 1, **_plain(dataclasses.asdict(h))} for i, h in enumerate(inv.hypotheses)
        ],
        "claims": [_plain(dataclasses.asdict(c)) for c in report.claims],
        "missing": report.missing,
        "checks": [_plain(dataclasses.asdict(c)) for c in inv.checks],
        "evidence": [_plain(dataclasses.asdict(e)) for e in inv.evidence],
        "observations": [_plain(dataclasses.asdict(o)) for o in inv.observations],
    }


def to_json(report: Report) -> str:
    return json.dumps(to_dict(report), indent=2, ensure_ascii=False)


def to_markdown(report: Report) -> str:
    inv = report.investigation
    inc = inv.incident
    lines = [
        f"# Incident {inc.id}",
        "",
        f"- Trigger: {inc.trigger.value}"
        + (f" `{inc.alert.get('alertname', '')}`" if inc.alert else ""),
        f"- Window: {inc.window_start.isoformat()} — {inc.window_end.isoformat()}",
        "",
        "## Conclusion",
        "",
    ]
    if report.conclusion == "cause":
        lines.append(f"{report.claims[0].text} [{', '.join(report.claims[0].evidence_ids)}]")
    elif report.conclusion == "undecided":
        lines.append("The data supports more than one cause equally; Assay cannot tell them apart.")
    else:
        lines.append("Not enough data: no hypothesis is supported by the checks.")
    lines += ["", "## Hypotheses", "", "| # | Cause | Component | Status | Score |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for i, h in enumerate(inv.hypotheses, 1):
        lines.append(f"| {i} | {h.cause_type} | {h.component} | {h.status.value} | {h.score:+g} |")
    lines += ["", "## Findings", ""]
    names = {h.id: f"{h.cause_type} ({h.component})" for h in inv.hypotheses}
    for claim in report.claims:
        if claim.id == "S0":
            continue
        hyp = names.get(claim.hypothesis_id or "", "")
        lines.append(
            f"- {claim.text} — {claim.relation.value} {hyp} [{', '.join(claim.evidence_ids)}]"
        )
    if report.missing:
        lines += ["", "## Missing data", ""]
        lines += [f"- {item}" for item in report.missing]
    lines += ["", "## Evidence", ""]
    checks = {c.id: c for c in inv.checks}
    obs = {o.id: o for o in inv.observations}
    for e in inv.evidence:
        check = checks[e.check_id]
        lines.append(f"- **{e.id}** {check.name}: {e.relation.value}, {e.link_type.value}")
        for oid in e.observation_ids:
            o = obs[oid]
            lines.append(f"  - {oid} {o.source} `{o.query}` → {json.dumps(o.payload)}")
    return "\n".join(lines) + "\n"
