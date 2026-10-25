from __future__ import annotations

from typing import Any

from run_eval import score_run, summarize

RECORD: dict[str, Any] = {
    "ground_truth": {"root_cause_type": "dependency_unavailable", "component": "redis"},
    "events": [
        {"event": "warmup_started", "at": "2026-10-25T12:00:00+00:00"},
        {"event": "fault_injected", "at": "2026-10-25T12:02:00+00:00"},
        {"event": "fault_recovered", "at": "2026-10-25T12:05:00+00:00"},
        {"event": "cooldown_finished", "at": "2026-10-25T12:07:00+00:00"},
    ],
}


def report(first_seen: str, *ranked: tuple[str, str]) -> dict[str, Any]:
    return {
        "valid": True,
        "conclusion": "cause",
        "incident": {"trigger_detail": {"first_seen": first_seen}},
        "hypotheses": [
            {"cause_type": c, "component": comp, "status": "supported"} for c, comp in ranked
        ],
        "claims": [{"evidence_ids": ["E1"]}, {"evidence_ids": []}],
    }


def test_correct_top1() -> None:
    row = score_run(
        RECORD,
        report("2026-10-25T12:02:40+00:00", ("dependency_unavailable", "redis"), ("x", "y")),
    )
    assert row["correct_top1"] and row["in_top3"] and row["alert_in_run"]
    assert row["claims_with_evidence"] == 1


def test_wrong_top1_and_alert_from_another_run() -> None:
    row = score_run(
        RECORD,
        report("2026-10-25T11:50:00+00:00", ("x", "y"), ("dependency_unavailable", "redis")),
    )
    assert row["rank"] == 2 and not row["correct_top1"] and row["in_top3"]
    assert not row["alert_in_run"]


def test_summary() -> None:
    rows = [
        {
            "correct_top1": True,
            "in_top3": True,
            "claims": 2,
            "claims_with_evidence": 2,
            "report_valid": True,
            "alert_in_run": True,
            "investigation_seconds": 1.0,
        },
        {
            "correct_top1": False,
            "in_top3": True,
            "claims": 2,
            "claims_with_evidence": 1,
            "report_valid": True,
            "alert_in_run": True,
            "investigation_seconds": 3.0,
        },
    ]
    s = summarize(rows)
    assert s["top1"] == 0.5 and s["top3"] == 1.0 and s["claims_with_evidence"] == 0.75
    assert s["mean_investigation_seconds"] == 2.0
