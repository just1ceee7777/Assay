"""Investigation entities. No I/O and no dependencies outside the standard library.

The boundary between facts and guesses runs through these types:
- facts: Observation (what a source returned) and Evidence (how a fact bears on a guess);
- guesses: Hypothesis, with Check as the prediction that tests it;
- a cause is never "confirmed" here: that takes a verified fix, which comes later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Trigger(StrEnum):
    ALERT = "alert"
    MANUAL = "manual"


class Relation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class LinkType(StrEnum):
    TEMPORAL = "temporal"
    CORRELATION = "correlation"
    DEPENDENCY = "dependency"
    VERIFIED_CAUSAL = "verified_causal"


class CheckResult(StrEnum):
    HELD = "held"  # what the hypothesis predicts was observed
    FAILED = "failed"  # the data shows the prediction did not hold
    NO_DATA = "no_data"  # the data needed to tell is missing


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    WEAKENED = "weakened"
    REFUTED = "refuted"


@dataclass
class Incident:
    id: str
    trigger: Trigger
    window_start: datetime
    window_end: datetime
    alert: dict[str, str] = field(default_factory=dict)
    # The query and the firing interval that produced the window, when there was an alert.
    trigger_detail: dict[str, Any] = field(default_factory=dict)
    # When the symptoms were present, if known (from the alert). A cause has to be
    # present then or just before; the wider window only gives context.
    symptoms_start: datetime | None = None
    symptoms_end: datetime | None = None


@dataclass
class Observation:
    id: str
    source: str
    component: str
    ts: datetime
    kind: str
    payload: dict[str, Any]
    query: str


@dataclass
class Hypothesis:
    id: str
    cause_type: str
    component: str
    origin: str = "rule"
    score: float = 0.0
    status: HypothesisStatus = HypothesisStatus.OPEN


@dataclass
class Check:
    id: str
    hypothesis_id: str
    name: str
    description: str
    query: str
    expected_if_true: str
    result: CheckResult
    detail: str
    ran_at: datetime


@dataclass
class Evidence:
    id: str
    check_id: str
    observation_ids: list[str]
    relation: Relation
    link_type: LinkType
    strength: float
